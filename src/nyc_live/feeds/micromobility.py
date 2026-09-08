"""Citi Bike GBFS adapter.

Discovers `station_information` and `station_status` from the GBFS root
(`settings.citibike_gbfs_root`) and joins them on `station_id` into
`BikeStation` records. Child URLs are never hardcoded: whatever host the root
publishes is what gets fetched.

Root shapes handled:
* GBFS 2.x: ``data.<lang>.feeds`` (the ``en`` block is preferred, else the first block)
* GBFS 3.x: ``data.feeds``

Freshness:
* ``upstream_generated_at`` = ``last_updated`` of ``station_status`` (root as fallback)
* ``stale_after`` = ``fetched_at`` + max(``DEFAULT_TTL[CITIBIKE]``, published ``ttl``)

Ebike counts come from ``num_ebikes_available`` when present, else from
``vehicle_types_available`` (classified through the ``vehicle_types`` feed when
the root lists one), else ``None``. Nothing is invented.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    BikeStation,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    Snapshot,
    now_utc,
)
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

FEED = FeedName.CITIBIKE

STATION_INFORMATION = "station_information"
STATION_STATUS = "station_status"
VEHICLE_TYPES = "vehicle_types"

MAX_UNMATCHED_FRACTION = 0.05
"""More than this share of station_ids present on only one side of the join is a parse failure."""

ELECTRIC_PROPULSION = frozenset({"electric_assist", "electric"})
"""GBFS `propulsion_type` values that count as an ebike."""


class CitiBikeAdapter:
    """FeedAdapter[BikeStation] over the public Citi Bike GBFS feeds (no auth)."""

    name: FeedName = FEED
    ttl: timedelta = DEFAULT_TTL[FEED]

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._limiter = RateLimiter(self.ttl)

    def is_configured(self) -> bool:
        return True

    async def fetch(self) -> Snapshot[BikeStation]:
        root_url = self._settings.citibike_gbfs_root
        await self._limiter.wait(root_url)
        started = time.perf_counter()

        root = await self._get_json(root_url)
        feeds = _discover_feeds(root, root_url)
        info_url = feeds.get(STATION_INFORMATION)
        status_url = feeds.get(STATION_STATUS)
        if info_url is None or status_url is None:
            raise FeedUnavailable(
                FEED,
                f"GBFS root does not list {STATION_INFORMATION} and {STATION_STATUS}; "
                f"found {sorted(feeds)}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=root_url,
            )

        info_doc, status_doc = await asyncio.gather(
            self._get_json(info_url), self._get_json(status_url)
        )
        info_rows = _stations(info_doc, info_url)
        status_rows = _stations(status_doc, status_url)

        ebike_type_ids: frozenset[str] | None = None
        if _needs_vehicle_types(status_rows):
            vt_url = feeds.get(VEHICLE_TYPES)
            if vt_url is None:
                log.warning(
                    "citibike: station_status lacks num_ebikes_available and the root lists no "
                    "%s feed; ebikes_available will be None",
                    VEHICLE_TYPES,
                )
            else:
                ebike_type_ids = _ebike_type_ids(await self._get_json(vt_url), vt_url)

        fetched_at = now_utc()
        records = _join(info_rows, status_rows, ebike_type_ids, status_url)

        published_ttl = _as_int(status_doc.get("ttl"))
        if published_ttl is None:
            published_ttl = _as_int(root.get("ttl"))
        ttl = self.ttl
        if published_ttl is not None and published_ttl > 0:
            ttl = max(ttl, timedelta(seconds=published_ttl))

        generated_at = _parse_timestamp(status_doc.get("last_updated"))
        if generated_at is None:
            generated_at = _parse_timestamp(root.get("last_updated"))

        return Snapshot[BikeStation](
            feed=FEED,
            fetched_at=fetched_at,
            stale_after=fetched_at + ttl,
            source_url=root_url,
            records=records,
            upstream_generated_at=generated_at,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def _get_json(self, url: str) -> dict[str, Any]:
        resp = await get_with_retry(
            self._client, url, feed=FEED, retries=self._settings.http_retries
        )
        try:
            body = resp.json()
        except ValueError as exc:
            raise FeedUnavailable(
                FEED,
                f"response from {url} is not JSON: {exc}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=url,
                upstream_status=resp.status_code,
            ) from exc
        if not isinstance(body, dict):
            raise FeedUnavailable(
                FEED,
                f"response from {url} is not a JSON object",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=url,
                upstream_status=resp.status_code,
            )
        return body


# ---------------------------------------------------------------------------
# Root discovery
# ---------------------------------------------------------------------------


def _discover_feeds(root: Mapping[str, Any], root_url: str) -> dict[str, str]:
    """Return {feed name: url} from a GBFS 2.x (`data.<lang>.feeds`) or 3.x (`data.feeds`) root."""
    data = root.get("data")
    if not isinstance(data, dict):
        raise FeedUnavailable(
            FEED, "GBFS root has no 'data' object", kind=ErrorKind.UPSTREAM_PARSE, url=root_url
        )
    feeds = data.get("feeds")
    if not isinstance(feeds, list):
        block = data.get("en")
        if not isinstance(block, dict):
            block = next((v for v in data.values() if isinstance(v, dict) and "feeds" in v), None)
        feeds = block.get("feeds") if isinstance(block, dict) else None
    if not isinstance(feeds, list):
        raise FeedUnavailable(
            FEED,
            "GBFS root has neither data.feeds nor data.<lang>.feeds",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=root_url,
        )
    out: dict[str, str] = {}
    for entry in feeds:
        if not isinstance(entry, dict):
            continue
        name, url = entry.get("name"), entry.get("url")
        if isinstance(name, str) and isinstance(url, str) and url:
            out.setdefault(name, url)
    return out


def _stations(doc: Mapping[str, Any], url: str) -> list[dict[str, Any]]:
    data = doc.get("data")
    stations = data.get("stations") if isinstance(data, dict) else None
    if not isinstance(stations, list):
        raise FeedUnavailable(
            FEED,
            f"{url} has no data.stations list",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
        )
    return [row for row in stations if isinstance(row, dict)]


def _needs_vehicle_types(status_rows: list[dict[str, Any]]) -> bool:
    return any(
        _as_int(row.get("num_ebikes_available")) is None
        and isinstance(row.get("vehicle_types_available"), list)
        for row in status_rows
    )


def _ebike_type_ids(doc: Mapping[str, Any], url: str) -> frozenset[str]:
    data = doc.get("data")
    types = data.get("vehicle_types") if isinstance(data, dict) else None
    if not isinstance(types, list):
        raise FeedUnavailable(
            FEED,
            f"{url} has no data.vehicle_types list",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
        )
    ids: set[str] = set()
    for vt in types:
        if not isinstance(vt, dict):
            continue
        type_id = vt.get("vehicle_type_id")
        if type_id is not None and vt.get("propulsion_type") in ELECTRIC_PROPULSION:
            ids.add(str(type_id))
    return frozenset(ids)


# ---------------------------------------------------------------------------
# Join + mapping
# ---------------------------------------------------------------------------


def _join(
    info_rows: list[dict[str, Any]],
    status_rows: list[dict[str, Any]],
    ebike_type_ids: frozenset[str] | None,
    status_url: str,
) -> list[BikeStation]:
    info_by_id = {sid: row for row in info_rows if (sid := _as_str(row.get("station_id")))}
    status_by_id = {sid: row for row in status_rows if (sid := _as_str(row.get("station_id")))}
    all_ids = info_by_id.keys() | status_by_id.keys()
    if not all_ids:
        raise FeedUnavailable(
            FEED,
            "station_information and station_status contain no stations",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=status_url,
        )

    matched = info_by_id.keys() & status_by_id.keys()
    status_only = len(status_by_id) - len(matched)
    info_only = len(info_by_id) - len(matched)
    unmatched = status_only + info_only
    fraction = unmatched / len(all_ids)
    if unmatched:
        log.info(
            "citibike: dropped %d unmatched station rows (%d status-only, %d info-only) of %d "
            "(%.1f%%)",
            unmatched,
            status_only,
            info_only,
            len(all_ids),
            fraction * 100,
        )
    if fraction > MAX_UNMATCHED_FRACTION:
        raise FeedUnavailable(
            FEED,
            f"{unmatched} of {len(all_ids)} station_ids ({fraction:.1%}) appear in only one of "
            f"station_information / station_status (limit {MAX_UNMATCHED_FRACTION:.0%})",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=status_url,
        )

    records: list[BikeStation] = []
    out_of_bbox = 0
    malformed = 0
    for sid in sorted(matched):
        info, status = info_by_id[sid], status_by_id[sid]
        lat, lon = _as_float(info.get("lat")), _as_float(info.get("lon"))
        if lat is None or lon is None:
            malformed += 1
            continue
        if not in_nyc_bbox(lat, lon):
            out_of_bbox += 1
            continue
        record = _build_record(sid, info, status, lat=lat, lon=lon, ebike_type_ids=ebike_type_ids)
        if record is None:
            malformed += 1
            continue
        records.append(record)

    if out_of_bbox:
        log.info("citibike: dropped %d stations with coordinates outside NYC bbox", out_of_bbox)
    if malformed:
        log.warning("citibike: dropped %d stations with missing or malformed fields", malformed)
    if not records:
        raise FeedUnavailable(
            FEED,
            f"no usable stations after join ({len(matched)} matched, {malformed} malformed, "
            f"{out_of_bbox} outside bbox)",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=status_url,
        )
    return records


def _build_record(
    station_id: str,
    info: Mapping[str, Any],
    status: Mapping[str, Any],
    *,
    lat: float,
    lon: float,
    ebike_type_ids: frozenset[str] | None,
) -> BikeStation | None:
    name = _as_str(info.get("name"))
    bikes = _as_int(status.get("num_bikes_available"))
    docks = _as_int(status.get("num_docks_available"))
    is_renting = _as_bool(status.get("is_renting"))
    is_returning = _as_bool(status.get("is_returning"))
    if name is None or bikes is None or docks is None or is_renting is None or is_returning is None:
        return None
    is_installed = _as_bool(status.get("is_installed"))
    try:
        return BikeStation(
            lat=lat,
            lon=lon,
            station_id=station_id,
            name=name,
            capacity=_as_int(info.get("capacity")),
            bikes_available=bikes,
            ebikes_available=_ebikes(status, ebike_type_ids),
            docks_available=docks,
            is_renting=is_renting,
            is_returning=is_returning,
            is_installed=True if is_installed is None else is_installed,
            last_reported=_parse_timestamp(status.get("last_reported")),
        )
    except ValidationError as exc:
        log.debug("citibike: station %s rejected by BikeStation: %s", station_id, exc)
        return None


def _ebikes(status: Mapping[str, Any], ebike_type_ids: frozenset[str] | None) -> int | None:
    """num_ebikes_available, else a sum over vehicle_types_available, else None."""
    direct = _as_int(status.get("num_ebikes_available"))
    if direct is not None:
        return direct
    available = status.get("vehicle_types_available")
    if ebike_type_ids is None or not isinstance(available, list):
        return None
    total = 0
    for entry in available:
        if not isinstance(entry, dict):
            continue
        count = _as_int(entry.get("count"))
        if count is not None and str(entry.get("vehicle_type_id")) in ebike_type_ids:
            total += count
    return total


# ---------------------------------------------------------------------------
# Coercion helpers (GBFS 2.x uses 0/1 ints and epoch seconds; 3.x uses bools and RFC3339)
# ---------------------------------------------------------------------------


def _as_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None
