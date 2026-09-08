"""Live Citi Bike GBFS test. Skipped unless NYC_LIVE_TESTS=1 (see tests/conftest.py)."""

from __future__ import annotations

import pytest

from nyc_live.config import Settings
from nyc_live.contracts import FeedName
from nyc_live.feeds.micromobility import CitiBikeAdapter
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import make_client

pytestmark = pytest.mark.live


async def test_live_citibike_station_snapshot(settings: Settings) -> None:
    async with make_client(settings) as client:
        snap = await CitiBikeAdapter(client=client, settings=settings).fetch()

    assert snap.feed is FeedName.CITIBIKE
    assert snap.source_url == settings.citibike_gbfs_root
    assert len(snap.records) > 1000, f"only {len(snap.records)} stations"

    with_capacity = sum(1 for r in snap.records if r.capacity is not None and r.capacity > 0)
    assert with_capacity / len(snap.records) > 0.9, (
        f"only {with_capacity}/{len(snap.records)} stations report capacity > 0"
    )
    assert all(in_nyc_bbox(r.lat, r.lon) for r in snap.records)
    assert len({r.station_id for r in snap.records}) == len(snap.records)
    assert all(r.bikes_available >= 0 and r.docks_available >= 0 for r in snap.records)

    assert snap.stale_after > snap.fetched_at
    assert snap.upstream_generated_at is not None
    assert snap.upstream_generated_at <= snap.fetched_at
    assert snap.latency_ms is not None and snap.latency_ms > 0
