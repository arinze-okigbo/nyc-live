"""Live tests against the real MTA upstreams. Skipped unless NYC_LIVE_TESTS=1."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest

from nyc_live.config import Settings
from nyc_live.contracts import FeedName, now_utc
from nyc_live.feeds.transit import (
    SUBWAY_FEED_SLUGS,
    SubwayAlertsAdapter,
    SubwayShapesAdapter,
    SubwayStopsAdapter,
    SubwayTripsAdapter,
    reset_raw_cache,
)
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import make_client

pytestmark = pytest.mark.live

# NYCT route ids: 1-7 (+ 6X/7X), lettered lines (+ FX), shuttles GS/FS/H, SI.
NYCT_ROUTE_RE = re.compile(r"^(?:[1-7]X?|[ABCDEFGJLMNQRWZ]X?|GS|FS|H|S|SI|SS)$")


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    reset_raw_cache()
    async with make_client(settings) as c:
        yield c


async def test_live_subway_trips(client: httpx.AsyncClient, settings: Settings) -> None:
    snap = await SubwayTripsAdapter(client=client, settings=settings).fetch()
    now = now_utc()
    assert snap.feed is FeedName.MTA_SUBWAY
    # the subway runs 24/7; even at 3 AM there are well over 50 active trips system-wide
    assert len(snap.records) >= 50
    assert {t.feed_slug for t in snap.records} == set(SUBWAY_FEED_SLUGS)
    routes = {t.route_id for t in snap.records}
    bad = sorted(r for r in routes if not NYCT_ROUTE_RE.match(r))
    assert not bad, f"unexpected route ids: {bad}"
    assert len(routes) >= 10
    assert now - timedelta(minutes=2) <= snap.fetched_at <= now
    assert snap.upstream_generated_at is not None
    assert now - snap.upstream_generated_at < timedelta(minutes=10)
    with_dir = sum(t.direction is not None for t in snap.records)
    assert with_dir / len(snap.records) > 0.9
    assert sum(bool(t.stop_times) for t in snap.records) > 0.5 * len(snap.records)
    vehicles = [t.vehicle for t in snap.records if t.vehicle is not None]
    assert len(vehicles) >= 20
    assert any(v.current_stop_id for v in vehicles)
    future_arrivals = [
        s.arrival
        for t in snap.records
        for s in t.stop_times
        if s.arrival is not None and s.arrival > now - timedelta(minutes=1)
    ]
    assert future_arrivals


async def test_live_subway_alerts(client: httpx.AsyncClient, settings: Settings) -> None:
    snap = await SubwayAlertsAdapter(client=client, settings=settings).fetch()
    now = now_utc()
    assert snap.feed is FeedName.MTA_SUBWAY_ALERTS
    assert len(snap.records) >= 1  # MTA always has planned-work alerts posted
    assert all(a.id and a.header.strip() for a in snap.records)
    routes = {r for a in snap.records for r in a.routes}
    assert routes and all(NYCT_ROUTE_RE.match(r) for r in routes), routes
    assert snap.upstream_generated_at is not None
    assert now - snap.upstream_generated_at < timedelta(hours=1)


async def test_live_subway_stops(client: httpx.AsyncClient, settings: Settings) -> None:
    snap = await SubwayStopsAdapter(client=client, settings=settings).fetch()
    assert snap.feed is FeedName.MTA_SUBWAY_STOPS
    assert len(snap.records) >= 1400  # ~1490 rows in the 2026-08-27 zip
    assert all(in_nyc_bbox(s.lat, s.lon) for s in snap.records)
    by_id = {s.stop_id: s for s in snap.records}
    assert by_id["127"].name == "Times Sq-42 St" and by_id["127"].parent_station is None
    assert by_id["127N"].parent_station == "127"
    assert {"1", "2", "3"} <= set(by_id["127"].routes)
    parents = {s.parent_station for s in snap.records if s.parent_station}
    assert parents <= set(by_id)
    assert sum(bool(s.routes) for s in snap.records) > 0.9 * len(snap.records)
    assert snap.upstream_generated_at is not None  # S3 Last-Modified header


async def test_live_subway_shapes(client: httpx.AsyncClient, settings: Settings) -> None:
    snap = await SubwayShapesAdapter(client=client, settings=settings).fetch()
    assert snap.feed is FeedName.MTA_SUBWAY_SHAPES
    # 2026-09-08 bundle: 257 shape_ids across 29 routes, 150,744 points total
    assert len(snap.records) >= 200
    routes = {s.route_id for s in snap.records}
    bad = sorted(r for r in routes if not NYCT_ROUTE_RE.match(r))
    assert not bad, f"unexpected route ids: {bad}"
    assert len(routes) >= 20
    by_route: dict[str, int] = {}
    for s in snap.records:
        by_route[s.route_id] = by_route.get(s.route_id, 0) + 1
    assert all(count >= 2 for count in by_route.values())  # every route has multiple shapes
    assert all(len(s.points) >= 2 for s in snap.records)
    # the adapter only drops a shape whose points are entirely outside NYC_BBOX, so every
    # surviving shape has at least one point inside it (not necessarily every point)
    assert all(any(in_nyc_bbox(lat, lon) for lat, lon in s.points) for s in snap.records)
    with_dir = sum(s.direction is not None for s in snap.records)
    assert with_dir / len(snap.records) > 0.9
    assert snap.upstream_generated_at is not None  # S3 Last-Modified header
