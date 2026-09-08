"""NYC DOT traffic cameras: the camera list adapter and the per-camera frame source.

Upstream (endpoints documented in the feed-cameras brief; no auth; not probed live,
see the field-mapping note below):

* list  : ``{settings.dot_cameras_base}/``            -> JSON array of ~900 cameras
* frame : ``{settings.dot_cameras_base}/{id}/image``  -> JPEG, refreshes about every 2 s

Camera ids are UUIDs and rotate. The list is refreshed on
``DEFAULT_TTL[FeedName.DOT_CAMERAS]``; a 404 on a frame is surfaced as
``ErrorKind.NOT_FOUND`` so callers refresh the list instead of retrying blindly.

FIELD MAPPING ASSUMPTIONS (confirm on the first live run)
---------------------------------------------------------
The list endpoint could not be probed from the sandbox this module was written in,
so upstream field names are mapped defensively from the known public shape. Both
camelCase and snake_case variants are accepted; the first present key wins:

    Camera.id        <- "id"
    Camera.name      <- "name"
    Camera.lat       <- "latitude"  | "lat"
    Camera.lon       <- "longitude" | "lon" | "lng" | "long"
    Camera.is_online <- "isOnline"  | "is_online" | "online"      (bool or "true"/"false")
    Camera.image_url <- "imageUrl"  | "image_url" | "url"         (else "{base}/{id}/image")
    Camera.area      <- "area"
    Camera.roadway   <- "roadway"   | "roadwayName" | "roadway_name"
    Camera.direction <- "direction" | "directionOfTravel" | "direction_of_travel"

A row missing any of ``id``, ``name``, ``lat``, ``lon`` is skipped and counted. If
*every* row is missing them the payload shape is wrong and ``fetch()`` raises
``FeedUnavailable(kind=UPSTREAM_PARSE)`` naming the keys it looked for. If the
``is_online`` key is absent on every row a warning is logged and cameras are
reported offline rather than guessed online.

PRIVACY
-------
``CameraFrameSource`` keeps at most ``FRAME_BUFFER_MAX_FRAMES_PER_CAMERA`` frames per
camera in memory, evicts them after ``FRAME_BUFFER_MAX_AGE``, and never writes a
frame to disk. See docs/privacy.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from nyc_live.config import Settings
from nyc_live.contracts import (
    CAMERA_FRAME_MIN_INTERVAL,
    DEFAULT_TTL,
    FRAME_BUFFER_MAX_AGE,
    FRAME_BUFFER_MAX_FRAMES_PER_CAMERA,
    Camera,
    CameraFrame,
    CameraFrameFetch,
    CameraSource,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    Snapshot,
    now_utc,
)
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

JPEG_MAGIC = b"\xff\xd8\xff"
"""SOI marker followed by the first byte of the next marker. Every JPEG starts this way."""

TELEMETRY_MAX_ROWS = 10_000
"""Upper bound on undrained CameraFrameFetch rows held in memory (oldest dropped first)."""

Clock = Callable[[], datetime]
FetchCallback = Callable[[CameraFrameFetch], None]

# Candidate upstream keys, first match wins. See the module docstring.
_ID_KEYS = ("id",)
_NAME_KEYS = ("name",)
_LAT_KEYS = ("latitude", "lat")
_LON_KEYS = ("longitude", "lon", "lng", "long")
_ONLINE_KEYS = ("isOnline", "is_online", "online")
_IMAGE_KEYS = ("imageUrl", "image_url", "url")
_AREA_KEYS = ("area",)
_ROADWAY_KEYS = ("roadway", "roadwayName", "roadway_name")
_DIRECTION_KEYS = ("direction", "directionOfTravel", "direction_of_travel")
_REQUIRED_KEYS_DESCRIPTION = f"id={_ID_KEYS}, name={_NAME_KEYS}, lat={_LAT_KEYS}, lon={_LON_KEYS}"
_LIST_WRAPPER_KEYS = ("cameras", "data", "items", "results")


# ---------------------------------------------------------------------------
# Small parsing helpers (shared with ny511.py)
# ---------------------------------------------------------------------------


def first_present(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    """First non-None value among candidate keys, else None."""
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_bool(value: Any) -> bool | None:
    """Accept real booleans, 0/1, and the strings 'true'/'false'/'yes'/'no'/'1'/'0'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "online"}:
            return True
        if lowered in {"false", "0", "no", "n", "offline"}:
            return False
    return None


