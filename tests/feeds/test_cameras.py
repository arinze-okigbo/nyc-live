"""CameraListAdapter: offline error/behaviour tests (respx, inline bodies), fixture replay, live."""

from __future__ import annotations

import importlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    Camera,
    CameraSource,
    ErrorKind,
    FeedAdapter,
    FeedName,
    FeedUnavailable,
)
from nyc_live.feeds import ADAPTER_SPECS
from nyc_live.feeds.cameras import CameraListAdapter, jpeg_dimensions, to_bool
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import make_client

BASE = "https://cams.test/api/cameras"
LIST_URL = f"{BASE}/"

# Inline, minimal rows shaped like the documented public list. NOT upstream data.
TIMES_SQUARE = {
    "id": "0a1b2c3d-0000-4000-8000-000000000001",
    "name": "7 Ave @ 42 St",
    "latitude": 40.7580,
    "longitude": -73.9855,
    "area": "Manhattan",
    "isOnline": True,
    "imageUrl": f"{BASE}/0a1b2c3d-0000-4000-8000-000000000001/image",
}
FLATBUSH_SNAKE = {
    "id": "0a1b2c3d-0000-4000-8000-000000000002",
    "name": "Flatbush Ave @ Atlantic Ave",
    "lat": "40.6840",
    "lon": "-73.9770",
    "area": "Brooklyn",
    "is_online": "false",
    "roadway_name": "Flatbush Ave",
    "direction_of_travel": "NB",
}
ALBANY_OUTSIDE = {
    "id": "0a1b2c3d-0000-4000-8000-000000000003",
    "name": "I-87 @ Exit 24",
    "latitude": 42.6526,
    "longitude": -73.7562,
    "isOnline": True,
}


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


def test_adapter_satisfies_protocol_and_registry(
    client: httpx.AsyncClient, cam_settings: Settings
) -> None:
    adapter = CameraListAdapter(client=client, settings=cam_settings)
    assert isinstance(adapter, FeedAdapter)
    assert adapter.name is FeedName.DOT_CAMERAS
    assert adapter.ttl == DEFAULT_TTL[FeedName.DOT_CAMERAS]
    assert adapter.is_configured() is True
    assert adapter.list_url == LIST_URL
    assert adapter.frame_url("abc") == f"{BASE}/abc/image"
    spec = next(s for s in ADAPTER_SPECS if s.feed is FeedName.DOT_CAMERAS)
    cls = getattr(importlib.import_module(spec.module), spec.cls)
    assert cls is CameraListAdapter


async def test_maps_both_key_styles_and_drops_outside_bbox(
    client: httpx.AsyncClient,
    cam_settings: Settings,
    respx_mock: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="nyc_live.feeds.cameras")
    route = respx_mock.get(LIST_URL).mock(
        return_value=httpx.Response(200, json=[TIMES_SQUARE, FLATBUSH_SNAKE, ALBANY_OUTSIDE])
    )
    adapter = CameraListAdapter(client=client, settings=cam_settings)
    snap = await adapter.fetch()

    assert route.call_count == 1
    assert snap.feed is FeedName.DOT_CAMERAS
    assert snap.source_url == LIST_URL
    assert snap.stale_after - snap.fetched_at == DEFAULT_TTL[FeedName.DOT_CAMERAS]
    assert snap.latency_ms is not None
    assert [c.id for c in snap.records] == [TIMES_SQUARE["id"], FLATBUSH_SNAKE["id"]]

    ts, fb = snap.records
    assert isinstance(ts, Camera)
    assert ts.source is CameraSource.NYC_DOT
    assert ts.is_online is True and ts.area == "Manhattan"
    assert ts.image_url == TIMES_SQUARE["imageUrl"]
    assert (ts.lat, ts.lon) == (40.7580, -73.9855)

    assert fb.is_online is False  # string "false"
    assert (fb.lat, fb.lon) == (40.6840, -73.9770)  # string coordinates coerced
    assert fb.image_url == f"{BASE}/{fb.id}/image"  # no imageUrl upstream -> derived
    assert fb.roadway == "Flatbush Ave" and fb.direction == "NB"

    assert "dropped 1 of 3 cameras outside NYC bbox" in caplog.text


