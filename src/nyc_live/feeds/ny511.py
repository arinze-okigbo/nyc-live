"""511NY feeds: the live statewide *events* adapter and the key-gated cameras stub.

``NY511EventsAdapter`` (``GET https://511ny.org/api/getevents?format=json``) is
the live one -- incidents, closures, roadwork, special events and transit
operations. It is **not** key-gated; see the class docstring for why.

``Ny511CamerasAdapter`` is still a stub:

* ``is_configured()`` is False when ``NY511_API_KEY`` is empty and ``fetch()``
  raises ``FeedNotConfigured`` so the registry and ``just smoke`` skip it cleanly.
* When a key is present, ``fetch()`` calls the documented
  ``GET https://511ny.org/api/getcameras?key=...&format=json`` endpoint and maps
  the statewide list into ``Camera`` records, keeping only the NYC bbox.

FIELD MAPPING ASSUMPTIONS (unverified live; confirm when a key is available)
-----------------------------------------------------------------------------
    Camera.id        <- "ID"        | "id"
    Camera.name      <- "Name"      | "name"
    Camera.lat       <- "Latitude"  | "latitude" | "lat"
    Camera.lon       <- "Longitude" | "longitude" | "lon"
    Camera.image_url <- "Url"       | "url" | "ImageUrl" | "imageUrl"
    Camera.is_online <- not Disabled and not Blocked   ("Disabled"/"Blocked", bool or string)
    Camera.roadway   <- "RoadwayName" | "roadwayName" | "roadway"
    Camera.direction <- "DirectionOfTravel" | "directionOfTravel" | "direction"
    Camera.area      <- "County" | "county" | "Region" | "region"

The key is sent as a query parameter (that is how the 511NY API works) and is
never included in error messages or logged URLs.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import ValidationError

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    Camera,
    CameraSource,
    ErrorKind,
    FeedName,
    FeedNotConfigured,
    FeedUnavailable,
    NY511Event,
    NY511Severity,
    Snapshot,
    now_utc,
)
from nyc_live.feeds.cameras import Clock, extract_rows, first_present, to_bool, to_float, to_str
from nyc_live.feeds.socrata import NYC_TZ  # the repo's one America/New_York zone
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

NY511_ENV_VAR = "NY511_API_KEY"
NY511_CAMERAS_URL = "https://511ny.org/api/getcameras"
# The events URL is NOT a constant here: it lives in Settings.ny511_events_url
# (NYC_LIVE_NY511_EVENTS_URL) so it is env-overridable like every other upstream.

# ---------------------------------------------------------------------------
# 511NY cameras (key-gated stub)
# ---------------------------------------------------------------------------

_ID_KEYS = ("ID", "id")
_NAME_KEYS = ("Name", "name")
_LAT_KEYS = ("Latitude", "latitude", "lat")
_LON_KEYS = ("Longitude", "longitude", "lon")
_IMAGE_KEYS = ("Url", "url", "ImageUrl", "imageUrl")
_DISABLED_KEYS = ("Disabled", "disabled")
_BLOCKED_KEYS = ("Blocked", "blocked")
_ROADWAY_KEYS = ("RoadwayName", "roadwayName", "roadway")
_DIRECTION_KEYS = ("DirectionOfTravel", "directionOfTravel", "direction")
_AREA_KEYS = ("County", "county", "Region", "region")


def map_ny511_camera(row: Mapping[str, Any]) -> Camera | None:
    cam_id = to_str(first_present(row, _ID_KEYS))
    name = to_str(first_present(row, _NAME_KEYS))
    lat = to_float(first_present(row, _LAT_KEYS))
    lon = to_float(first_present(row, _LON_KEYS))
    image_url = to_str(first_present(row, _IMAGE_KEYS))
    if cam_id is None or name is None or lat is None or lon is None or image_url is None:
        return None
    disabled = to_bool(first_present(row, _DISABLED_KEYS)) or False
    blocked = to_bool(first_present(row, _BLOCKED_KEYS)) or False
    return Camera(
        id=cam_id,
        source=CameraSource.NY511,
        name=name,
        is_online=not (disabled or blocked),
        image_url=image_url,
        lat=lat,
        lon=lon,
        area=to_str(first_present(row, _AREA_KEYS)),
        roadway=to_str(first_present(row, _ROADWAY_KEYS)),
        direction=to_str(first_present(row, _DIRECTION_KEYS)),
    )


def parse_ny511_camera_list(rows: Sequence[Any]) -> tuple[list[Camera], int, int]:
    """Returns (kept, skipped_unmappable, dropped_outside_bbox)."""
    kept: list[Camera] = []
    seen: set[str] = set()
    skipped = 0
    dropped = 0
    for row in rows:
        cam: Camera | None = None
        if isinstance(row, Mapping):
            try:
                cam = map_ny511_camera(row)
            except ValidationError:
                cam = None
        if cam is None:
            skipped += 1
            continue
        if not in_nyc_bbox(cam.lat, cam.lon):
            dropped += 1
            continue
        if cam.id in seen:
            continue
        seen.add(cam.id)
        kept.append(cam)
    return kept, skipped, dropped


class Ny511CamerasAdapter:
    """FeedAdapter[Camera] for 511NY, gated on NY511_API_KEY."""

    name: FeedName = FeedName.NY511_CAMERAS
    ttl = DEFAULT_TTL[FeedName.NY511_CAMERAS]
    env_var = NY511_ENV_VAR

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        *,
        base_url: str = NY511_CAMERAS_URL,
        clock: Clock | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.list_url = base_url
        self._now: Clock = clock or now_utc
        self._last: Snapshot[Camera] | None = None

    def is_configured(self) -> bool:
        return bool((self.settings.ny511_api_key or "").strip())

    async def fetch(self) -> Snapshot[Camera]:
        if not self.is_configured():
            raise FeedNotConfigured(self.name, self.env_var)
        if self._last is not None and self._now() < self._last.stale_after:
            return self._last
        key = (self.settings.ny511_api_key or "").strip()
        started = time.perf_counter()
        resp = await get_with_retry(
            self.client,
            self.list_url,
            feed=self.name,
            retries=self.settings.http_retries,
            params={"key": key, "format": "json"},
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise FeedUnavailable(
                self.name,
                f"camera list is not valid JSON: {exc}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=self.list_url,
                upstream_status=resp.status_code,
            ) from exc
        rows = extract_rows(payload, feed=self.name, url=self.list_url)
        cameras, skipped, dropped = parse_ny511_camera_list(rows)
        if dropped:
            log.info(
                "%s: dropped %d of %d cameras outside NYC bbox", self.name.value, dropped, len(rows)
            )
        if skipped:
            log.warning("%s: skipped %d of %d unmappable rows", self.name.value, skipped, len(rows))
        if not cameras:
            raise FeedUnavailable(
                self.name,
                f"no NYC cameras mapped from {len(rows)} rows "
                f"(skipped={skipped}, outside_bbox={dropped})",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=self.list_url,
                upstream_status=resp.status_code,
            )
        fetched_at = self._now()
        snap = Snapshot[Camera](
            feed=self.name,
            fetched_at=fetched_at,
            stale_after=fetched_at + self.ttl,
            source_url=self.list_url,
            records=cameras,
            latency_ms=latency_ms,
        )
        self._last = snap
        return snap


# ---------------------------------------------------------------------------
# 511NY events (live, keyless)
# ---------------------------------------------------------------------------

FEED_EVENTS = FeedName.NY511_EVENTS.value

NY511_TIMESTAMP_FORMAT = "%d/%m/%Y %H:%M:%S"
"""511NY renders every timestamp as ``DD/MM/YYYY HH:MM:SS`` -- day first, **not**
the American ``MM/DD``. Verified against the live feed on 2026-09-09 three ways:

1. across 2,420 events the first component ranges 01..31 while the second never
   exceeds 12;
2. the newest ``LastUpdated`` parses as 2026-09-09 15:50:08, ten minutes before
   the wall clock in New York, and as nonsense under ``MM/DD``;
3. rows spell the same date out in prose, e.g. ``StartDate "27/07/2026
   00:00:00"`` alongside ``Description "... Continuous Monday July 27th, 2026
   12:00 AM thru Thursday October 22nd, 2026 11:59 PM"`` with
   ``PlannedEndDate "22/10/2026 23:59:00"``.

Point 2 also fixes the zone: the values track New York local time (15:50 local /
19:50 UTC), and the format itself carries no offset, so each one is localised to
``NYC_TZ`` and converted to UTC. Getting this wrong is silent -- roughly a third
of all dates are valid under both readings -- so it is asserted in the tests."""

NY511_NO_DATA = "no data"
"""511NY's own sentinel for an unpopulated field (2,213 of 2,420 ``LanesAffected``
values). It means "not reported", so it is normalised to None rather than shown
to a user as if it were a lane status."""

_SEVERITY_ALIASES: dict[str, NY511Severity] = {
    # 511NY's documented set, plus the two values it uses for "not assessed".
    # Observed live 2026-09-09: Unknown 2197, Minor 186, Major 27, Moderate 9, None 1.
    "unknown": NY511Severity.UNKNOWN,
    "none": NY511Severity.UNKNOWN,
    "": NY511Severity.UNKNOWN,
    "minor": NY511Severity.MINOR,
    "moderate": NY511Severity.MODERATE,
    "major": NY511Severity.MAJOR,
}


POLYLINE_PRECISION = 5
"""511NY encodes ``MapEncodedPolyline`` with Google's algorithm at precision 5 (1e5),
not 6. Measured, not assumed: decoding all 28 polylines in the 2026-09-09 payload at
1e5 puts the first vertex a median 2e-6 deg (~0.2 m) from the event's own
``Latitude``/``Longitude``, worst case 0.0106 deg (~1.2 km, an event whose segment
legitimately starts up the road). At 1e6 the same decode lands ~39 deg away -- off
the planet's worth of wrong, and silently plottable, which is why this is pinned by
a test that decodes a real polyline and compares it to its own event's coordinates."""