def to_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_rows(payload: Any, *, feed: FeedName, url: str) -> list[Any]:
    """The list endpoint returns a bare JSON array; tolerate a one-level dict wrapper."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _LIST_WRAPPER_KEYS:
            inner = payload.get(key)
            if isinstance(inner, list):
                return inner
    raise FeedUnavailable(
        feed,
        f"expected a JSON array of cameras (or a dict wrapping one under "
        f"{_LIST_WRAPPER_KEYS}), got {type(payload).__name__}",
        kind=ErrorKind.UPSTREAM_PARSE,
        url=url,
    )


# ---------------------------------------------------------------------------
# JPEG helpers
# ---------------------------------------------------------------------------

_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def is_jpeg(data: bytes) -> bool:
    return data.startswith(JPEG_MAGIC)


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the first SOF segment, or None if it cannot be found.

    Pure byte scanning; no image library. Returns None rather than raising on a
    truncated or odd stream so the frame is still served.
    """
    if not is_jpeg(data):
        return None
    n = len(data)
    i = 2
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:  # standalone markers
            i += 2
            continue
        if marker in {0xD9, 0xDA}:  # EOI / SOS before any SOF: give up
            return None
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in _SOF_MARKERS:
            if i + 9 > n:
                return None
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return (width, height) if width and height else None
        i += 2 + seg_len
    return None


# ---------------------------------------------------------------------------
# Camera list adapter
# ---------------------------------------------------------------------------


@dataclass
class ParseStats:
    total: int = 0
    missing_required: int = 0
    invalid: int = 0
    duplicate_ids: int = 0
    online_key_missing: int = 0
    dropped_outside_bbox: int = 0
    kept: int = 0
    assumed_keys: str = field(default=_REQUIRED_KEYS_DESCRIPTION)


def map_dot_camera(row: Mapping[str, Any], base_url: str) -> Camera | None:
    """Explicit mapping of one DOT row into `Camera`. None if a required key is absent."""
    cam_id = to_str(first_present(row, _ID_KEYS))
    name = to_str(first_present(row, _NAME_KEYS))
    lat = to_float(first_present(row, _LAT_KEYS))
    lon = to_float(first_present(row, _LON_KEYS))
    if cam_id is None or name is None or lat is None or lon is None:
        return None
    online = to_bool(first_present(row, _ONLINE_KEYS))
    image_url = to_str(first_present(row, _IMAGE_KEYS)) or f"{base_url}/{cam_id}/image"
    return Camera(
        id=cam_id,
        source=CameraSource.NYC_DOT,
        name=name,
        is_online=bool(online),
        image_url=image_url,
        lat=lat,
        lon=lon,
        area=to_str(first_present(row, _AREA_KEYS)),
        roadway=to_str(first_present(row, _ROADWAY_KEYS)),
        direction=to_str(first_present(row, _DIRECTION_KEYS)),
    )


def parse_dot_camera_list(rows: Sequence[Any], base_url: str) -> tuple[list[Camera], ParseStats]:
    """Map every row, drop out-of-bbox cameras and duplicate ids, count everything."""
    stats = ParseStats(total=len(rows))
    seen: set[str] = set()
    kept: list[Camera] = []
    for row in rows:
        if not isinstance(row, Mapping):
            stats.invalid += 1
            continue
        try:
            cam = map_dot_camera(row, base_url)
        except ValidationError:
            stats.invalid += 1
            continue
        if cam is None:
            stats.missing_required += 1
            continue
        if first_present(row, _ONLINE_KEYS) is None:
            stats.online_key_missing += 1
        if not in_nyc_bbox(cam.lat, cam.lon):
            stats.dropped_outside_bbox += 1
            continue
        if cam.id in seen:
            stats.duplicate_ids += 1
            continue
        seen.add(cam.id)
        kept.append(cam)
    stats.kept = len(kept)
    return kept, stats


