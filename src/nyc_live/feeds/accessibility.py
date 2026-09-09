"""MTA elevator & escalator outages (`nyct_ene.json`), placed on the map where we can.

Upstream (no API key required, same keyless api.mta.info gateway as the GTFS feeds)
-----------------------------------------------------------------------------------
* Outages: ``{settings.mta_gtfs_base}/nyct%2Fnyct_ene.json``. The ``%2F`` is part of the
  path and is sent verbatim (one URL-encoded path segment), exactly like the GTFS feeds
  in ``nyc_live.feeds.transit``; an unescaped slash gets API Gateway's misleading
  ``403 Missing Authentication Token``. Verified live 2026-09-09: HTTP 200, a JSON array
  of 136 outage objects, ``content-type: ${header.s3FileType}`` (an unsubstituted
  template on MTA's side -- the content type is worthless, so the body is parsed as JSON
  regardless of what the header claims).
* A WRONG key on this endpoint does NOT 404. The gateway proxies S3 and returns
  **HTTP 200** with an XML ``<Error><Code>NoSuchKey</Code>`` body (verified live). So
  ``get_with_retry``'s 404 handling can never fire here: ``parse_outages`` sniffs that
  body itself and raises ``FeedUnavailable(kind=NOT_FOUND)`` naming the endpoint. A
  renamed or retired key is fatal and loud, never an empty outage list.

Field mapping (real row shape, verified live 2026-09-09)
-------------------------------------------------------
``station``, ``trainno``, ``equipment``, ``equipmenttype``, ``serving``, ``ADA``,
``reason``, ``outagedate``, ``estimatedreturntoservice``, ``isupcomingoutage``,
``ismaintenanceoutage``, ``borough``.

* ``isupcomingoutage`` is the load-bearing one. Currently-out and scheduled-future
  outages ship in the SAME payload (78 upcoming / 58 active in the 2026-09-09 sample);
  conflating them tells a rider an elevator is broken when it is working right now. It
  maps straight to ``ElevatorOutage.is_upcoming`` and nothing else derives from it --
  in particular the adapter never filters upcoming rows out, because "the elevator goes
  out at 10 PM tonight" is exactly what a wheelchair user planning a trip needs.
* ``outagedate`` / ``estimatedreturntoservice`` are ``MM/DD/YYYY HH:MM:SS AM/PM`` --
  a 12-hour clock, unlike every other MTA feed here -- in New York local time with no
  zone. They are localised to ``America/New_York`` and converted to aware UTC. Empty
  strings become ``None``; an unparseable value becomes ``None`` and is counted and
  logged rather than killing a real outage record.
* ``trainno`` is the served route(s), slash-delimited (checked against the real data:
  values run from ``"F"`` to ``"A/C/E/N/Q/R/W/1/2/3/7/S"``, and include the non-subway
  token ``"LIRR"``). It is split on ``/`` and kept verbatim, LIRR included -- that is
  what MTA published about the station.
* ``equipmenttype`` is ``"EL"``/``"ES"`` -> ``ElevatorEquipmentType``. Anything else is
  dropped with a WARNING naming the value: the frozen enum has no "other" member, and
  silently coercing an unknown type to "elevator" would be a lie to a rider who needs a
  lift. If *every* row is unusable the whole feed raises rather than serving nothing.
* ``borough`` is empty on 100% of live rows (136/136) and there is no borough field in
  the contract, so it is read and discarded, deliberately, not smuggled in elsewhere.
* ``equipment`` is NOT unique: the same lift legitimately appears in several rows (one
  per scheduled window). Records are one-per-upstream-row, in upstream order.

Placing an outage: station name -> MTA_SUBWAY_STOPS
---------------------------------------------------
This feed has NO coordinates, only a station *name*, which is why ``ElevatorOutage`` is
``MaybeLocated``. Coordinates are joined from ``SubwayStopsAdapter`` (static GTFS
``stops.txt``, shared through ``transit.RawBytesCache``, 24 h TTL) and the join is
deliberately conservative -- an outage that cannot be placed confidently keeps
``lat``/``lon`` as ``None`` and still ships in the list:

1. Normalise both names (case, punctuation, ``42St`` -> ``42 st``, ``West`` -> ``w``,
   ``Square`` -> ``sq``, ...) and take the GTFS *parent* stations whose name matches.
2. Keep only candidates whose GTFS routes intersect ``trainno``. This is a filter, not
   a tiebreak: a unique name match with contradicting routes is REJECTED. Real example
   -- the outage station "Cortlandt St" on route ``1`` matches exactly one GTFS station
   named "Cortlandt St" (``R25``, the N/R/W platform), but the 1 train's station there
   is called "WTC Cortlandt". Name alone would have placed it at the wrong station.
3. If one candidate survives, use it. If several survive but they all lie within
   ``STATION_COMPLEX_RADIUS_M`` of each other they are the platforms of one station
   complex (Times Sq-42 St spans 119 m; 14 St 6 Av/7 Av spans 339 m), so a real
   candidate's real coordinate is used -- the one serving the most of ``trainno``,
   then the one nearest the candidates' centroid, then lowest stop_id for determinism.
4. Otherwise ``None``. Real example -- "Gun Hill Rd" on ``2/5`` matches two genuinely
   different stations 1,910 m apart (White Plains Rd and Dyre Av); guessing between
   them would send a rider to the wrong station, so it is left unplaced.

Measured on the live 2026-09-09 payload: 133/136 rows placed (97.8%) -- 99 by a unique
name+route match, 34 within a station complex -- 2 rejected for route contradiction
("Cortlandt St") and 1 for spread ("Gun Hill Rd"). No coordinate is ever synthesised.

The stops join is best-effort *enrichment*: if the static GTFS bundle is unreachable the
outages are still served with every ``lat``/``lon`` as ``None`` and a WARNING is logged.
The accessibility feed's own upstream is ``nyct_ene.json``, and it must not go dark
because a 5.6 MB zip on S3 is having a bad day.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    ElevatorEquipmentType,
    ElevatorOutage,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    Snapshot,
    SubwayStop,
    now_utc,
)
from nyc_live.feeds.socrata import NYC_TZ  # the repo's one America/New_York zone
from nyc_live.feeds.transit import SubwayStopsAdapter, raw_cache
from nyc_live.geo import haversine_m, in_nyc_bbox

log = logging.getLogger(__name__)

ENE_SLUG = "nyct%2Fnyct_ene.json"
"""Outage feed key. ``%2F`` is verbatim; see the module docstring."""

ENE_TIMESTAMP_FORMAT = "%m/%d/%Y %I:%M:%S %p"
"""MM/DD/YYYY HH:MM:SS AM/PM, New York local, no zone. 12-hour clock (``%I`` + ``%p``)."""

EQUIPMENT_TYPES: Mapping[str, ElevatorEquipmentType] = {
    "EL": ElevatorEquipmentType.ELEVATOR,
    "ES": ElevatorEquipmentType.ESCALATOR,
}

TRUTHY = frozenset({"Y", "YES", "TRUE", "1"})
"""Live rows only ever use ``Y``/``N``; the rest are accepted defensively."""

ROUTE_DELIMITER = "/"
"""Verified against the live payload: ``"A/C/E/N/Q/R/W/1/2/3/7/S"``, never a comma."""

STATION_COMPLEX_RADIUS_M = 400.0
"""Max spread between surviving candidates that still counts as one station complex.

