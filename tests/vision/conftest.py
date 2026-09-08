"""Shared test doubles for the nyc-vision tests.

Everything here is a LOGIC INPUT, clearly labelled: a synthetic JPEG made with Pillow
and an in-process stand-in for the DOT camera list / frame source. None of it pretends
to be a recorded upstream response, and no synthetic number is presented as data.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

import pytest
from PIL import Image

from nyc_live.contracts import (
    CAMERA_FRAME_MIN_INTERVAL,
    DEFAULT_TTL,
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

# A spread of plausible NYC coordinates, one per borough-ish area, so the
# stratification tests have something with real geographic structure.
BOROUGH_POINTS: dict[str, tuple[float, float]] = {
    "manhattan": (40.7580, -73.9855),
    "brooklyn": (40.6782, -73.9442),
    "queens": (40.7282, -73.7949),
    "bronx": (40.8448, -73.8648),
    "staten_island": (40.5795, -74.1502),
}


def make_camera(
    camera_id: str,
    lat: float,
    lon: float,
    *,
    name: str | None = None,
    is_online: bool = True,
) -> Camera:
    return Camera(
        id=camera_id,
        source=CameraSource.NYC_DOT,
        name=name or f"Camera {camera_id}",
        is_online=is_online,
        image_url=f"https://webcams.nyctmc.org/api/cameras/{camera_id}/image",
        lat=lat,
        lon=lon,
    )


def borough_cameras(per_borough: int = 20) -> list[Camera]:
    """`per_borough` cameras tightly clustered (within ~200 m) around each borough point.

    Tight on purpose: the clusters must not straddle a stratification grid cell, so the
    sampling tests can assert an exact per-borough share.
    """
    cams: list[Camera] = []
    for borough, (lat, lon) in BOROUGH_POINTS.items():
        for i in range(per_borough):
            cams.append(
                make_camera(
                    f"{borough}-{i:03d}",
                    lat + (i % 5) * 0.0004,
                    lon + (i // 5) * 0.0004,
                    name=f"{borough.title()} {i}",
                )
            )
    return cams


def synthetic_jpeg(width: int = 64, height: int = 48, colour: int = 128) -> bytes:
    """A real JPEG produced locally by Pillow. Not upstream data; a decode-path input."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (colour, colour, colour)).save(buf, format="JPEG")
    return buf.getvalue()


class FakeCameraFeed:
    """Stands in for `CameraListAdapter`; returns a fixed Snapshot or raises."""

    name = FeedName.DOT_CAMERAS
    ttl = DEFAULT_TTL[FeedName.DOT_CAMERAS]

    def __init__(self, cameras: Sequence[Camera], *, error: FeedUnavailable | None = None) -> None:
        self.cameras = list(cameras)
        self.error = error
        self.calls = 0

    def is_configured(self) -> bool:
        return True

    async def fetch(self) -> Snapshot[Camera]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        t = now_utc()
        return Snapshot[Camera](
            feed=self.name,
            fetched_at=t,
            stale_after=t + self.ttl,
            source_url="https://webcams.nyctmc.org/api/cameras/",
            records=list(self.cameras),
        )


class FakeFrameSource:
    """In-process FrameSource: hands out a synthetic JPEG and records telemetry.

    Mirrors the real `CameraFrameSource` surface the pipeline uses: `get_frame`,
    `drain_telemetry`, `evict_expired`. Cameras named in `fail_ids` raise
    `FeedUnavailable` and record a failed telemetry row, exactly like a 404 upstream.
    """

    def __init__(
        self,
        *,
        fail_ids: Iterable[str] = (),
        jpeg: bytes | None = None,
        clock: datetime | None = None,
    ) -> None:
        self.fail_ids = set(fail_ids)
        self.jpeg = jpeg if jpeg is not None else synthetic_jpeg()
        self.telemetry: list[CameraFrameFetch] = []
        self.requested: list[str] = []
        self.evictions = 0
        self._fixed_now = clock

    def _now(self) -> datetime:
        return self._fixed_now or now_utc()

    async def get_frame(self, camera_id: str) -> CameraFrame:
        self.requested.append(camera_id)
        t = self._now()
        if camera_id in self.fail_ids:
            self.telemetry.append(
                CameraFrameFetch(
                    camera_id=camera_id,
                    ts=t,
                    ok=False,
                    status_code=404,
                    latency_ms=12.0,
                    error="not_found: camera not found upstream (404)",
                )
            )
            raise FeedUnavailable(
                FeedName.DOT_CAMERA_FRAMES,
                f"camera {camera_id} not found upstream (404)",
                kind=ErrorKind.NOT_FOUND,
                upstream_status=404,
            )
        self.telemetry.append(
            CameraFrameFetch(
                camera_id=camera_id,
                ts=t,
                ok=True,
                status_code=200,
                latency_ms=34.5,
                byte_size=len(self.jpeg),
            )
        )
        return CameraFrame(
            camera_id=camera_id,
            fetched_at=t,
            stale_after=t + CAMERA_FRAME_MIN_INTERVAL,
            content_type="image/jpeg",
            data=self.jpeg,
            byte_size=len(self.jpeg),
            width=64,
            height=48,
        )

    def drain_telemetry(self) -> list[CameraFrameFetch]:
        rows = list(self.telemetry)
        self.telemetry.clear()
        return rows

    def evict_expired(self) -> int:
        self.evictions += 1
        return 0


@pytest.fixture
def jpeg_bytes() -> bytes:
    return synthetic_jpeg()


@pytest.fixture
def cameras() -> list[Camera]:
    return borough_cameras()


def minutes_ago(n: float) -> datetime:
    return now_utc() - timedelta(minutes=n)