class CameraListAdapter:
    """FeedAdapter[Camera] for the NYC DOT camera list."""

    name: FeedName = FeedName.DOT_CAMERAS
    ttl = DEFAULT_TTL[FeedName.DOT_CAMERAS]

    def __init__(
        self, client: httpx.AsyncClient, settings: Settings, *, clock: Clock | None = None
    ) -> None:
        self.client = client
        self.settings = settings
        self.base_url = settings.dot_cameras_base.rstrip("/")
        self.list_url = f"{self.base_url}/"
        self._now: Clock = clock or now_utc
        self._last: Snapshot[Camera] | None = None

    def is_configured(self) -> bool:
        return True

    @property
    def last_snapshot(self) -> Snapshot[Camera] | None:
        return self._last

    def frame_url(self, camera_id: str) -> str:
        return f"{self.base_url}/{camera_id}/image"

    async def fetch(self) -> Snapshot[Camera]:
        # Honour the TTL as our own minimum upstream interval (the cache does too).
        if self._last is not None and self._now() < self._last.stale_after:
            return self._last
        started = time.perf_counter()
        resp = await get_with_retry(
            self.client, self.list_url, feed=self.name, retries=self.settings.http_retries
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
        cameras, stats = parse_dot_camera_list(rows, self.base_url)
        self._log_stats(stats)
        if not cameras:
            raise FeedUnavailable(
                self.name,
                self._empty_reason(stats),
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

    @staticmethod
    def _empty_reason(stats: ParseStats) -> str:
        if stats.total == 0:
            return "camera list is empty (0 rows)"
        if stats.kept == 0 and stats.dropped_outside_bbox == stats.total:
            return f"all {stats.total} cameras are outside the NYC bbox"
        return (
            f"none of {stats.total} rows had the required keys; looked for {stats.assumed_keys} "
            f"(missing_required={stats.missing_required}, invalid={stats.invalid})"
        )

    def _log_stats(self, stats: ParseStats) -> None:
        if stats.dropped_outside_bbox:
            log.info(
                "%s: dropped %d of %d cameras outside NYC bbox",
                self.name.value,
                stats.dropped_outside_bbox,
                stats.total,
            )
        if stats.missing_required or stats.invalid:
            log.warning(
                "%s: skipped %d rows missing required keys and %d invalid rows (of %d); "
                "assumed keys %s",
                self.name.value,
                stats.missing_required,
                stats.invalid,
                stats.total,
                stats.assumed_keys,
            )
        if stats.duplicate_ids:
            log.warning("%s: dropped %d duplicate camera ids", self.name.value, stats.duplicate_ids)
        if stats.total and stats.online_key_missing == stats.total - stats.missing_required:
            log.warning(
                "%s: no row carried an online flag (looked for %s); reporting all as offline",
                self.name.value,
                _ONLINE_KEYS,
            )


# ---------------------------------------------------------------------------
# Frame source
# ---------------------------------------------------------------------------


class CameraFrameSource:
    """FrameSource for DOT JPEG frames with the 2 s per-camera cadence.

    * ``get_frame(camera_id)`` returns the buffered frame if it is younger than
      ``CAMERA_FRAME_MIN_INTERVAL``; otherwise waits on the per-camera
      ``RateLimiter`` and fetches ``{base}/{id}/image``.
    * At most ``FRAME_BUFFER_MAX_FRAMES_PER_CAMERA`` frames per camera are kept,
      in memory only, and evicted once older than ``FRAME_BUFFER_MAX_AGE``.
    * Every upstream attempt appends a ``CameraFrameFetch`` row to an in-memory
      list (``drain_telemetry()``) and, if given, calls ``on_fetch(row)``. This
      module never opens DuckDB; the caller persists the rows.
    * A 404 is ``ErrorKind.NOT_FOUND`` (the id rotated; refresh the list). A body
      without JPEG magic bytes is ``ErrorKind.UPSTREAM_PARSE``.
    """

    feed: FeedName = FeedName.DOT_CAMERA_FRAMES

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        *,
        on_fetch: FetchCallback | None = None,
        limiter: RateLimiter | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.base_url = settings.dot_cameras_base.rstrip("/")
        self._on_fetch = on_fetch
        self._limiter = limiter or RateLimiter(CAMERA_FRAME_MIN_INTERVAL)
        self._now: Clock = clock or now_utc
        self._buffers: dict[str, deque[CameraFrame]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._telemetry: deque[CameraFrameFetch] = deque(maxlen=TELEMETRY_MAX_ROWS)

    # -- public helpers ----------------------------------------------------

    def frame_url(self, camera_id: str) -> str:
        return f"{self.base_url}/{camera_id}/image"

    @property
    def buffered_camera_count(self) -> int:
        return len(self._buffers)

    def buffered_frames(self, camera_id: str) -> list[CameraFrame]:
        """Frames currently held for a camera (oldest first), after eviction."""
        self.evict_expired()
        return list(self._buffers.get(camera_id, ()))

    def buffered_frame(self, camera_id: str) -> CameraFrame | None:
        """Newest buffered frame for a camera, or None. Never touches upstream."""
        frames = self.buffered_frames(camera_id)
        return frames[-1] if frames else None

    def evict_expired(self) -> int:
        """Drop every frame older than FRAME_BUFFER_MAX_AGE. Returns how many were dropped."""
        cutoff = self._now() - FRAME_BUFFER_MAX_AGE
        dropped = 0
        for camera_id in list(self._buffers):
            buf = self._buffers[camera_id]
            while buf and buf[0].fetched_at < cutoff:
                buf.popleft()
                dropped += 1
            if not buf:
                del self._buffers[camera_id]
        return dropped

    def clear(self) -> int:
        """Drop every buffered frame (e.g. on shutdown). Returns how many were dropped."""
        dropped = sum(len(b) for b in self._buffers.values())
        self._buffers.clear()
        return dropped

    def drain_telemetry(self) -> list[CameraFrameFetch]:
        """Return and clear the accumulated CameraFrameFetch rows."""
        rows = list(self._telemetry)
        self._telemetry.clear()
        return rows

    @property
    def pending_telemetry(self) -> int:
        return len(self._telemetry)

    # -- FrameSource -------------------------------------------------------

    async def get_frame(self, camera_id: str) -> CameraFrame:
        async with self._lock(camera_id):
            cached = self.buffered_frame(camera_id)
            if cached is not None and self._now() < cached.stale_after:
                return cached
            await self._limiter.wait(camera_id)
            return await self._fetch(camera_id)

    # -- internals ---------------------------------------------------------

    def _lock(self, camera_id: str) -> asyncio.Lock:
        lock = self._locks.get(camera_id)
        if lock is None:
            lock = self._locks[camera_id] = asyncio.Lock()
        return lock

    async def _fetch(self, camera_id: str) -> CameraFrame:
        url = self.frame_url(camera_id)
        started = time.perf_counter()
        try:
            resp = await get_with_retry(
                self.client, url, feed=self.feed, retries=self.settings.http_retries
            )
        except FeedUnavailable as exc:
            self._record(
                camera_id,
                ok=False,
                status_code=exc.upstream_status,
                latency_ms=_elapsed_ms(started),
                error=f"{exc.kind.value}: {exc.message}",
            )
            if exc.kind is ErrorKind.NOT_FOUND:
                self._buffers.pop(camera_id, None)
                raise FeedUnavailable(
                    self.feed,
                    f"camera {camera_id} not found upstream (404); ids rotate, refresh the list",
                    kind=ErrorKind.NOT_FOUND,
                    url=url,
                    upstream_status=404,
                ) from exc
            raise
        latency_ms = _elapsed_ms(started)
        data = resp.content
        if not is_jpeg(data):
            content_type = resp.headers.get("content-type", "<none>")
            message = (
                f"body for camera {camera_id} is not a JPEG "
                f"(content-type {content_type!r}, {len(data)} bytes)"
            )
            self._record(
                camera_id,
                ok=False,
                status_code=resp.status_code,
                latency_ms=latency_ms,
                byte_size=len(data),
                error=f"{ErrorKind.UPSTREAM_PARSE.value}: {message}",
            )
            raise FeedUnavailable(
                self.feed,
                message,
                kind=ErrorKind.UPSTREAM_PARSE,
                url=url,
                upstream_status=resp.status_code,
            )
        fetched_at = self._now()
        dims = jpeg_dimensions(data)
        frame = CameraFrame(
            camera_id=camera_id,
            fetched_at=fetched_at,
            stale_after=fetched_at + CAMERA_FRAME_MIN_INTERVAL,
            content_type="image/jpeg",
            data=data,
            byte_size=len(data),
            width=dims[0] if dims else None,
            height=dims[1] if dims else None,
        )
        buf = self._buffers.get(camera_id)
        if buf is None:
            buf = self._buffers[camera_id] = deque(maxlen=FRAME_BUFFER_MAX_FRAMES_PER_CAMERA)
        buf.append(frame)
        self._record(
            camera_id,
            ok=True,
            status_code=resp.status_code,
            latency_ms=latency_ms,
            byte_size=len(data),
        )
        return frame

    def _record(
        self,
        camera_id: str,
        *,
        ok: bool,
        status_code: int | None,
        latency_ms: float | None,
        byte_size: int | None = None,
        error: str | None = None,
    ) -> None:
        row = CameraFrameFetch(
            camera_id=camera_id,
            ts=self._now(),
            ok=ok,
            status_code=status_code,
            latency_ms=latency_ms,
            byte_size=byte_size,
            error=error,
        )
        self._telemetry.append(row)
        if self._on_fetch is not None:
            try:
                self._on_fetch(row)
            except Exception:
                log.exception("on_fetch callback failed for camera %s", camera_id)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