Calibrated on real data, not guessed: the widest genuine complex in the live payload is
14 St (6 Av F/M/L vs 7 Av 1/2/3) at 339 m, and the nearest false positive is the two
distinct Gun Hill Rd stations at 1,910 m."""

_NOSUCHKEY_MARKERS = (b"<Code>NoSuchKey</Code>", b"<Error>")

_NAME_ABBREVIATIONS: Mapping[str, str] = {
    "avenue": "av",
    "ave": "av",
    "boulevard": "blvd",
    "center": "ctr",
    "centre": "ctr",
    "east": "e",
    "north": "n",
    "parkway": "pkwy",
    "road": "rd",
    "south": "s",
    "square": "sq",
    "street": "st",
    "west": "w",
}
"""Applied token-wise to BOTH sides, so the map only has to be self-consistent."""

_DIGIT_LETTER_RE = re.compile(r"(\d)([a-z])")
_LETTER_DIGIT_RE = re.compile(r"([a-z])(\d)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def outages_url(base: str) -> str:
    return f"{base.rstrip('/')}/{ENE_SLUG}"


# ---------------------------------------------------------------------------
# Row parsing (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutageParse:
    outages: list[ElevatorOutage]
    dropped_missing_id: int
    dropped_unknown_type: int
    bad_timestamps: int


def _text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _flag(row: Mapping[str, Any], key: str) -> bool:
    return _text(row, key).upper() in TRUTHY


def parse_ene_timestamp(raw: str) -> datetime | None:
    """``09/28/2026 10:00:00 PM`` (New York local) -> aware UTC. Empty -> None.

    Raises ``ValueError`` on a non-empty value that does not parse, so the caller can
    count it. During the autumn DST fold an ambiguous local time resolves to the first
    (daylight) occurrence, which is ``datetime``'s default and the only choice the
    upstream string supports.
    """
    if not raw.strip():
        return None
    naive = datetime.strptime(raw.strip(), ENE_TIMESTAMP_FORMAT)
    return naive.replace(tzinfo=NYC_TZ).astimezone(UTC)


def split_routes(raw: str) -> list[str]:
    """``"B/D/N/Q/R/2/3/4/5/LIRR"`` -> the ten tokens, order preserved, de-duplicated.

    Non-subway tokens (``LIRR``) are kept: they are what MTA published about the station.
    """
    seen: set[str] = set()
    out: list[str] = []
    for token in raw.split(ROUTE_DELIMITER):
        route = token.strip()
        if route and route not in seen:
            seen.add(route)
            out.append(route)
    return out


def parse_outage_rows(rows: Sequence[Mapping[str, Any]]) -> OutageParse:
    """Map raw upstream rows to records. Never places them; that is `place_outages`."""
    outages: list[ElevatorOutage] = []
    missing_id = unknown_type = bad_timestamps = 0
    unknown_values: set[str] = set()
    for row in rows:
        equipment_id = _text(row, "equipment")
        station = _text(row, "station")
        if not equipment_id or not station:
            missing_id += 1
            continue
        raw_type = _text(row, "equipmenttype").upper()
        equipment_type = EQUIPMENT_TYPES.get(raw_type)
        if equipment_type is None:
            unknown_type += 1
            unknown_values.add(raw_type)
            continue
        started, started_bad = _timestamp(row, "outagedate")
        estimated, estimated_bad = _timestamp(row, "estimatedreturntoservice")
        bad_timestamps += started_bad + estimated_bad
        outages.append(
            ElevatorOutage(
                equipment_id=equipment_id,
                equipment_type=equipment_type,
                station=station,
                routes=split_routes(_text(row, "trainno")),
                serving=_text(row, "serving") or None,
                is_ada=_flag(row, "ADA"),
                reason=_text(row, "reason") or None,
                outage_started=started,
                estimated_return=estimated,
                is_upcoming=_flag(row, "isupcomingoutage"),
                is_maintenance=_flag(row, "ismaintenanceoutage"),
            )
        )
    if unknown_type:
        log.warning(
            "mta_elevator_outages: dropped %d row(s) with an unmapped equipmenttype %s; "
            "ElevatorEquipmentType has only elevator/escalator",
            unknown_type,
            sorted(unknown_values),
        )
    return OutageParse(outages, missing_id, unknown_type, bad_timestamps)


def _timestamp(row: Mapping[str, Any], key: str) -> tuple[datetime | None, int]:
    raw = _text(row, key)
    try:
        return parse_ene_timestamp(raw), 0
    except ValueError:
        log.warning(
            "mta_elevator_outages: unparseable %s %r on %r", key, raw, _text(row, "equipment")
        )
        return None, 1


def parse_outages(body: bytes, *, url: str) -> OutageParse:
    """Decode the payload or raise. Detects MTA's HTTP-200 ``NoSuchKey`` XML."""
    feed = FeedName.MTA_ELEVATOR_OUTAGES
    head = body.lstrip()[:512]
    # only an XML body is sniffed, so a JSON payload that happens to contain the marker
    # text inside a `serving` string can never be mistaken for an error page
    if head.startswith(b"<") and any(marker in head for marker in _NOSUCHKEY_MARKERS):
        raise FeedUnavailable(
            feed,
            f"{url} returned an S3 NoSuchKey error body with HTTP 200; the "
            f"{ENE_SLUG!r} key is wrong or retired",
            kind=ErrorKind.NOT_FOUND,
            url=url,
            upstream_status=200,
        )
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FeedUnavailable(
            feed,
            f"body ({len(body)} bytes) is not JSON: {exc}",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
        ) from exc
    if not isinstance(payload, list):
        raise FeedUnavailable(
            feed,
            f"expected a JSON array of outages, got {type(payload).__name__}",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
        )
    rows = [row for row in payload if isinstance(row, Mapping)]
    if len(rows) != len(payload):
        raise FeedUnavailable(
            feed,
            f"{len(payload) - len(rows)} of {len(payload)} array items are not objects",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
        )
    parsed = parse_outage_rows(rows)
    if rows and not parsed.outages:
        raise FeedUnavailable(
            feed,
            f"none of the {len(rows)} upstream rows were usable "
            f"({parsed.dropped_missing_id} without equipment/station, "
            f"{parsed.dropped_unknown_type} with an unmapped equipmenttype)",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
        )
    return parsed


