"""511NY cameras: key-gated adapter stub (Phase 1 deferred feed).

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
from collections.abc import Mapping, Sequence
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
    Snapshot,
    now_utc,
)
from nyc_live.feeds.cameras import Clock, extract_rows, first_present, to_bool, to_float, to_str
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import get_with_retry

log = logging.getLogger(__name__)

NY511_ENV_VAR = "NY511_API_KEY"
NY511_CAMERAS_URL = "https://511ny.org/api/getcameras"

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
