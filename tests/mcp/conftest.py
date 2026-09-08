"""Fixtures for nyc-mcp tests: fake adapters behind the real cache, no network.

The records below are synthetic inputs for exercising tool logic (filtering, joins,
envelope shape). They are not upstream fixtures and are never presented as real data.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP
from pydantic import BaseModel

from nyc_live.cache import CachedFeed, FeedRegistry
from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    BikeStation,
    Camera,
    CameraSource,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    ServiceRequest,
    Snapshot,
    StopTimeUpdate,
    SubwayAlert,
    SubwayStop,
    SubwayTrip,
    WeatherObservation,
    WeatherReport,
    now_utc,
)
from nyc_live.feeds.cameras import CameraFrameSource
from nyc_live.http import make_client
from nyc_live.services import Services
from nyc_live.store import Store
from nyc_mcp.server import create_server

CAM_BASE = "https://cams.test/api/cameras"
CAM_ONLINE = "0a1b2c3d-0000-4000-8000-0000000000aa"
CAM_OFFLINE = "0a1b2c3d-0000-4000-8000-0000000000bb"
TIMES_SQ = (40.7580, -73.9855)
BATTERY = (40.7033, -74.0170)


def fake_jpeg(width: int = 352, height: int = 240) -> bytes:
    """Structurally valid JPEG header bytes (SOI, SOF0, EOI). Not a real image."""
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + sof0 + b"\xff\xd9"


class StaticAdapter[RecordT: BaseModel]:
    """FeedAdapter that serves a fixed record list."""

    def __init__(self, name: FeedName, records: list[RecordT], *, configured: bool = True):
        self.name = name
        self.ttl = DEFAULT_TTL[name]
        self.records = records
        self.configured = configured
        self.fetches = 0

    def is_configured(self) -> bool:
        return self.configured

    async def fetch(self) -> Snapshot[RecordT]:
        self.fetches += 1
        t = now_utc()
        return Snapshot[RecordT](
            feed=self.name,
            fetched_at=t,
            stale_after=t + self.ttl,
            source_url=f"https://fake.test/{self.name.value}",
            records=list(self.records),
        )


class DownAdapter:
    """FeedAdapter whose upstream is down."""

    def __init__(self, name: FeedName, *, kind: ErrorKind = ErrorKind.UPSTREAM_HTTP):
        self.name = name
        self.ttl = DEFAULT_TTL[name]
        self.kind = kind

    def is_configured(self) -> bool:
        return True

    async def fetch(self) -> Snapshot[Any]:
        raise FeedUnavailable(
            self.name,
            f"HTTP 403 from https://fake.test/{self.name.value}",
            kind=self.kind,
            url=f"https://fake.test/{self.name.value}",
            upstream_status=403,
        )


def cameras() -> list[Camera]:
    return [
        Camera(
            id=CAM_ONLINE,
            source=CameraSource.NYC_DOT,
            name="7 Ave @ 42 St",
            is_online=True,
            image_url=f"{CAM_BASE}/{CAM_ONLINE}/image",
            lat=TIMES_SQ[0],
            lon=TIMES_SQ[1],
        ),
        Camera(
            id=CAM_OFFLINE,
            source=CameraSource.NYC_DOT,
            name="Broadway @ 43 St",
            is_online=False,
            image_url=f"{CAM_BASE}/{CAM_OFFLINE}/image",
            lat=40.7590,
            lon=-73.9860,
        ),
        Camera(
            id="0a1b2c3d-0000-4000-8000-0000000000cc",
            source=CameraSource.NYC_DOT,
            name="State St @ Battery Pl",
            is_online=True,
            image_url=f"{CAM_BASE}/cc/image",
            lat=BATTERY[0],
            lon=BATTERY[1],
        ),
    ]


def stops() -> list[SubwayStop]:
    return [
        SubwayStop(stop_id="127", name="Times Sq-42 St", lat=40.75529, lon=-73.987495, routes=["1", "2", "3"]),
        SubwayStop(stop_id="127N", name="Times Sq-42 St", lat=40.75529, lon=-73.987495, parent_station="127", routes=["1", "2", "3"]),
        SubwayStop(stop_id="127S", name="Times Sq-42 St", lat=40.75529, lon=-73.987495, parent_station="127", routes=["1", "2", "3"]),
        SubwayStop(stop_id="142", name="South Ferry", lat=40.702068, lon=-74.013664, routes=["1"]),
        SubwayStop(stop_id="142N", name="South Ferry", lat=40.702068, lon=-74.013664, parent_station="142", routes=["1"]),
    ]  # fmt: skip


def trips(now: datetime) -> list[SubwayTrip]:
    return [
        SubwayTrip(
            trip_id="t1",
            route_id="1",
            feed_slug="gtfs",
            direction="N",
            stop_times=[
                StopTimeUpdate(stop_id="142N", arrival=now + timedelta(minutes=2)),
                StopTimeUpdate(stop_id="127N", arrival=now + timedelta(minutes=9)),
            ],
        ),
        SubwayTrip(
            trip_id="t2",
            route_id="2",
            feed_slug="gtfs",
            direction="S",
            stop_times=[
                StopTimeUpdate(stop_id="127S", arrival=now + timedelta(minutes=4)),
                StopTimeUpdate(stop_id="127S", arrival=now - timedelta(minutes=1)),
            ],
        ),
        SubwayTrip(
            trip_id="t3",
            route_id="3",
            feed_slug="gtfs",
            direction="N",
            stop_times=[StopTimeUpdate(stop_id="127N", arrival=now + timedelta(hours=2))],
        ),
    ]


def alerts() -> list[SubwayAlert]:
    return [
        SubwayAlert(id="a1", header="1 trains skip South Ferry", routes=["1"], stop_ids=["142"]),
        SubwayAlert(id="a2", header="G delays", routes=["G"], stop_ids=[]),
    ]


def bikes() -> list[BikeStation]:
    return [
        BikeStation(
            station_id="s1",
            name="W 41 St & 8 Ave",
            capacity=40,
            bikes_available=5,
            ebikes_available=2,
            docks_available=35,
            is_renting=True,
            is_returning=True,
            lat=40.7565,
            lon=-73.9900,
        ),
        BikeStation(
            station_id="s2",
            name="Battery Pl & State St",
            capacity=20,
            bikes_available=0,
            ebikes_available=0,
            docks_available=20,
            is_renting=True,
            is_returning=True,
            lat=BATTERY[0],
            lon=BATTERY[1],
        ),
    ]


def requests_311(now: datetime) -> list[ServiceRequest]:
    return [
        ServiceRequest(
            unique_key="1",
            created_at=now - timedelta(hours=1),
            closed_at=None,
            agency="NYPD",
            complaint_type="Noise - Street/Sidewalk",
            descriptor="Loud Music/Party",
            status="Open",
            borough="MANHATTAN",
            lat=40.7575,
            lon=-73.9860,
        ),
        ServiceRequest(
            unique_key="2",
            created_at=now - timedelta(hours=2),
            closed_at=None,
            agency="DOT",
            complaint_type="Street Condition",
            descriptor="Pothole",
            status="Open",
            borough="MANHATTAN",
            lat=None,
            lon=None,
        ),
    ]


def weather(now: datetime) -> list[WeatherReport]:
    return [
        WeatherReport(
            station_id="KNYC",
            station_name="New York City, Central Park",
            lat=40.7789,
            lon=-73.9692,
            observation=WeatherObservation(observed_at=now - timedelta(minutes=20), text="Clear", temperature_c=21.0),
        )
    ]  # fmt: skip


@pytest.fixture
def cam_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"dot_cameras_base": CAM_BASE, "http_retries": 0})


@pytest.fixture
async def client(cam_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(cam_settings) as c:
        yield c


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{CAM_BASE}/{CAM_ONLINE}/image").mock(
            return_value=httpx.Response(
                200, content=fake_jpeg(), headers={"content-type": "image/jpeg"}
            )
        )
        router.get(f"{CAM_BASE}/{CAM_OFFLINE}/image").mock(return_value=httpx.Response(404))
        yield router  # fmt: skip


@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def make_registry(
    now: datetime,
    *,
    down: frozenset[FeedName] | set[FeedName] = frozenset(),
    store: Store | None = None,
) -> FeedRegistry:
    adapters: list[Any] = [
        StaticAdapter(FeedName.DOT_CAMERAS, cameras()),
        StaticAdapter(FeedName.MTA_SUBWAY, trips(now)),
        StaticAdapter(FeedName.MTA_SUBWAY_ALERTS, alerts()),
        StaticAdapter(FeedName.MTA_SUBWAY_STOPS, stops()),
        StaticAdapter(FeedName.CITIBIKE, bikes()),
        StaticAdapter(FeedName.NYC_311, requests_311(now)),
        StaticAdapter(FeedName.WEATHER, weather(now)),
        StaticAdapter(FeedName.MTA_BUS, [], configured=False),
        StaticAdapter(FeedName.NY511_CAMERAS, [], configured=False),
    ]
    adapters = [DownAdapter(a.name) if a.name in down else a for a in adapters]
    return FeedRegistry(CachedFeed(a, store=store) for a in adapters)  # fmt: skip


@pytest.fixture
def services(
    cam_settings: Settings, client: httpx.AsyncClient, store: Store, mock: respx.MockRouter
) -> Services:
    registry = make_registry(now_utc(), store=store)
    frames = CameraFrameSource(client=client, settings=cam_settings)
    return Services(
        settings=cam_settings, client=client, registry=registry, frames=frames, store=store
    )


@pytest.fixture
def server(services: Services) -> FastMCP:
    return create_server(services)


@pytest.fixture
async def mcp(server: FastMCP) -> AsyncIterator[Client]:
    async with Client(server) as c:
        yield c
