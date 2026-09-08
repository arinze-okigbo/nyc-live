"""Ny511CamerasAdapter: key-gated skip path, plus a minimal configured-path mapping check."""

from __future__ import annotations

import importlib

import httpx
import pytest
import respx

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    CameraSource,
    ErrorKind,
    FeedAdapter,
    FeedName,
    FeedNotConfigured,
    FeedUnavailable,
)
from nyc_live.feeds import ADAPTER_SPECS
from nyc_live.feeds.ny511 import NY511_CAMERAS_URL, Ny511CamerasAdapter
from nyc_live.http import make_client


@pytest.fixture
def unkeyed_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.delenv("NY511_API_KEY", raising=False)
    return settings.model_copy(update={"ny511_api_key": None, "http_retries": 0})


@pytest.fixture
async def client(unkeyed_settings: Settings):
    async with make_client(unkeyed_settings) as c:
        yield c


def test_registry_and_protocol(client: httpx.AsyncClient, unkeyed_settings: Settings) -> None:
    adapter = Ny511CamerasAdapter(client=client, settings=unkeyed_settings)
    assert isinstance(adapter, FeedAdapter)
    assert adapter.name is FeedName.NY511_CAMERAS
    assert adapter.ttl == DEFAULT_TTL[FeedName.NY511_CAMERAS]
    spec = next(s for s in ADAPTER_SPECS if s.feed is FeedName.NY511_CAMERAS)
    assert getattr(importlib.import_module(spec.module), spec.cls) is Ny511CamerasAdapter


@pytest.mark.parametrize("key", [None, "", "   "])
async def test_skips_cleanly_without_key(
    client: httpx.AsyncClient,
    unkeyed_settings: Settings,
    respx_mock: respx.MockRouter,
    key: str | None,
) -> None:
    settings = unkeyed_settings.model_copy(update={"ny511_api_key": key})
    adapter = Ny511CamerasAdapter(client=client, settings=settings)
    assert adapter.is_configured() is False
    with pytest.raises(FeedNotConfigured) as exc:
        await adapter.fetch()
    assert isinstance(exc.value, FeedUnavailable)
    assert exc.value.kind is ErrorKind.NOT_CONFIGURED
    assert exc.value.env_var == "NY511_API_KEY"
    assert exc.value.feed is FeedName.NY511_CAMERAS
    assert not respx_mock.calls  # never touched the network


async def test_configured_path_maps_and_keeps_only_nyc(
    client: httpx.AsyncClient, unkeyed_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    # Inline rows in the documented 511NY shape; not upstream data.
    rows = [
        {
            "ID": "NYC-1",
            "Name": "I-278 at Kosciuszko Bridge",
            "Latitude": 40.7290,
            "Longitude": -73.9280,
            "Url": "https://511ny.org/example/NYC-1.jpg",
            "RoadwayName": "I-278",
            "DirectionOfTravel": "Eastbound",
            "Disabled": False,
            "Blocked": "false",
        },
        {
            "ID": "ALB-1",
            "Name": "I-87 at Exit 24",
            "Latitude": 42.6526,
            "Longitude": -73.7562,
            "Url": "https://511ny.org/example/ALB-1.jpg",
            "Disabled": True,
        },
    ]
    settings = unkeyed_settings.model_copy(update={"ny511_api_key": "test-key"})
    route = respx_mock.get(NY511_CAMERAS_URL, params={"key": "test-key", "format": "json"}).mock(
        return_value=httpx.Response(200, json=rows)
    )
    adapter = Ny511CamerasAdapter(client=client, settings=settings)
    assert adapter.is_configured() is True
    snap = await adapter.fetch()
    assert route.call_count == 1
    assert snap.source_url == NY511_CAMERAS_URL  # key never appears in source_url
    (cam,) = snap.records
    assert cam.id == "NYC-1" and cam.source is CameraSource.NY511
    assert cam.is_online is True and cam.roadway == "I-278" and cam.direction == "Eastbound"