# ---------------------------------------------------------------------------
# Station-name matching (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StationCandidate:
    stop_id: str
    name: str
    lat: float
    lon: float
    routes: frozenset[str]


@dataclass(frozen=True)
class StationIndex:
    """Normalised-name -> GTFS parent stations, built once per stops snapshot."""

    by_name: Mapping[str, tuple[StationCandidate, ...]]
    stop_count: int

    def candidates(self, station: str) -> tuple[StationCandidate, ...]:
        return self.by_name.get(normalize_station_name(station), ())


@dataclass(frozen=True)
class PlacementStats:
    placed_unique: int = 0
    placed_complex: int = 0
    no_name_match: int = 0
    route_contradiction: int = 0
    spread_too_wide: int = 0
    out_of_bbox: int = 0

    @property
    def placed(self) -> int:
        return self.placed_unique + self.placed_complex


def normalize_station_name(name: str) -> str:
    """Fold an MTA station name to a comparable key.

    ``"42St/Port Authority-Bus Terminal"`` and GTFS's
    ``"42 St-Port Authority Bus Terminal"`` both fold to
    ``"42 st port authority bus terminal"``; ``"West 8 St-NY Aquarium"`` folds onto
    GTFS's ``"W 8 St-NY Aquarium"``. Both real misses in the live payload.
    """
    lowered = name.lower()
    split = _LETTER_DIGIT_RE.sub(r"\1 \2", _DIGIT_LETTER_RE.sub(r"\1 \2", lowered))
    tokens = _NON_ALNUM_RE.sub(" ", split).split()
    return " ".join(_NAME_ABBREVIATIONS.get(t, t) for t in tokens)