POLYLINE_MAX_ORIGIN_OFFSET_DEG = 0.5
"""Sanity valve: a decoded line whose first vertex is further than this from the
event's own point is not that event's geometry. 47x the worst real offset observed
above, and far below the ~39 deg a precision mix-up produces, so it rejects a broken
decode without ever rejecting real data. A line that fails it is dropped to empty
``points``; the event itself survives and renders as a pin."""


def decode_polyline(
    encoded: str, *, precision: int = POLYLINE_PRECISION
) -> list[tuple[float, float]]:
    """Google encoded-polyline -> ordered ``(lat, lon)`` vertices. Raises ValueError if malformed.

    Deliberately not a dependency: the algorithm is a dozen lines and adding a
    package for it would need orchestrator sign-off for no benefit.
    """
    factor = float(10**precision)
    points: list[tuple[float, float]] = []
    index = 0
    lat = 0
    lon = 0
    length = len(encoded)
    while index < length:
        deltas: list[int] = []
        for _ in range(2):
            shift = 0
            result = 0
            while True:
                if index >= length:
                    raise ValueError("polyline ended mid-value")
                byte = ord(encoded[index]) - 63
                if byte < 0:
                    raise ValueError(
                        f"polyline has a character below the encoding range at {index}"
                    )
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
                if shift > 30:
                    raise ValueError(f"polyline value at {index} does not terminate")
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        points.append((lat / factor, lon / factor))
    return points


