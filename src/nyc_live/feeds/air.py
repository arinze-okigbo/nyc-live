"""Open-Meteo air-quality adapter: one hourly ``AirQualityReading`` per NYC grid cell.

Keyless and free -- AirNow and PurpleAir both require an API key, Open-Meteo does
not. One ``GET /v1/air-quality`` per fetch: the endpoint accepts comma-separated
multi-coordinate requests, so five borough anchors cost a single request against a
rate-limited free tier instead of five.

GRID SNAPPING
-------------
Open-Meteo answers from its own ~0.1 deg model grid, never from the coordinates
asked for. Central Park (40.7829, -73.9654) comes back as (40.800003, -74.0),
about 3 km away and across the Hudson. ``AirQualityReading.lat``/``lon`` are
therefore the grid point actually sampled, as the contract requires: the requested
anchor is never presented as the sampled location, it survives only in log lines.

Because of that snapping two different requests can land on the *same* cell.
Measured 2026-09-09: LaGuardia (40.7769, -73.8740) and the Bronx (40.8448,
-73.8648) both resolve to (40.800003, -73.9) -- one sample, returned twice.
Readings are therefore deduplicated on the returned grid coordinate, so a single
sample is never reported as two markers.

Readings are deliberately *not* deduplicated on value. The five borough anchors
below resolve to five distinct grid cells, and those cells genuinely diverge:
measured in the same hour, US AQI 70 over Manhattan/Bronx/Brooklyn, 67 over
Queens, 56 over Staten Island. Two distinct cells whose numbers happen to agree
in one hour are still two cells, and collapsing them would erase the spatial
layer the next hour needs.

TIMESTAMPS
----------
``current.time`` is naive ("2026-09-09T20:00"); its offset lives beside it in
``utc_offset_seconds`` (0, with ``timezone: "GMT"``, because the request sends no
timezone). The offset is attached explicitly and the result converted to UTC --
never assumed local, never let through naive, since the contract demands an
``AwareDatetime``.

Every pollutant field is optional upstream. Nulls stay ``None``; nothing is filled.

CONFIG
------
The base URL comes from ``Settings.air_quality_base``
(``NYC_LIVE_AIR_QUALITY_BASE``) like every other upstream, read through the
``base`` property on each call rather than bound at import time, so this feed
can be redirected or killed by env.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import ValidationError

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    AirQualityReading,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    Snapshot,
    now_utc,
)
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

FEED = FeedName.AIR_QUALITY

CURRENT_VARIABLES: tuple[str, ...] = (
    "us_aqi",
    "pm2_5",
    "pm10",
    "ozone",
    "nitrogen_dioxide",
)
"""Exactly the fields `AirQualityReading` carries. Adding one here needs a contract change."""

FAILURE_RETRY_FLOOR = timedelta(seconds=60)
"""After a failed fetch the 1 h cadence floor is released so the next refresh is not
stuck behind the whole TTL -- but this shorter floor still applies, so a broken
upstream is retried at most once a minute rather than hammered on a free tier."""


@dataclass(frozen=True)
class SamplePoint:
    """A point we ask about. What comes back is a grid cell near it, not this."""

    name: str
    lat: float
    lon: float


DEFAULT_SAMPLE_POINTS: tuple[SamplePoint, ...] = (
    SamplePoint("Manhattan", 40.7829, -73.9654),
    SamplePoint("Bronx", 40.8448, -73.8648),
    SamplePoint("Brooklyn", 40.6782, -73.9442),
    SamplePoint("Queens", 40.7282, -73.7949),
    SamplePoint("Staten Island", 40.5795, -74.1502),
)
"""One anchor per borough. Verified 2026-09-09 to resolve to five *distinct* grid
cells; a denser set (weather.py's Central Park / LaGuardia / JFK plus these) does
collapse, which is why dedupe_by_grid_point exists."""


# ---------------------------------------------------------------------------
# Parsing (pure; raises ValueError on any shape problem)
# ---------------------------------------------------------------------------


def _require_mapping(obj: object, where: str) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise ValueError(f"{where}: expected an object, got {type(obj).__name__}")
    return obj


def _number(raw: object, where: str) -> float | None:
    """Upstream null -> None (never filled). Anything non-numeric is a shape error."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ValueError(f"{where}: expected a number or null, got {raw!r}")
    return float(raw)


def _required_number(raw: object, where: str) -> float:
    value = _number(raw, where)
    if value is None:
        raise ValueError(f"{where}: required, got null")
    return value


def _aqi(raw: object, where: str) -> int | None:
    """`us_aqi` is an integer index. Upstream sends an int; a float is rounded, not
    rejected, because the EPA index has no fractional part. Null stays None."""
    value = _number(raw, where)
    return None if value is None else round(value)


def parse_observed_at(raw: object, utc_offset_seconds: object, where: str) -> datetime:
    """Turn Open-Meteo's ``current.time`` into an aware UTC datetime.

    The string is normally naive and its offset is a sibling field, so the offset
    is attached explicitly rather than guessed. An already-aware string (should
    the API ever start sending one) is simply converted.
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{where}: current.time is missing or not a string")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{where}: current.time {raw!r} is not ISO-8601: {exc}") from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    offset = _number(utc_offset_seconds, f"{where}.utc_offset_seconds")
    if offset is None:
        raise ValueError(
            f"{where}: current.time {raw!r} is naive and utc_offset_seconds is absent; "
            "refusing to guess the zone"
        )
    return parsed.replace(tzinfo=timezone(timedelta(seconds=int(offset)))).astimezone(UTC)


def parse_reading(entry: Mapping[str, Any], where: str) -> AirQualityReading:
    """One element of the response -> one reading at the grid point actually sampled."""
    current = _require_mapping(entry.get("current"), f"{where}.current")
    return AirQualityReading(
        lat=_required_number(entry.get("latitude"), f"{where}.latitude"),
        lon=_required_number(entry.get("longitude"), f"{where}.longitude"),
        observed_at=parse_observed_at(current.get("time"), entry.get("utc_offset_seconds"), where),
        us_aqi=_aqi(current.get("us_aqi"), f"{where}.current.us_aqi"),
        pm2_5=_number(current.get("pm2_5"), f"{where}.current.pm2_5"),
        pm10=_number(current.get("pm10"), f"{where}.current.pm10"),
        ozone=_number(current.get("ozone"), f"{where}.current.ozone"),
        nitrogen_dioxide=_number(
            current.get("nitrogen_dioxide"), f"{where}.current.nitrogen_dioxide"
        ),
    )


def parse_air_quality(body: object) -> tuple[list[AirQualityReading], float | None]:
    """Parse a whole ``/v1/air-quality`` body into readings plus the upstream interval.

    A multi-coordinate request answers with a JSON array, a single-coordinate one
    with a bare object; both are accepted. The returned interval is the shortest
    ``current.interval`` seen (3600 s in practice), or None if upstream omitted it.
    """
    if isinstance(body, Mapping):
        entries: list[Any] = [body]
    elif isinstance(body, list):
        entries = list(body)
    else:
        raise ValueError(f"body is neither an object nor an array, got {type(body).__name__}")
    if not entries:
        raise ValueError("body is an empty array; no grid point was returned")

    readings: list[AirQualityReading] = []
    intervals: list[float] = []
    for index, raw in enumerate(entries):
        where = f"entry[{index}]"
        entry = _require_mapping(raw, where)
        if entry.get("error"):
            raise ValueError(f"{where}: upstream reported an error: {entry.get('reason')!r}")
        readings.append(parse_reading(entry, where))
        current = _require_mapping(entry.get("current"), f"{where}.current")
        interval = _number(current.get("interval"), f"{where}.current.interval")
        if interval is not None and interval > 0:
            intervals.append(interval)
    return readings, (min(intervals) if intervals else None)


def dedupe_by_grid_point(readings: Iterable[AirQualityReading]) -> list[AirQualityReading]:
    """Drop repeats of the same sampled cell, keeping the first, preserving order.

    Two requested points that snap to one grid cell are one sample. Emitting it
    twice would fabricate a second observation out of nothing.
    """
    seen: set[tuple[float, float]] = set()
    kept: list[AirQualityReading] = []
    for reading in readings:
        key = (reading.lat, reading.lon)
        if key in seen:
            log.info(
                "air_quality: two requested points snapped onto grid cell (%s, %s); "
                "keeping one reading",
                reading.lat,
                reading.lon,
            )
            continue
        seen.add(key)
        kept.append(reading)
    return kept


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AirQualityAdapter:
    name: FeedName = FEED
    backoff_s: float = 1.0  # gentler than the house default; free tier

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self.ttl: timedelta = DEFAULT_TTL[self.name]
        self.points: tuple[SamplePoint, ...] = DEFAULT_SAMPLE_POINTS
        self._limiter = RateLimiter(self.ttl)
        self._retry_limiter = RateLimiter(FAILURE_RETRY_FLOOR)

    def is_configured(self) -> bool:
        return True

    @property
    def base(self) -> str:
        # Read per call, never cached on the instance: an env redirect must take
        # effect without reconstructing the adapter.
        return self._settings.air_quality_base.rstrip("/")

    @property
    def params(self) -> dict[str, str]:
        return {
            "latitude": ",".join(f"{p.lat:.4f}" for p in self.points),
            "longitude": ",".join(f"{p.lon:.4f}" for p in self.points),
            "current": ",".join(CURRENT_VARIABLES),
        }

    @property
    def source_url(self) -> str:
        return str(httpx.URL(self.base, params=self.params))

    async def fetch(self) -> Snapshot[AirQualityReading]:
        key = self.name.value
        await self._limiter.wait(key)
        await self._retry_limiter.wait(key)
        try:
            return await self._fetch_once()
        except BaseException:
            # Release the 1 h floor so the next refresh is not stuck sleeping a whole
            # TTL inside the caller's refresh lock. The 60 s floor stamped by
            # _retry_limiter.wait() above still stands, so a down upstream is retried
            # at most once a minute. After a *success* this is never reached and the
            # 1 h floor governs, which is strictly longer than the 60 s one.
            self._limiter.forget(key)
            raise

    async def _fetch_once(self) -> Snapshot[AirQualityReading]:
        started = time.perf_counter()
        fetched_at = now_utc()
        source_url = self.source_url
        resp = await get_with_retry(
            self._client,
            self.base,
            feed=self.name,
            retries=self._settings.http_retries,
            params=self.params,
            backoff_s=self.backoff_s,
        )
        try:
            body = resp.json()
        except ValueError as exc:
            raise FeedUnavailable(
                self.name,
                f"non-JSON body from {self.base}: {exc}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=source_url,
                upstream_status=resp.status_code,
            ) from exc

        try:
            readings, interval_s = parse_air_quality(body)
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            raise FeedUnavailable(
                self.name,
                f"unexpected air-quality payload: {exc}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=source_url,
                upstream_status=resp.status_code,
            ) from exc

        unique = dedupe_by_grid_point(readings)
        kept = [r for r in unique if in_nyc_bbox(r.lat, r.lon)]
        dropped = len(unique) - len(kept)
        if dropped:
            log.warning(
                "air_quality: dropped %d of %d grid point(s) outside the NYC bbox",
                dropped,
                len(unique),
            )
        if not kept:
            raise FeedUnavailable(
                self.name,
                f"none of the {len(readings)} sampled grid point(s) fell inside the NYC "
                "bbox; the configured sample points are wrong",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=source_url,
                upstream_status=resp.status_code,
            )

        ttl = self.ttl if interval_s is None else max(self.ttl, timedelta(seconds=interval_s))
        return Snapshot[AirQualityReading](
            feed=self.name,
            fetched_at=fetched_at,
            stale_after=fetched_at + ttl,
            source_url=source_url,
            records=kept,
            upstream_generated_at=max(r.observed_at for r in kept),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )


__all__ = [
    "CURRENT_VARIABLES",
    "DEFAULT_SAMPLE_POINTS",
    "FAILURE_RETRY_FLOOR",
    "AirQualityAdapter",
    "SamplePoint",
    "dedupe_by_grid_point",
    "parse_air_quality",
    "parse_observed_at",
    "parse_reading",
]