def canonical_route(route_id: str) -> str:
    """Fold a route id for comparison: express variants and shuttles onto their parent.

    GTFS uses ``6X``/``7X``/``FX`` for express patterns and ``GS``/``FS``/``H``/``SI``
    for the shuttles that ``trainno`` writes as ``S``.
    """
    route = route_id.strip().upper()
    if route in {"GS", "FS", "H", "SI", "SS"}:
        return "S"
    return route[:-1] if len(route) > 1 and route.endswith("X") else route


def build_station_index(stops: Iterable[SubwayStop]) -> StationIndex:
    """Index the GTFS *parent* stations (children are platforms of the same station)."""
    by_name: dict[str, list[StationCandidate]] = {}
    count = 0
    for stop in stops:
        if stop.parent_station is not None:
            continue
        count += 1
        by_name.setdefault(normalize_station_name(stop.name), []).append(
            StationCandidate(
                stop_id=stop.stop_id,
                name=stop.name,
                lat=stop.lat,
                lon=stop.lon,
                routes=frozenset(canonical_route(r) for r in stop.routes),
            )
        )
    return StationIndex(
        by_name={name: tuple(cands) for name, cands in by_name.items()}, stop_count=count
    )


def _spread_m(candidates: Sequence[StationCandidate]) -> float:
    return max(haversine_m(a.lat, a.lon, b.lat, b.lon) for a in candidates for b in candidates)