def event_points(
    raw: object, *, lat: float, lon: float, event_id: str
) -> list[tuple[float, float]]:
    """``MapEncodedPolyline`` -> the affected stretch, or ``[]`` when there isn't one.

    Never raises: a null, blank, non-string, undecodable or implausibly-far
    polyline costs the event its line, not its existence -- the consumer falls
    back to the point, exactly as the contract's ``points`` docstring says.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        points = decode_polyline(raw)
    except ValueError as exc:
        log.warning("%s: event %s has an undecodable polyline: %s", FEED_EVENTS, event_id, exc)
        return []
    if not points:
        return []
    first_lat, first_lon = points[0]
    offset = max(abs(first_lat - lat), abs(first_lon - lon))
    if offset > POLYLINE_MAX_ORIGIN_OFFSET_DEG:
        log.warning(
            "%s: event %s polyline starts %.3f deg from its own point; dropping the line",
            FEED_EVENTS,
            event_id,
            offset,
        )
        return []
    return points


def extract_event_rows(payload: Any, *, url: str) -> list[Any]:
    """``getevents`` returns a bare JSON array. Anything else is a parse error."""
    if isinstance(payload, list):
        return payload
    raise FeedUnavailable(
        FeedName.NY511_EVENTS,
        f"expected a JSON array of events, got {type(payload).__name__}",
        kind=ErrorKind.UPSTREAM_PARSE,
        url=url,
    )


def parse_ny511_timestamp(raw: object) -> datetime | None:
    """``"22/10/2026 23:59:00"`` (New York local) -> an aware UTC datetime.

    None / empty string mean *absent* -- 323 of 2,420 events ship
    ``PlannedEndDate: ""`` because they have no planned end -- and return None,
    never the epoch. Anything else that will not parse raises ValueError so the
    caller can drop and count that one row instead of inventing a time.
    """
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError(f"expected a 511NY timestamp string, got {type(raw).__name__}")
    text = raw.strip()
    if not text:
        return None
    naive = datetime.strptime(text, NY511_TIMESTAMP_FORMAT)
    return naive.replace(tzinfo=NYC_TZ).astimezone(UTC)


def unrecognized_severity(raw: object) -> str | None:
    """The upstream severity rendered as-is when it is outside 511NY's vocabulary, else None.

    Exists so the adapter can report *one* aggregated warning per fetch instead of
    one per row: this is not hypothetical. On 2026-09-09 the statewide feed grew a
    third publisher, NITTEC (Niagara/Buffalo), emitting a numeric ``Severity: "1"``
    on its own undocumented scale alongside TRANSCOM's and TRAVELIQ's words. One
    row today, but a per-row warning would become log spam the moment that source
    scales up.
    """
    if raw is None:
        return None
    return None if str(raw).strip().lower() in _SEVERITY_ALIASES else str(raw)


def parse_ny511_severity(raw: object) -> NY511Severity:
    """Case-insensitive map onto the contract enum; anything unrecognised -> UNKNOWN.

    Deliberately lenient, following ``weather._alert_severity``: one surprising
    string in one event must not take down the whole incident layer -- and that has
    already earned its keep (see :func:`unrecognized_severity`). Nothing is guessed:
    NITTEC's ``"1"`` is *not* silently read as "minor", because its scale is
    undocumented and inventing a rank would be fabricating severity data. It becomes
    an honest UNKNOWN, and the adapter logs the vocabulary drift so it is visible.
    """
    if raw is None:
        return NY511Severity.UNKNOWN
    severity = _SEVERITY_ALIASES.get(str(raw).strip().lower())
    if severity is None:
        log.debug("%s: unrecognized severity %r; recording as unknown", FEED_EVENTS, raw)
        return NY511Severity.UNKNOWN
    return severity


def _optional_field(raw: object, *, sentinel: str | None = None) -> str | None:
    """Trimmed string, or None for null / blank / 511NY's ``No Data`` sentinel."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or (sentinel is not None and text.lower() == sentinel):
        return None
    return text