async def test_fetch_inside_ttl_does_not_hit_upstream(
    client: httpx.AsyncClient, cam_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(LIST_URL).mock(return_value=httpx.Response(200, json=[TIMES_SQUARE]))
    clock = FakeClock()
    adapter = CameraListAdapter(client=client, settings=cam_settings, clock=clock)
    first = await adapter.fetch()
    second = await adapter.fetch()
    assert second is first and route.call_count == 1
    clock.advance(DEFAULT_TTL[FeedName.DOT_CAMERAS] + timedelta(seconds=1))
    third = await adapter.fetch()
    assert third is not first and route.call_count == 2


async def test_duplicate_ids_are_dropped_once(
    client: httpx.AsyncClient, cam_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(LIST_URL).mock(return_value=httpx.Response(200, json=[TIMES_SQUARE] * 3))
    snap = await CameraListAdapter(client=client, settings=cam_settings).fetch()
    assert len(snap.records) == 1


async def test_missing_required_keys_on_every_row_is_upstream_parse(
    client: httpx.AsyncClient, cam_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(LIST_URL).mock(
        return_value=httpx.Response(200, json=[{"cameraId": "x", "title": "y"}, {}])
    )
    with pytest.raises(FeedUnavailable) as exc:
        await CameraListAdapter(client=client, settings=cam_settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "latitude" in exc.value.message  # names the keys it looked for


async def test_partially_bad_rows_are_skipped_not_fatal(
    client: httpx.AsyncClient, cam_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    bad_lat = {**TIMES_SQUARE, "id": "bad", "latitude": 400.0}
    respx_mock.get(LIST_URL).mock(
        return_value=httpx.Response(200, json=[TIMES_SQUARE, {"name": "no id"}, bad_lat, "junk"])
    )
    snap = await CameraListAdapter(client=client, settings=cam_settings).fetch()
    assert [c.id for c in snap.records] == [TIMES_SQUARE["id"]]


@pytest.mark.parametrize(
    "body",
    [b"<html>maintenance</html>", b"", json.dumps({"status": "ok"}).encode(), b"[]"],
)
async def test_unusable_bodies_are_upstream_parse(
    client: httpx.AsyncClient, cam_settings: Settings, respx_mock: respx.MockRouter, body: bytes
) -> None:
    respx_mock.get(LIST_URL).mock(return_value=httpx.Response(200, content=body))
    with pytest.raises(FeedUnavailable) as exc:
        await CameraListAdapter(client=client, settings=cam_settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_PARSE


async def test_all_outside_bbox_is_loud(
    client: httpx.AsyncClient, cam_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(LIST_URL).mock(return_value=httpx.Response(200, json=[ALBANY_OUTSIDE]))
    with pytest.raises(FeedUnavailable) as exc:
        await CameraListAdapter(client=client, settings=cam_settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "outside the NYC bbox" in exc.value.message


async def test_404_on_list_is_not_found(
    client: httpx.AsyncClient, cam_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(LIST_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(FeedUnavailable) as exc:
        await CameraListAdapter(client=client, settings=cam_settings).fetch()
    assert exc.value.kind is ErrorKind.NOT_FOUND and exc.value.upstream_status == 404


async def test_5xx_is_upstream_http(
    client: httpx.AsyncClient, cam_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(LIST_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(FeedUnavailable) as exc:
        await CameraListAdapter(client=client, settings=cam_settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_HTTP and exc.value.upstream_status == 503
    assert route.call_count == 1  # http_retries=0 in cam_settings


async def test_timeout_is_upstream_timeout(
    client: httpx.AsyncClient, cam_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(LIST_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FeedUnavailable) as exc:
        await CameraListAdapter(client=client, settings=cam_settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_TIMEOUT


def test_to_bool_variants() -> None:
    assert to_bool(True) is True and to_bool(False) is False
    assert to_bool("true") is True and to_bool("False") is False
    assert to_bool(1) is True and to_bool(0) is False
    assert to_bool("maybe") is None and to_bool(None) is None


def test_jpeg_dimensions_rejects_non_jpeg() -> None:
    assert jpeg_dimensions(b"") is None
    assert jpeg_dimensions(b"GIF89a") is None
    assert jpeg_dimensions(b"\xff\xd8\xff\xd9") is None


# ---------------------------------------------------------------------------
# Recorded fixture replay (see tests/fixtures/cameras/RECORD.md)
# ---------------------------------------------------------------------------


async def test_replays_recorded_camera_list(
    client: httpx.AsyncClient,
    cam_settings: Settings,
    respx_mock: respx.MockRouter,
    fixtures_dir: Path,
) -> None:
    path = fixtures_dir / "cameras" / "cameras.json"
    if not path.exists():
        pytest.skip(f"fixture not recorded yet: {path}")
    respx_mock.get(LIST_URL).mock(return_value=httpx.Response(200, content=path.read_bytes()))
    snap = await CameraListAdapter(client=client, settings=cam_settings).fetch()
    assert snap.records
    ids = [c.id for c in snap.records]
    assert len(ids) == len(set(ids))
    for cam in snap.records:
        assert cam.source is CameraSource.NYC_DOT
        assert in_nyc_bbox(cam.lat, cam.lon)
        assert cam.image_url.startswith("http")
        assert cam.name


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_camera_list(settings: Settings) -> None:
    async with make_client(settings) as live_client:
        snap = await CameraListAdapter(client=live_client, settings=settings).fetch()
    online = [c for c in snap.records if c.is_online]
    assert len(online) > 500, f"only {len(online)} online of {len(snap.records)}"
    ids = [c.id for c in snap.records]
    assert len(ids) == len(set(ids))
    for cam in snap.records:
        assert in_nyc_bbox(cam.lat, cam.lon), (cam.id, cam.lat, cam.lon)
        assert cam.source is CameraSource.NYC_DOT