def _pick_in_complex(
    candidates: Sequence[StationCandidate], routes: frozenset[str]
) -> StationCandidate:
    """Most of `routes` served, then nearest the candidates' centroid, then stop_id."""
    lat = sum(c.lat for c in candidates) / len(candidates)
    lon = sum(c.lon for c in candidates) / len(candidates)
    return min(
        candidates,
        key=lambda c: (-len(c.routes & routes), haversine_m(lat, lon, c.lat, c.lon), c.stop_id),
    )


def match_station(
    index: StationIndex, station: str, routes: Iterable[str]
) -> tuple[StationCandidate | None, str]:
    """Resolve one outage to a GTFS station. Returns (candidate, reason).

    `reason` is the placement bucket -- ``unique``, ``complex``, ``no_name_match``,
    ``route_contradiction`` or ``spread_too_wide`` -- so callers can report an honest
    match rate instead of a silent None.
    """
    candidates = index.candidates(station)
    if not candidates:
        return None, "no_name_match"
    wanted = frozenset(canonical_route(r) for r in routes)
    survivors = [c for c in candidates if c.routes & wanted] if wanted else list(candidates)
    if not survivors:
        return None, "route_contradiction"
    if len(survivors) == 1:
        return survivors[0], "unique"
    if _spread_m(survivors) > STATION_COMPLEX_RADIUS_M:
        return None, "spread_too_wide"
    return _pick_in_complex(survivors, wanted), "complex"


def place_outages(
    outages: Sequence[ElevatorOutage], index: StationIndex | None
) -> tuple[list[ElevatorOutage], PlacementStats]:
    """Attach coordinates where the match is confident. Never drops an outage."""
    if index is None:
        return list(outages), PlacementStats(no_name_match=len(outages))
    buckets: dict[str, int] = {}
    placed: list[ElevatorOutage] = []
    out_of_bbox = 0
    for outage in outages:
        candidate, reason = match_station(index, outage.station, outage.routes)
        if candidate is not None and not in_nyc_bbox(candidate.lat, candidate.lon):
            # cannot happen with SubwayStopsAdapter output (it bbox-filters already);
            # belt and braces so a bad join can never put a marker in the ocean
            out_of_bbox += 1
            candidate, reason = None, "out_of_bbox"
        buckets[reason] = buckets.get(reason, 0) + 1
        placed.append(
            outage
            if candidate is None
            else outage.model_copy(update={"lat": candidate.lat, "lon": candidate.lon})
        )
    stats = PlacementStats(
        placed_unique=buckets.get("unique", 0),
        placed_complex=buckets.get("complex", 0),
        no_name_match=buckets.get("no_name_match", 0),
        route_contradiction=buckets.get("route_contradiction", 0),
        spread_too_wide=buckets.get("spread_too_wide", 0),
        out_of_bbox=out_of_bbox,
    )
    return placed, stats


# ---------------------------------------------------------------------------
# Station index cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CachedIndex:
    source_url: str
    expires_at: datetime
    index: StationIndex


