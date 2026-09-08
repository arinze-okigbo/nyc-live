"""CameraFrameSource: cadence, buffer, eviction, telemetry, error paths, fixture replay, live."""

from __future__ import annotations

import importlib
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from nyc_live.config import Settings
from nyc_live.contracts import (
    CAMERA_FRAME_MIN_INTERVAL,
    FRAME_BUFFER_MAX_AGE,
    FRAME_BUFFER_MAX_FRAMES_PER_CAMERA,
    CameraFrame,
    CameraFrameFetch,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    FrameSource,
)
from nyc_live.feeds import FRAME_SOURCE_SPEC
from nyc_live.feeds.cameras import (
    JPEG_MAGIC,
    CameraFrameSource,
    CameraListAdapter,
    jpeg_dimensions,
)
from nyc_live.http import RateLimiter, make_client
from nyc_live.store import Store

BASE = "https://cams.test/api/cameras"
CAM = "0a1b2c3d-0000-4000-8000-0000000000aa"
FRAME_URL = f"{BASE}/{CAM}/image"


def fake_jpeg(width: int = 352, height: int = 240, pad: int = 0) -> bytes:
    """Structurally valid JPEG header bytes (SOI, SOF0, optional COM, EOI). Not a real image."""
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    body = b"\xff\xd8" + sof0
    if pad:
        body += b"\xff\xfe" + struct.pack(">H", pad + 2) + b"\x00" * pad
    return body + b"\xff\xd9"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def cam_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"dot_cameras_base": BASE, "http_retries": 0})


@pytest.fixture
async def client(cam_settings: Settings):
    async with make_client(cam_settings) as c:
        yield c


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def source(
    client: httpx.AsyncClient, cam_settings: Settings, clock: FakeClock
) -> CameraFrameSource:
    """Default 2 s limiter, injected clock."""
    return CameraFrameSource(client=client, settings=cam_settings, clock=clock)


@pytest.fixture
def fast_source(
    client: httpx.AsyncClient, cam_settings: Settings, clock: FakeClock
) -> CameraFrameSource:
    """Zero-interval limiter so eviction/buffer tests never sleep; cadence is tested separately."""
    return CameraFrameSource(
        client=client, settings=cam_settings, clock=clock, limiter=RateLimiter(timedelta(0))
    )


def test_frame_source_satisfies_protocol_and_registry(source: CameraFrameSource) -> None:
    assert isinstance(source, FrameSource)
    assert source.feed is FeedName.DOT_CAMERA_FRAMES
    assert source.frame_url(CAM) == FRAME_URL
    module_name, cls_name = FRAME_SOURCE_SPEC
    assert getattr(importlib.import_module(module_name), cls_name) is CameraFrameSource
    assert CameraListAdapter.frame_url is not None  # same URL scheme on both classes


def test_fake_jpeg_helper_parses() -> None:
    data = fake_jpeg(352, 240, pad=2000)
    assert data.startswith(JPEG_MAGIC) and len(data) > 1024
    assert jpeg_dimensions(data) == (352, 240)


async def test_get_frame_returns_jpeg_and_records_telemetry(
    source: CameraFrameSource, respx_mock: respx.MockRouter, clock: FakeClock, tmp_path: Path
) -> None:
    body = fake_jpeg(352, 240, pad=1500)
    route = respx_mock.get(FRAME_URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
    )
    seen: list[CameraFrameFetch] = []
    source._on_fetch = seen.append

    frame = await source.get_frame(CAM)

    assert route.call_count == 1
    assert isinstance(frame, CameraFrame)
    assert frame.camera_id == CAM
    assert frame.data == body and frame.byte_size == len(body)
    assert frame.content_type == "image/jpeg"
    assert (frame.width, frame.height) == (352, 240)
    assert frame.fetched_at == clock.now
    assert frame.stale_after == clock.now + CAMERA_FRAME_MIN_INTERVAL
    assert "data" not in frame.model_dump()  # bytes never leave via serialisation

    rows = source.drain_telemetry()
    assert source.pending_telemetry == 0
    assert rows == seen and len(rows) == 1
    row = rows[0]
    assert row.ok is True and row.status_code == 200 and row.byte_size == len(body)
    assert row.latency_ms is not None and row.error is None and row.ts == clock.now

    # Nothing hit the disk.
    assert not list(tmp_path.rglob("*.jpg")) and not list(tmp_path.rglob("*.jpeg"))