def map_ny511_event(row: Mapping[str, Any]) -> NY511Event:
    """Map one upstream row onto ``NY511Event``. Raises ValueError on a row that cannot be.

    ``MapEncodedPolyline`` becomes ``points`` (see :func:`event_points`). ``Schedule``,
    ``NavteqLinkId``, ``LcsEntries`` and the ``*Location`` / ``*City`` fields are
    intentionally not carried: the frozen contract has no home for them and
    ``extra="forbid"`` would reject them.

    ``StartDate`` (not ``Reported``) fills ``started_at``. Nothing is lost by that
    choice: the two were byte-identical on all 2,420 events of the 2026-09-09
    payload, so ``Reported`` carries no independent information today.
    """
    event_id = _optional_field(row.get("ID"))
    if event_id is None:
        raise ValueError("event has no ID")
    event_type = _optional_field(row.get("EventType"))
    if event_type is None:
        raise ValueError(f"event {event_id!r} has no EventType")
    description = _optional_field(row.get("Description"))
    if description is None:
        raise ValueError(f"event {event_id!r} has no Description")
    lat = to_float(row.get("Latitude"))
    lon = to_float(row.get("Longitude"))
    if lat is None or lon is None:
        raise ValueError(f"event {event_id!r} has no usable coordinates")
    return NY511Event(
        lat=lat,
        lon=lon,
        id=event_id,
        event_type=event_type,
        event_subtype=_optional_field(row.get("EventSubType")),
        severity=parse_ny511_severity(row.get("Severity")),
        description=description,
        roadway=_optional_field(row.get("RoadwayName")),
        direction=_optional_field(row.get("DirectionOfTravel")),
        county=_optional_field(row.get("CountyName")),
        lanes_affected=_optional_field(row.get("LanesAffected"), sentinel=NY511_NO_DATA),
        started_at=parse_ny511_timestamp(row.get("StartDate")),
        planned_end=parse_ny511_timestamp(row.get("PlannedEndDate")),
        last_updated=parse_ny511_timestamp(row.get("LastUpdated")),
        points=event_points(row.get("MapEncodedPolyline"), lat=lat, lon=lon, event_id=event_id),
    )


def parse_ny511_event_list(rows: Sequence[Any]) -> tuple[list[NY511Event], int, int]:
    """Map, de-duplicate and bbox-filter a statewide event list.

    Returns ``(kept, skipped_unmappable, dropped_outside_bbox)``. ~63% of the
    statewide feed is dropped by the bbox on a normal day (1,529 of 2,420 on
    2026-09-09); the adapter logs both counts.
    """
    kept: list[NY511Event] = []
    seen: set[str] = set()
    odd_severities: Counter[str] = Counter()
    skipped = 0
    dropped = 0
    for row in rows:
        if not isinstance(row, Mapping):
            skipped += 1
            continue
        odd = unrecognized_severity(row.get("Severity"))
        if odd is not None:
            odd_severities[odd] += 1
        try:
            event = map_ny511_event(row)
        except (ValidationError, ValueError, TypeError) as exc:
            skipped += 1
            if skipped <= 3:
                log.warning("%s: skipping unmappable event: %s", FEED_EVENTS, exc)
            continue
        if not in_nyc_bbox(event.lat, event.lon):
            dropped += 1
            continue
        if event.id in seen:
            continue
        seen.add(event.id)
        kept.append(event)
    if odd_severities:
        log.warning(
            "%s: %d event(s) carried a severity outside 511NY's vocabulary, recorded as "
            "unknown: %s",
            FEED_EVENTS,
            sum(odd_severities.values()),
            dict(odd_severities.most_common(5)),
        )
    return kept, skipped, dropped


