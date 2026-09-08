"""Fixtures for nyc-dash tests: fake adapters behind the real cache, no network.

The records below are synthetic inputs for exercising the API, the SSE stream and the
degradation paths. They are hand-built, not recorded upstream responses, and they are
never presented anywhere as real NYC data.

Three adapter behaviours cover the three envelope statuses the dashboard must handle:

* `FakeAdapter`                     -> "fresh"
* `FakeAdapter(born_stale=True)` primed once, then `.fail = True` -> "stale"
  (the cache keeps the last good snapshot and attaches the refresh error)
* `FakeAdapter(fail=True)` from the start -> "error" (no snapshot ever succeeded)
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from nyc_dash.app import create_app
from nyc_live.cache import CachedFeed, FeedRegistry
from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    BikeStation,
    Camera,
    CameraFrame,
    CameraSource,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    RestaurantInspection,
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
from nyc_live.services import Services
from nyc_live.store import Store

TIMES_SQ = (40.7580, -73.9855)
BATTERY = (40.7033, -74.0170)

CAM_ONLINE = "0a1b2c3d-0000-4000-8000-0000000000aa"
CAM_OFFLINE = "0a1b2c3d-0000-4000-8000-0000000000bb"

FEEDS_IN_REGISTRY: tuple[FeedName, ...] = (
    FeedName.DOT_CAMERAS,
    FeedName.MTA_SUBWAY,
    FeedName.MTA_SUBWAY_ALERTS,
    FeedName.MTA_SUBWAY_STOPS,
    FeedName.CITIBIKE,
    FeedName.NYC_311,
    FeedName.DOHMH_INSPECTIONS,
    FeedName.WEATHER,
    FeedName.MTA_BUS,
    FeedName.NY511_CAMERAS,
)


class FakeAdapter[RecordT: BaseModel]:
    """FeedAdapter serving a fixed record list, switchable to failing at any time."""

    def __init__(
        self,
        name: FeedName,
        records: list[RecordT],
        *,
        configured: bool = True,
        fail: bool = False,
        born_stale: bool = False,
    ) -> None:
        self.name = name
        self.ttl = DEFAULT_TTL[name]
        self.records = records
        self.configured = configured
        self.fail = fail
        self.born_stale = born_stale
        self.fetches = 0

    def is_configured(self) -> bool:
        return self.configured

    async def fetch(self) -> Snapshot[RecordT]:
        self.fetches += 1
        if self.fail:
            raise FeedUnavailable(
                self.name,
                f"HTTP 403 from https://fake.test/{self.name.value}",
                kind=ErrorKind.UPSTREAM_HTTP,
                url=f"https://fake.test/{self.name.value}",
                upstream_status=403,
            )
        t = now_utc() - (self.ttl + timedelta(seconds=1) if self.born_stale else timedelta(0))
        return Snapshot[RecordT](
            feed=self.name,
            fetched_at=t,
            stale_after=t + self.ttl,
            source_url=f"https://fake.test/{self.name.value}",
            records=list(self.records),
        )


class NoFrames:
    """FrameSource stub: the dashboard never asks for JPEGs, but Services requires one."""

    async def get_frame(self, camera_id: str) -> CameraFrame:
        raise FeedUnavailable(
            FeedName.DOT_CAMERA_FRAMES,
            f"no frame source in tests (camera {camera_id})",
            kind=ErrorKind.NOT_FOUND,
        )


# --------------------------------------------------------------------------- records


def cameras() -> list[Camera]:
    return [
        Camera(
            id=CAM_ONLINE,
            source=CameraSource.NYC_DOT,
            name="7 Ave @ 42 St",
            is_online=True,
            image_url=f"https://cams.test/{CAM_ONLINE}/image",
            lat=TIMES_SQ[0],
            lon=TIMES_SQ[1],
        ),
        Camera(
            id=CAM_OFFLINE,
            source=CameraSource.NYC_DOT,
            name="State St @ Battery Pl",
            is_online=False,
            image_url=f"https://cams.test/{CAM_OFFLINE}/image",
            lat=BATTERY[0],
            lon=BATTERY[1],
        ),
    ]


def stops() -> list[SubwayStop]:
    return [
        SubwayStop(stop_id="127", name="Times Sq-42 St", lat=40.75529, lon=-73.987495, routes=["1", "2", "3"]),
        SubwayStop(stop_id="127N", name="Times Sq-42 St", lat=40.75529, lon=-73.987495, parent_station="127", routes=["1", "2", "3"]),
        SubwayStop(stop_id="127S", name="Times Sq-42 St", lat=40.75529, lon=-73.987495, parent_station="127", routes=["1", "2", "3"]),
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
            stop_times=[StopTimeUpdate(stop_id="127S", arrival=now + timedelta(minutes=4))],
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
            lat=BATTERY[0],
            lon=BATTERY[1],
        ),
    ]


def inspections() -> list[RestaurantInspection]:
    return [
        RestaurantInspection(
            camis="50000001",
            dba="Test Diner",
            boro="Manhattan",
            inspection_date=datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
            lat=40.7570,
            lon=-73.9870,
        )
    ]


def weather(now: datetime) -> list[WeatherReport]:
    return [
        WeatherReport(
            station_id="KNYC",
            station_name="New York City, Central Park",
            lat=40.7789,
            lon=-73.9692,
            observation=WeatherObservation(
                observed_at=now - timedelta(minutes=20), text="Clear", temperature_c=21.0
            ),
        )
    ]


# --------------------------------------------------------------------------- fixtures


class Fakes:
    """The fake registry plus its adapters, so a test can flip one feed to failing."""

    def __init__(self, adapters: dict[FeedName, Any], registry: FeedRegistry) -> None:
        self.adapters = adapters
        self.registry = registry

    def kill(self, feed: FeedName) -> None:
        self.adapters[feed].fail = True


def build_fakes(
    now: datetime,
    *,
    down: frozenset[FeedName] | set[FeedName] = frozenset(),
    born_stale: frozenset[FeedName] | set[FeedName] = frozenset(),
    missing: frozenset[FeedName] | set[FeedName] = frozenset(),
    store: Store | None = None,
) -> Fakes:
    records: dict[FeedName, list[Any]] = {
        FeedName.DOT_CAMERAS: cameras(),
        FeedName.MTA_SUBWAY: trips(now),
        FeedName.MTA_SUBWAY_ALERTS: alerts(),
        FeedName.MTA_SUBWAY_STOPS: stops(),
        FeedName.CITIBIKE: bikes(),
        FeedName.NYC_311: requests_311(now),
        FeedName.DOHMH_INSPECTIONS: inspections(),
        FeedName.WEATHER: weather(now),
        FeedName.MTA_BUS: [],
        FeedName.NY511_CAMERAS: [],
    }
    key_gated = {FeedName.MTA_BUS, FeedName.NY511_CAMERAS}
    adapters: dict[FeedName, Any] = {
        name: FakeAdapter(
            name,
            records[name],
            configured=name not in key_gated,
            fail=name in down,
            born_stale=name in born_stale,
        )
        for name in FEEDS_IN_REGISTRY
        if name not in missing
    }
    registry = FeedRegistry(CachedFeed(a, store=store) for a in adapters.values())
    return Fakes(adapters, registry)


@pytest.fixture
def frozen_now() -> datetime:
    return now_utc()


@pytest.fixture
def fakes(frozen_now: datetime, store: Store) -> Fakes:
    return build_fakes(frozen_now, store=store)


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    """Shared client handed to Services. No dashboard code path ever uses it."""
    return httpx.AsyncClient()


def make_services(
    settings: Settings, client: httpx.AsyncClient, fakes: Fakes, store: Store | None
) -> Services:
    return Services(
        settings=settings, client=client, registry=fakes.registry, frames=NoFrames(), store=store
    )


@pytest.fixture
def services(
    settings: Settings, http_client: httpx.AsyncClient, fakes: Fakes, store: Store
) -> Services:
    return make_services(settings, http_client, fakes, store)


@pytest.fixture
def app(services: Services) -> FastAPI:
    return create_app(services)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