async def test_second_call_inside_cadence_returns_buffer_without_upstream(
    source: CameraFrameSource, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = respx_mock.get(FRAME_URL).mock(return_value=httpx.Response(200, content=fake_jpeg()))
    first = await source.get_frame(CAM)
    clock.advance(timedelta(seconds=1))
    second = await source.get_frame(CAM)
    assert second is first
    assert route.call_count == 1
    assert len(source.drain_telemetry()) == 1  # buffered hits are not upstream attempts


async def test_after_cadence_window_refetches(
    fast_source: CameraFrameSource, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = respx_mock.get(FRAME_URL).mock(return_value=httpx.Response(200, content=fake_jpeg()))
    first = await fast_source.get_frame(CAM)
    clock.advance(CAMERA_FRAME_MIN_INTERVAL + timedelta(milliseconds=1))
    second = await fast_source.get_frame(CAM)
    assert second is not first and route.call_count == 2


async def test_buffer_keeps_at_most_max_frames_per_camera(
    fast_source: CameraFrameSource, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(FRAME_URL).mock(return_value=httpx.Response(200, content=fake_jpeg()))
    for _ in range(FRAME_BUFFER_MAX_FRAMES_PER_CAMERA + 3):
        await fast_source.get_frame(CAM)
        clock.advance(CAMERA_FRAME_MIN_INTERVAL + timedelta(seconds=1))
    frames = fast_source.buffered_frames(CAM)
    assert len(frames) == FRAME_BUFFER_MAX_FRAMES_PER_CAMERA
    assert frames[-1] is fast_source.buffered_frame(CAM)
    assert fast_source.buffered_camera_count == 1


async def test_frames_evicted_after_max_age(
    fast_source: CameraFrameSource, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = respx_mock.get(FRAME_URL).mock(return_value=httpx.Response(200, content=fake_jpeg()))
    first = await fast_source.get_frame(CAM)
    assert fast_source.buffered_frame(CAM) is first

    clock.advance(FRAME_BUFFER_MAX_AGE - timedelta(seconds=1))
    assert fast_source.buffered_frame(CAM) is first  # still inside max age

    clock.advance(timedelta(seconds=2))
    assert fast_source.buffered_frame(CAM) is None
    assert fast_source.buffered_camera_count == 0

    refetched = await fast_source.get_frame(CAM)
    assert refetched is not first and route.call_count == 2
    assert fast_source.clear() == 1 and fast_source.buffered_camera_count == 0


async def test_404_on_frame_is_not_found_and_drops_buffer(
    fast_source: CameraFrameSource, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = respx_mock.get(FRAME_URL).mock(return_value=httpx.Response(200, content=fake_jpeg()))
    await fast_source.get_frame(CAM)
    clock.advance(CAMERA_FRAME_MIN_INTERVAL + timedelta(seconds=1))
    route.mock(return_value=httpx.Response(404, text="not found"))

    with pytest.raises(FeedUnavailable) as exc:
        await fast_source.get_frame(CAM)
    assert exc.value.kind is ErrorKind.NOT_FOUND
    assert exc.value.feed is FeedName.DOT_CAMERA_FRAMES
    assert exc.value.upstream_status == 404 and exc.value.url == FRAME_URL
    assert CAM in exc.value.message
    assert fast_source.buffered_frame(CAM) is None

    rows = fast_source.drain_telemetry()
    assert [r.ok for r in rows] == [True, False]
    assert rows[1].status_code == 404 and rows[1].error and "not_found" in rows[1].error


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"<html><body>camera offline</body></html>", "text/html"),
        (b"", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png"),
        (b"\xff\xd8" + b"\x00" * 10, "image/jpeg"),  # SOI but no marker: not a JPEG stream
    ],
)
async def test_non_jpeg_body_is_upstream_parse(
    source: CameraFrameSource,
    respx_mock: respx.MockRouter,
    body: bytes,
    content_type: str,
) -> None:
    respx_mock.get(FRAME_URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": content_type})
    )
    with pytest.raises(FeedUnavailable) as exc:
        await source.get_frame(CAM)
    assert exc.value.kind is ErrorKind.UPSTREAM_PARSE
    assert exc.value.upstream_status == 200 and exc.value.url == FRAME_URL
    assert source.buffered_frame(CAM) is None
    (row,) = source.drain_telemetry()
    assert row.ok is False and row.status_code == 200 and row.byte_size == len(body)
    assert row.error and "upstream_parse" in row.error


async def test_5xx_and_timeout_are_recorded(
    source: CameraFrameSource, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(FRAME_URL).mock(return_value=httpx.Response(502))
    with pytest.raises(FeedUnavailable) as exc:
        await source.get_frame(CAM)
    assert exc.value.kind is ErrorKind.UPSTREAM_HTTP and exc.value.upstream_status == 502

    other = "0a1b2c3d-0000-4000-8000-0000000000bb"
    respx_mock.get(f"{BASE}/{other}/image").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FeedUnavailable) as exc2:
        await source.get_frame(other)
    assert exc2.value.kind is ErrorKind.UPSTREAM_TIMEOUT

    rows = source.drain_telemetry()
    assert [(r.camera_id, r.ok, r.status_code) for r in rows] == [
        (CAM, False, 502),
        (other, False, None),
    ]


async def test_telemetry_rows_persist_through_store(
    fast_source: CameraFrameSource, respx_mock: respx.MockRouter, store: Store
) -> None:
    respx_mock.get(FRAME_URL).mock(return_value=httpx.Response(200, content=fake_jpeg()))
    await fast_source.get_frame(CAM)
    assert store.record_frame_fetches(fast_source.drain_telemetry()) == 1
    assert store.execute("SELECT camera_id, ok FROM camera_frame_fetches") == [(CAM, True)]


async def test_callback_exception_does_not_break_fetch(
    client: httpx.AsyncClient, cam_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    def boom(_: CameraFrameFetch) -> None:
        raise RuntimeError("sink is down")

    respx_mock.get(FRAME_URL).mock(return_value=httpx.Response(200, content=fake_jpeg()))
    src = CameraFrameSource(client=client, settings=cam_settings, on_fetch=boom)
    frame = await src.get_frame(CAM)
    assert frame.byte_size > 0 and len(src.drain_telemetry()) == 1


# ---------------------------------------------------------------------------
# Recorded fixture replay (see tests/fixtures/cameras/RECORD.md)
# ---------------------------------------------------------------------------


async def test_replays_recorded_frame(
    source: CameraFrameSource, respx_mock: respx.MockRouter, fixtures_dir: Path
) -> None:
    path = fixtures_dir / "cameras" / "frame.jpg"
    if not path.exists():
        pytest.skip(f"fixture not recorded yet: {path}")
    body = path.read_bytes()
    respx_mock.get(FRAME_URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
    )
    frame = await source.get_frame(CAM)
    assert frame.data.startswith(JPEG_MAGIC) and frame.byte_size > 1024
    assert frame.width and frame.height and frame.width > 0 and frame.height > 0


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_frame_fetch_and_cadence(settings: Settings) -> None:
    requests: list[str] = []

    async def on_request(request: httpx.Request) -> None:
        requests.append(str(request.url))

    async with make_client(settings, event_hooks={"request": [on_request]}) as live_client:
        snap = await CameraListAdapter(client=live_client, settings=settings).fetch()
        online = next(c for c in snap.records if c.is_online)
        source = CameraFrameSource(client=live_client, settings=settings)

        first = await source.get_frame(online.id)
        assert first.data.startswith(JPEG_MAGIC)
        assert first.byte_size > 1024
        frame_requests_after_first = [u for u in requests if u.endswith("/image")]
        assert len(frame_requests_after_first) == 1

        second = await source.get_frame(online.id)  # well inside 2 s
        assert second is first
        assert len([u for u in requests if u.endswith("/image")]) == 1

    rows = source.drain_telemetry()
    assert len(rows) == 1 and rows[0].ok is True and rows[0].byte_size == first.byte_size