class _StationIndexCache:
    """One built index, held until the stops snapshot behind it goes stale.

    Keyed by the static-GTFS URL so an env override (``NYC_LIVE_MTA_STATIC_GTFS_URL``)
    can never be served a stale index built from the previous bundle.
    """

    def __init__(self) -> None:
        self._entry: _CachedIndex | None = None

    def get(self, source_url: str) -> StationIndex | None:
        entry = self._entry
        if entry is None or entry.source_url != source_url or now_utc() >= entry.expires_at:
            return None
        return entry.index

    def put(self, source_url: str, index: StationIndex, expires_at: datetime) -> None:
        self._entry = _CachedIndex(source_url=source_url, expires_at=expires_at, index=index)

    def reset(self) -> None:
        self._entry = None


_STATION_INDEX = _StationIndexCache()


def reset_station_index() -> None:
    """Forget the cached station index. Tests only."""
    _STATION_INDEX.reset()


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ElevatorOutagesAdapter:
    """MTA elevator/escalator outages, joined to static GTFS stops where confident."""

    name = FeedName.MTA_ELEVATOR_OUTAGES
    ttl: timedelta = DEFAULT_TTL[FeedName.MTA_ELEVATOR_OUTAGES]

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def is_configured(self) -> bool:
        return True

    @property
    def source_url(self) -> str:
        return outages_url(self.settings.mta_gtfs_base)

    async def _station_index(self) -> StationIndex | None:
        """The GTFS parent-station index, or None when the static bundle is unreachable.

        Cached until the stops snapshot it was built from goes stale (24 h), so the
        5 min outage cadence neither re-downloads nor re-parses the 5.6 MB zip.
        """
        stops_adapter = SubwayStopsAdapter(client=self.client, settings=self.settings)
        url = stops_adapter.source_url
        cached = _STATION_INDEX.get(url)
        if cached is not None:
            return cached
        try:
            snapshot = await stops_adapter.fetch()
        except FeedUnavailable as exc:
            log.warning(
                "mta_elevator_outages: static GTFS stops unavailable (%s); every outage "
                "will be served without coordinates",
                exc,
            )
            return None
        index = build_station_index(snapshot.records)
        _STATION_INDEX.put(url, index, snapshot.stale_after)
        log.info(
            "mta_elevator_outages: station index built from %d GTFS parent stations "
            "(%d distinct normalised names)",
            index.stop_count,
            len(index.by_name),
        )
        return index

    async def fetch(self) -> Snapshot[ElevatorOutage]:
        started = time.perf_counter()
        url = self.source_url
        raw = await raw_cache().get(
            self.client, url, feed=self.name, ttl=self.ttl, retries=self.settings.http_retries
        )
        parsed = parse_outages(raw.body, url=url)
        if parsed.dropped_missing_id:
            log.warning(
                "mta_elevator_outages: dropped %d row(s) with no equipment id or station",
                parsed.dropped_missing_id,
            )
        if parsed.bad_timestamps:
            log.warning(
                "mta_elevator_outages: %d unparseable timestamp(s) left as None",
                parsed.bad_timestamps,
            )
        if not parsed.outages:
            log.warning(
                "mta_elevator_outages: upstream returned an empty outage list; reporting "
                "zero outages rather than inventing any (MTA normally publishes 100+)"
            )
        records, stats = place_outages(parsed.outages, await self._station_index())
        log.info(
            "mta_elevator_outages: %d outages (%d out now, %d upcoming); placed %d/%d "
            "(%d unique, %d within a complex); unplaced: %d unknown station, "
            "%d route contradiction, %d ambiguous beyond %.0f m",
            len(records),
            sum(not o.is_upcoming for o in records),
            sum(o.is_upcoming for o in records),
            stats.placed,
            len(records),
            stats.placed_unique,
            stats.placed_complex,
            stats.no_name_match,
            stats.route_contradiction,
            stats.spread_too_wide,
            STATION_COMPLEX_RADIUS_M,
        )
        return Snapshot[ElevatorOutage](
            feed=self.name,
            fetched_at=raw.fetched_at,
            stale_after=raw.fetched_at + self.ttl,
            source_url=url,
            records=records,
            upstream_generated_at=raw.last_modified,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
