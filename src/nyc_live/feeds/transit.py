"""MTA subway adapters: GTFS-realtime trips, GTFS-realtime alerts, static GTFS stops.

Upstreams (no API key required, per api.mta.info: "Accounts and API keys are no
longer required to access these feeds")
-------------------------------------------------------------------------------
* Trips (trip updates + vehicle positions), one protobuf feed per line group:
  ``{settings.mta_gtfs_base}/nyct%2F{slug}`` for every slug in ``SUBWAY_FEED_SLUGS``.
  The ``%2F`` is part of the path and is sent verbatim (one URL-encoded path segment,
  not ``nyct/{slug}``) — api.mta.info's own copy-paste feed URLs use this form, and an
  unescaped slash there gets API Gateway's misleading ``403 Missing Authentication
  Token`` (its generic response for an unmatched route, not an auth failure).
  Slugs are verified at runtime: a 404 on any slug raises ``FeedUnavailable`` with
  ``ErrorKind.NOT_FOUND`` naming the slug. A failed slug never yields an empty snapshot.
* Alerts: ``{settings.mta_gtfs_base}/camsys%2Fsubway-alerts`` (different prefix; same
  verbatim ``%2F`` convention as trips).
* Static GTFS (stops): ``settings.mta_static_gtfs_url``, which defaults to
  ``STATIC_GTFS_URL`` = ``https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip``
  (verified 2026-09-08: HTTP 200, ``application/zip``, ~5.6 MB, ``stops.txt`` has ~1490
  rows with ``stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station``).
  Like every other upstream here the URL is read from ``Settings`` at fetch time, so
  ``NYC_LIVE_MTA_STATIC_GTFS_URL`` can redirect or kill this feed.
  The legacy ``web.mta.info/developers/data/nyct/subway/google_transit.zip`` returns 403
  from MTA and must not be used. If the S3 zip is unreachable or is not a zip containing
  ``stops.txt`` the adapter raises; it never falls back to a bundled copy.

Shared raw-bytes cache
----------------------
Every adapter in this module fetches through one module-level ``RawBytesCache`` keyed by
URL. Within a URL's TTL the cached bytes are served and the upstream is not touched, so
``SubwayTripsAdapter`` and ``SubwayAlertsAdapter`` never double-fetch inside a TTL
window, concurrent callers are single-flighted per URL, and a per-URL ``RateLimiter``
(interval = TTL) is the belt to the cache's braces. ``reset_raw_cache()`` exists for tests.

Parsing notes
-------------
* Direction: derived from the NYCT trip_id suffix (``..N`` / ``..S``, single-dot for
  shuttles). If the trip_id carries none, the last character of the vehicle's stop_id or
  the first stop_time_update's stop_id (NYCT stop ids end in ``N``/``S``) is used. The
  NYCT ``nyct_trip_descriptor`` protobuf extension also carries direction but its compiled
  bindings are not a project dependency, so it is not read.
* Vehicle positions in NYCT feeds have no lat/lon; join ``current_stop_id`` to
  ``SubwayStopsAdapter`` records to place trains.
* ``SubwayAlert.updated_at`` is left ``None``: MTA publishes it only in the Mercury
  protobuf extension, which is not compiled into ``gtfs-realtime-bindings``.
* ``SubwayStop.routes`` is derived from ``trips.txt`` (trip -> route) joined with
  ``stop_times.txt`` (trip -> stop), then unioned up to the parent station. Measured cost
  on the 2026-08-27 zip: 0.03 s + 0.50 s, run once per 24 h TTL in a worker thread.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    Snapshot,
    StopTimeUpdate,
    SubwayAlert,
    SubwayDirection,
    SubwayStop,
    SubwayTrip,
    VehiclePosition,
    VehicleStatus,
    now_utc,
)
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

SUBWAY_FEED_SLUGS: tuple[str, ...] = (
    "gtfs",  # 1 2 3 4 5 6 7 S (42 St shuttle)
    "gtfs-ace",
    "gtfs-bdfm",
    "gtfs-g",
    "gtfs-jz",
    "gtfs-nqrw",
    "gtfs-l",
    "gtfs-si",
)
ALERTS_SLUG = "camsys%2Fsubway-alerts"
STATIC_GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip"
"""Documented default only; it is the default of ``Settings.mta_static_gtfs_url``.
Adapters resolve the URL from settings at fetch time, never from this constant."""

_DIRECTION_RE = re.compile(r"\.{1,2}([NS])(?=[0-9A-Z]|$)")
_STOPS_REQUIRED_COLUMNS = ("stop_id", "stop_name", "stop_lat", "stop_lon")


def subway_feed_url(base: str, slug: str) -> str:
    return f"{base.rstrip('/')}/nyct%2F{slug}"


def alerts_url(base: str) -> str:
    return f"{base.rstrip('/')}/{ALERTS_SLUG}"


# ---------------------------------------------------------------------------
# Shared raw-bytes cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawFetch:
    url: str
    body: bytes
    fetched_at: datetime
    expires_at: datetime
    last_modified: datetime | None
    latency_ms: float
    from_cache: bool


class RawBytesCache:
    """Per-URL TTL cache of upstream bytes with single-flight refresh and rate limiting."""

    def __init__(self) -> None:
        self._entries: dict[str, RawFetch] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._limiters: dict[float, RateLimiter] = {}

    def _lock(self, url: str) -> asyncio.Lock:
        lock = self._locks.get(url)
        if lock is None:
            lock = self._locks[url] = asyncio.Lock()
        return lock

    def _limiter(self, ttl: timedelta) -> RateLimiter:
        key = ttl.total_seconds()
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = self._limiters[key] = RateLimiter(ttl)
        return limiter

    def peek(self, url: str) -> RawFetch | None:
        entry = self._entries.get(url)
        if entry is None or now_utc() >= entry.expires_at:
            return None
        return entry

    async def get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        feed: FeedName,
        ttl: timedelta,
        retries: int | None = None,
    ) -> RawFetch:
        async with self._lock(url):
            cached = self.peek(url)
            if cached is not None:
                return replace(cached, from_cache=True)
            limiter = self._limiter(ttl)
            await limiter.wait(url)
            started = time.perf_counter()
            try:
                resp = await get_with_retry(client, url, feed=feed, retries=retries)
            except FeedUnavailable:
                # a failed attempt must not hold the cadence floor against the next try
                limiter.forget(url)
                raise
            fetched_at = now_utc()
            entry = RawFetch(
                url=url,
                body=resp.content,
                fetched_at=fetched_at,
                expires_at=fetched_at + ttl,
                last_modified=_parse_http_date(resp.headers.get("Last-Modified")),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                from_cache=False,
            )
            self._entries[url] = entry
            return entry

    def reset(self) -> None:
        self._entries.clear()
        self._locks.clear()
        self._limiters.clear()


_RAW_CACHE = RawBytesCache()


def raw_cache() -> RawBytesCache:
    return _RAW_CACHE


def reset_raw_cache() -> None:
    """Forget every cached body and rate-limit stamp. Tests only."""
    _RAW_CACHE.reset()


def _parse_http_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# GTFS-realtime parsing helpers (pure functions, unit-testable)
# ---------------------------------------------------------------------------


def parse_feed_message(
    body: bytes, *, feed: FeedName, url: str, slug: str
) -> gtfs_realtime_pb2.FeedMessage:
    """Decode a FeedMessage or raise UPSTREAM_PARSE naming the slug."""
    msg = gtfs_realtime_pb2.FeedMessage()
    try:
        msg.ParseFromString(body)
    except DecodeError as exc:
        raise FeedUnavailable(
            feed,
            f"slug {slug!r}: body ({len(body)} bytes) is not a GTFS-realtime FeedMessage: {exc}",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
        ) from exc
    if not msg.HasField("header") or not msg.header.gtfs_realtime_version:
        raise FeedUnavailable(
            feed,
            f"slug {slug!r}: decoded message has no FeedHeader; body is not GTFS-realtime",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
        )
    return msg


def _epoch(seconds: int) -> datetime | None:
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


def direction_from_trip_id(trip_id: str) -> SubwayDirection | None:
    m = _DIRECTION_RE.search(trip_id)
    if m is None:
        return None
    return "N" if m.group(1) == "N" else "S"


def _direction_from_stop_id(stop_id: str | None) -> SubwayDirection | None:
    if not stop_id:
        return None
    last = stop_id[-1]
    if last == "N":
        return "N"
    if last == "S":
        return "S"
    return None


def _stop_time_updates(tu: gtfs_realtime_pb2.TripUpdate) -> list[StopTimeUpdate]:
    out: list[StopTimeUpdate] = []
    for stu in tu.stop_time_update:
        if not stu.stop_id:
            continue
        arrival = _epoch(stu.arrival.time) if stu.HasField("arrival") else None
        departure = _epoch(stu.departure.time) if stu.HasField("departure") else None
        out.append(StopTimeUpdate(stop_id=stu.stop_id, arrival=arrival, departure=departure))
    return out


def _vehicle(vp: gtfs_realtime_pb2.VehiclePosition) -> VehiclePosition:
    status: VehicleStatus | None = None
    if vp.HasField("current_status"):
        status = VehicleStatus(
            gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus.Name(vp.current_status)
        )
    return VehiclePosition(
        current_stop_id=vp.stop_id or None,
        status=status,
        timestamp=_epoch(vp.timestamp) if vp.HasField("timestamp") else None,
        current_stop_sequence=(
            vp.current_stop_sequence if vp.HasField("current_stop_sequence") else None
        ),
    )


def parse_trips(msg: gtfs_realtime_pb2.FeedMessage, slug: str) -> tuple[list[SubwayTrip], int]:
    """Merge trip_update and vehicle entities by trip_id. Returns (trips, dropped_count)."""
    order: list[str] = []
    updates: dict[str, gtfs_realtime_pb2.TripUpdate] = {}
    vehicles: dict[str, gtfs_realtime_pb2.VehiclePosition] = {}
    for ent in msg.entity:
        if ent.HasField("trip_update"):
            tid = ent.trip_update.trip.trip_id
            if tid not in updates and tid not in vehicles:
                order.append(tid)
            updates[tid] = ent.trip_update
        if ent.HasField("vehicle"):
            tid = ent.vehicle.trip.trip_id
            if tid not in updates and tid not in vehicles:
                order.append(tid)
            vehicles[tid] = ent.vehicle

    trips: list[SubwayTrip] = []
    dropped = 0
    for tid in order:
        tu = updates.get(tid)
        vp = vehicles.get(tid)
        descriptors: list[gtfs_realtime_pb2.TripDescriptor] = []
        if tu is not None:
            descriptors.append(tu.trip)
        if vp is not None:
            descriptors.append(vp.trip)
        route_id = next((d.route_id for d in descriptors if d.route_id), "")
        start_date = next((d.start_date for d in descriptors if d.start_date), None)
        if not tid or not route_id:
            dropped += 1
            continue
        stop_times = _stop_time_updates(tu) if tu else []
        vehicle = _vehicle(vp) if vp else None
        direction = direction_from_trip_id(tid)
        if direction is None:
            direction = _direction_from_stop_id(vehicle.current_stop_id if vehicle else None)
        if direction is None and stop_times:
            direction = _direction_from_stop_id(stop_times[0].stop_id)
        trips.append(
            SubwayTrip(
                trip_id=tid,
                route_id=route_id,
                feed_slug=slug,
                direction=direction,
                start_date=start_date,
                stop_times=stop_times,
                vehicle=vehicle,
            )
        )
    return trips, dropped


def _translated(ts: gtfs_realtime_pb2.TranslatedString) -> str | None:
    if not ts.translation:
        return None
    for lang in ("en", "en-US", ""):
        for tr in ts.translation:
            if tr.language == lang and tr.text:
                return tr.text
    for tr in ts.translation:
        if tr.text and not tr.language.endswith("html"):
            return tr.text
    return ts.translation[0].text or None


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def parse_alerts(msg: gtfs_realtime_pb2.FeedMessage) -> tuple[list[SubwayAlert], int]:
    """Map alert entities to SubwayAlert. Returns (alerts, dropped_count)."""
    alerts: list[SubwayAlert] = []
    dropped = 0
    for ent in msg.entity:
        if not ent.HasField("alert"):
            continue
        al = ent.alert
        header = _translated(al.header_text)
        if not ent.id or not header:
            dropped += 1
            continue
        starts = [p.start for p in al.active_period if p.HasField("start") and p.start > 0]
        open_ended = any(not p.HasField("end") or p.end == 0 for p in al.active_period)
        ends = [p.end for p in al.active_period if p.HasField("end") and p.end > 0]
        alerts.append(
            SubwayAlert(
                id=ent.id,
                header=header,
                description=_translated(al.description_text),
                active_from=_epoch(min(starts)) if starts else None,
                active_until=None if open_ended or not ends else _epoch(max(ends)),
                routes=_unique(ie.route_id for ie in al.informed_entity),
                stop_ids=_unique(ie.stop_id for ie in al.informed_entity),
                effect=(
                    gtfs_realtime_pb2.Alert.Effect.Name(al.effect)
                    if al.HasField("effect")
                    else None
                ),
                updated_at=None,
            )
        )
    return alerts, dropped


# ---------------------------------------------------------------------------
# Static GTFS parsing (runs in a worker thread)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaticGtfsParse:
    stops: list[SubwayStop]
    dropped_out_of_bbox: int
    dropped_malformed: int
    routes_derived: bool


def _read_csv(zf: zipfile.ZipFile, name: str) -> csv.DictReader[str]:
    return csv.DictReader(io.TextIOWrapper(zf.open(name), encoding="utf-8-sig", newline=""))


def _derive_stop_routes(zf: zipfile.ZipFile) -> dict[str, set[str]]:
    trip_route: dict[str, str] = {}
    for row in _read_csv(zf, "trips.txt"):
        if row.get("trip_id") and row.get("route_id"):
            trip_route[row["trip_id"]] = row["route_id"]
    stop_routes: dict[str, set[str]] = {}
    with zf.open("stop_times.txt") as f:
        reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
        header = next(reader, None)
        if header is None or "trip_id" not in header or "stop_id" not in header:
            return {}
        ti, si = header.index("trip_id"), header.index("stop_id")
        for row in reader:
            route = trip_route.get(row[ti])
            if route:
                stop_routes.setdefault(row[si], set()).add(route)
    return stop_routes


def parse_static_gtfs(body: bytes, *, feed: FeedName, url: str) -> StaticGtfsParse:
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as exc:
        raise FeedUnavailable(
            feed,
            f"body ({len(body)} bytes) is not a zip archive: {exc}",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
        ) from exc
    with zf:
        names = set(zf.namelist())
        if "stops.txt" not in names:
            raise FeedUnavailable(
                feed,
                f"zip has no stops.txt (members: {sorted(names)[:10]})",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=url,
            )
        reader = _read_csv(zf, "stops.txt")
        columns = reader.fieldnames or []
        missing = [c for c in _STOPS_REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise FeedUnavailable(
                feed,
                f"stops.txt is missing columns {missing}; has {list(columns)}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=url,
            )
        rows = list(reader)
        routes_derived = {"trips.txt", "stop_times.txt"} <= names
        stop_routes = _derive_stop_routes(zf) if routes_derived else {}

    # union child (127N/127S) routes up to the parent station (127)
    for row in rows:
        parent = (row.get("parent_station") or "").strip()
        if parent and row["stop_id"] in stop_routes:
            stop_routes.setdefault(parent, set()).update(stop_routes[row["stop_id"]])

    stops: list[SubwayStop] = []
    out_of_bbox = malformed = 0
    for row in rows:
        stop_id = (row.get("stop_id") or "").strip()
        name = (row.get("stop_name") or "").strip()
        try:
            lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
        except (TypeError, ValueError):
            malformed += 1
            continue
        if not stop_id or not name:
            malformed += 1
            continue
        if not in_nyc_bbox(lat, lon):
            out_of_bbox += 1
            continue
        parent = (row.get("parent_station") or "").strip() or None
        stops.append(
            SubwayStop(
                stop_id=stop_id,
                name=name,
                lat=lat,
                lon=lon,
                parent_station=parent,
                routes=sorted(stop_routes.get(stop_id, ())),
            )
        )
    if not stops:
        raise FeedUnavailable(
            feed,
            f"stops.txt yielded no usable rows ({len(rows)} rows, {malformed} malformed, "
            f"{out_of_bbox} outside NYC)",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
        )
    return StaticGtfsParse(stops, out_of_bbox, malformed, routes_derived)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class _BaseAdapter:
    name: FeedName
    ttl: timedelta

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def is_configured(self) -> bool:
        return True

    async def _raw(self, url: str) -> RawFetch:
        return await _RAW_CACHE.get(
            self.client, url, feed=self.name, ttl=self.ttl, retries=self.settings.http_retries
        )


class SubwayTripsAdapter(_BaseAdapter):
    """All NYCT trip updates + vehicle positions, every slug fetched concurrently and merged."""

    name = FeedName.MTA_SUBWAY
    ttl = DEFAULT_TTL[FeedName.MTA_SUBWAY]
    slugs: tuple[str, ...] = SUBWAY_FEED_SLUGS

    def url_for(self, slug: str) -> str:
        return subway_feed_url(self.settings.mta_gtfs_base, slug)

    @property
    def source_url(self) -> str:
        return subway_feed_url(self.settings.mta_gtfs_base, "{" + ",".join(self.slugs) + "}")

    async def _fetch_slug(self, slug: str) -> tuple[RawFetch, list[SubwayTrip], int | None]:
        url = self.url_for(slug)
        try:
            raw = await self._raw(url)
        except FeedUnavailable as exc:
            if exc.kind is ErrorKind.NOT_FOUND:
                raise FeedUnavailable(
                    self.name,
                    f"slug {slug!r} returned 404 at {url}; the NYCT feed slug is wrong or retired",
                    kind=ErrorKind.NOT_FOUND,
                    url=url,
                    upstream_status=404,
                ) from exc
            raise FeedUnavailable(
                self.name,
                f"slug {slug!r}: {exc.message}",
                kind=exc.kind,
                url=url,
                upstream_status=exc.upstream_status,
                retry_after_s=exc.retry_after_s,
            ) from exc
        msg = parse_feed_message(raw.body, feed=self.name, url=url, slug=slug)
        trips, dropped = parse_trips(msg, slug)
        if dropped:
            log.warning(
                "mta_subway slug %s: dropped %d entities without trip/route id", slug, dropped
            )
        generated = msg.header.timestamp if msg.header.HasField("timestamp") else None
        return raw, trips, generated

    async def fetch(self) -> Snapshot[SubwayTrip]:
        started = time.perf_counter()
        results = await asyncio.gather(
            *(self._fetch_slug(s) for s in self.slugs), return_exceptions=True
        )
        raws: list[RawFetch] = []
        records: list[SubwayTrip] = []
        generated: list[int] = []
        for slug, result in zip(self.slugs, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, FeedUnavailable):
                    raise result
                raise FeedUnavailable(
                    self.name,
                    f"slug {slug!r}: {type(result).__name__}: {result}",
                    kind=ErrorKind.INTERNAL,
                    url=self.url_for(slug),
                ) from result
            raw, trips, gen = result
            raws.append(raw)
            records.extend(trips)
            if gen:
                generated.append(gen)
        if not records:
            raise FeedUnavailable(
                self.name,
                f"all {len(self.slugs)} NYCT feeds decoded but contained zero trips; "
                "the subway never has zero trips, treating as an upstream fault",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=self.source_url,
            )
        fetched_at = min(r.fetched_at for r in raws)
        log.info(
            "mta_subway: %d trips from %d slugs (%d served from cache)",
            len(records),
            len(raws),
            sum(r.from_cache for r in raws),
        )
        return Snapshot[SubwayTrip](
            feed=self.name,
            fetched_at=fetched_at,
            stale_after=fetched_at + self.ttl,
            source_url=self.source_url,
            records=records,
            upstream_generated_at=_epoch(min(generated)) if generated else None,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )


class SubwayAlertsAdapter(_BaseAdapter):
    name = FeedName.MTA_SUBWAY_ALERTS
    ttl = DEFAULT_TTL[FeedName.MTA_SUBWAY_ALERTS]

    @property
    def source_url(self) -> str:
        return alerts_url(self.settings.mta_gtfs_base)

    async def fetch(self) -> Snapshot[SubwayAlert]:
        started = time.perf_counter()
        url = self.source_url
        try:
            raw = await self._raw(url)
        except FeedUnavailable as exc:
            if exc.kind is ErrorKind.NOT_FOUND:
                raise FeedUnavailable(
                    self.name,
                    f"slug {ALERTS_SLUG!r} returned 404 at {url}; the alerts feed path is wrong",
                    kind=ErrorKind.NOT_FOUND,
                    url=url,
                    upstream_status=404,
                ) from exc
            raise
        msg = parse_feed_message(raw.body, feed=self.name, url=url, slug=ALERTS_SLUG)
        alerts, dropped = parse_alerts(msg)
        if dropped:
            log.warning("mta_subway_alerts: dropped %d alerts without id/header text", dropped)
        if not alerts:
            log.warning(
                "mta_subway_alerts: feed decoded with zero alerts (%d entities)", len(msg.entity)
            )
        generated = msg.header.timestamp if msg.header.HasField("timestamp") else 0
        return Snapshot[SubwayAlert](
            feed=self.name,
            fetched_at=raw.fetched_at,
            stale_after=raw.fetched_at + self.ttl,
            source_url=url,
            records=alerts,
            upstream_generated_at=_epoch(generated),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )


class SubwayStopsAdapter(_BaseAdapter):
    """Static GTFS ``stops.txt`` (plus derived routes) from the MTA S3 bucket.

    The URL is resolved from ``settings.mta_static_gtfs_url`` at fetch time, exactly like
    the realtime adapters resolve ``settings.mta_gtfs_base``, so ``NYC_LIVE_MTA_STATIC_GTFS_URL``
    can redirect or kill this feed. ``STATIC_GTFS_URL`` below is only the documented default
    that the ``Settings`` field defaults to; nothing reads it at fetch time.
    """

    name = FeedName.MTA_SUBWAY_STOPS
    ttl = DEFAULT_TTL[FeedName.MTA_SUBWAY_STOPS]

    @property
    def source_url(self) -> str:
        return self.settings.mta_static_gtfs_url

    async def fetch(self) -> Snapshot[SubwayStop]:
        started = time.perf_counter()
        url = self.source_url
        raw = await self._raw(url)
        parsed = await asyncio.to_thread(parse_static_gtfs, raw.body, feed=self.name, url=url)
        if parsed.dropped_out_of_bbox or parsed.dropped_malformed:
            log.warning(
                "mta_subway_stops: dropped %d stops outside NYC bbox and %d malformed rows",
                parsed.dropped_out_of_bbox,
                parsed.dropped_malformed,
            )
        if not parsed.routes_derived:
            log.warning("mta_subway_stops: zip lacks trips.txt/stop_times.txt; routes left empty")
        return Snapshot[SubwayStop](
            feed=self.name,
            fetched_at=raw.fetched_at,
            stale_after=raw.fetched_at + self.ttl,
            source_url=url,
            records=parsed.stops,
            upstream_generated_at=raw.last_modified,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