class NY511EventsAdapter:
    """FeedAdapter[NY511Event] over 511NY's statewide event feed, filtered to NYC.

    NOT key-gated, on purpose. ``getevents`` answers ``format=json`` requests
    with the full 2,420-event payload and no key at all, and it ignores the
    ``key`` parameter entirely -- a deliberately invalid key returns the same
    byte-identical 200 (verified 2026-09-09). Gating this feed on
    ``NY511_API_KEY`` would therefore switch off a working live incident layer
    for every deployment that has no key, which is all of them today, and
    ``FeedNotConfigured`` is exactly the wrong signal for an endpoint that is
    serving data. So ``is_configured()`` is always True and the key is sent
    *only* when one happens to be set, which costs nothing now and is already
    correct if 511NY starts enforcing keys here as it does on ``getcameras``.

    If it ever does start enforcing, the failure is loud rather than an empty
    layer: a 401/403 raises ``FeedUnavailable`` with ``ErrorKind.UPSTREAM_HTTP``
    and a message naming ``NY511_API_KEY``, so the feed goes to
    ``status="error"``. It is not reported as ``NOT_CONFIGURED``, which
    ``smoke``/``FeedHealth`` treat as an intentional, benign skip.
    """

    name: FeedName = FeedName.NY511_EVENTS
    ttl = DEFAULT_TTL[FeedName.NY511_EVENTS]
    env_var = NY511_ENV_VAR

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        *,
        base_url: str | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        # Env-overridable like every other upstream (NYC_LIVE_NY511_EVENTS_URL);
        # `base_url` is the explicit per-instance override tests use.
        self.source_url = base_url or settings.ny511_events_url
        self._limiter = RateLimiter(self.ttl)

    def is_configured(self) -> bool:
        return True

    def request_params(self) -> dict[str, str]:
        """``format=json`` is mandatory (a bare ``getevents`` 404s). The key rides along
        only when configured, and never appears in ``source_url`` or in an error."""
        params = {"format": "json"}
        key = (self.settings.ny511_api_key or "").strip()
        if key:
            params["key"] = key
        return params

    async def fetch(self) -> Snapshot[NY511Event]:
        await self._limiter.wait(self.name.value)
        try:
            return await self._fetch_once()
        except BaseException:
            # A failed attempt must not hold the 2-minute cadence floor against
            # the next try inside the caller's refresh lock.
            self._limiter.forget(self.name.value)
            raise

    async def _fetch_once(self) -> Snapshot[NY511Event]:
        started = time.perf_counter()
        fetched_at = now_utc()
        resp = await self._get()
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise FeedUnavailable(
                self.name,
                f"event list is not valid JSON: {exc}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=self.source_url,
                upstream_status=resp.status_code,
            ) from exc
        rows = extract_event_rows(payload, url=self.source_url)
        events, skipped, dropped = parse_ny511_event_list(rows)
        if dropped:
            log.info(
                "%s: dropped %d of %d events outside the NYC bbox",
                self.name.value,
                dropped,
                len(rows),
            )
        if skipped:
            log.warning(
                "%s: skipped %d of %d unmappable events", self.name.value, skipped, len(rows)
            )
        if not events:
            # ~900 of the statewide events are in NYC at any hour, so zero means the
            # payload or the mapping changed shape, not that the city went quiet.
            # Fail loud; never serve an empty snapshot as if it were the truth.
            raise FeedUnavailable(
                self.name,
                f"no NYC events mapped from {len(rows)} statewide rows "
                f"(skipped={skipped}, outside_bbox={dropped})",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=self.source_url,
                upstream_status=resp.status_code,
            )
        return Snapshot[NY511Event](
            feed=self.name,
            fetched_at=fetched_at,
            stale_after=fetched_at + self.ttl,
            source_url=self.source_url,
            records=events,
            latency_ms=latency_ms,
        )

    async def _get(self) -> httpx.Response:
        try:
            return await get_with_retry(
                self.client,
                self.source_url,
                feed=self.name,
                retries=self.settings.http_retries,
                params=self.request_params(),
            )
        except FeedUnavailable as exc:
            if exc.upstream_status not in (401, 403):
                raise
            configured = bool((self.settings.ny511_api_key or "").strip())
            state = "the configured key was rejected" if configured else "no key was sent"
            raise FeedUnavailable(
                self.name,
                f"HTTP {exc.upstream_status} from 511NY getevents: {state}. This endpoint "
                f"served the full feed keylessly as of 2026-09-09; if that has changed, set "
                f"{self.env_var} to a valid 511NY key.",
                kind=ErrorKind.UPSTREAM_HTTP,
                url=self.source_url,
                upstream_status=exc.upstream_status,
            ) from exc


__all__ = [
    "FEED_EVENTS",
    "NY511_CAMERAS_URL",
    "NY511_ENV_VAR",
    "NY511_NO_DATA",
    "NY511_TIMESTAMP_FORMAT",
    "POLYLINE_MAX_ORIGIN_OFFSET_DEG",
    "POLYLINE_PRECISION",
    "NY511EventsAdapter",
    "Ny511CamerasAdapter",
    "decode_polyline",
    "event_points",
    "extract_event_rows",
    "map_ny511_camera",
    "map_ny511_event",
    "parse_ny511_camera_list",
    "parse_ny511_event_list",
    "parse_ny511_severity",
    "parse_ny511_timestamp",
    "unrecognized_severity",
]
