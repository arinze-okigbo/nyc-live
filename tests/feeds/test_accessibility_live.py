"""Live tests against the real MTA elevator/escalator outage feed. Skipped unless
NYC_LIVE_TESTS=1.

Thresholds are deliberately well below what was measured on 2026-09-09 (136 outages,
58 currently out / 78 upcoming, 133 placed = 97.8%) so a normal day never fails the
gate, while a feed that has genuinely changed shape does.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest

from nyc_live.config import Settings
from nyc_live.contracts import ElevatorEquipmentType, FeedName, now_utc
from nyc_live.feeds.accessibility import ENE_SLUG, ElevatorOutagesAdapter, reset_station_index
from nyc_live.feeds.transit import reset_raw_cache
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import make_client

pytestmark = pytest.mark.live

EQUIPMENT_ID_RE = re.compile(r"^(?:EL|ES)\d+[A-Z]?$")
# NYCT route ids as `trainno` writes them, plus the LIRR token MTA really publishes.
ROUTE_RE = re.compile(r"^(?:[1-7]|[ABCDEFGJLMNQRSWZ]|SIR|LIRR|MNR)$")


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    reset_raw_cache()
    reset_station_index()
    async with make_client(settings) as c:
        yield c


async def test_live_elevator_outages(client: httpx.AsyncClient, settings: Settings) -> None:
    snap = await ElevatorOutagesAdapter(client=client, settings=settings).fetch()
    now = now_utc()

    assert snap.feed is FeedName.MTA_ELEVATOR_OUTAGES
    assert snap.source_url.endswith(ENE_SLUG)
    assert now - timedelta(minutes=2) <= snap.fetched_at <= now
    assert snap.stale_after == snap.fetched_at + timedelta(minutes=5)

    # the system has ~100-200 open equipment outages at any hour; 136 on 2026-09-09
    assert len(snap.records) >= 40, f"only {len(snap.records)} outages, feed shape changed?"

    bad_ids = sorted(
        {o.equipment_id for o in snap.records if not EQUIPMENT_ID_RE.match(o.equipment_id)}
    )
    assert not bad_ids, f"unexpected equipment ids: {bad_ids}"
    assert all(o.station.strip() for o in snap.records)
    assert set(o.equipment_type for o in snap.records) == {
        ElevatorEquipmentType.ELEVATOR,
        ElevatorEquipmentType.ESCALATOR,
    }
    # elevators dominate (108/136 on 2026-09-09) and most are ADA-relevant
    elevators = [o for o in snap.records if o.equipment_type is ElevatorEquipmentType.ELEVATOR]
    assert len(elevators) > len(snap.records) / 2
    assert sum(o.is_ada for o in snap.records) > 0.5 * len(snap.records)

    routes = {r for o in snap.records for r in o.routes}
    bad_routes = sorted(r for r in routes if not ROUTE_RE.match(r))
    assert not bad_routes, f"unexpected trainno tokens: {bad_routes}"
    assert len(routes) >= 15
    # trainno really is multi-route: a station complex lists every line it serves
    assert max(len(o.routes) for o in snap.records) >= 4
    assert all(o.routes for o in snap.records)


async def test_live_upcoming_and_active_outages_are_both_present_and_distinct(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    snap = await ElevatorOutagesAdapter(client=client, settings=settings).fetch()
    now = now_utc()

    upcoming = [o for o in snap.records if o.is_upcoming]
    active = [o for o in snap.records if not o.is_upcoming]
    # both kinds ship in the same payload (78 / 58 on 2026-09-09); conflating them would
    # tell a rider a working elevator is broken
    assert upcoming and active
    assert len(upcoming) + len(active) == len(snap.records)

    # a scheduled outage starts in the future; a current one started in the past
    started_later = [o for o in upcoming if o.outage_started is not None and o.outage_started > now]
    assert len(started_later) > 0.8 * len(upcoming)
    started_earlier = [
        o for o in active if o.outage_started is not None and o.outage_started <= now
    ]
    assert len(started_earlier) > 0.8 * len(active)

    # timestamps are aware UTC and plausible, not 12-hour clock mangled into noon/midnight
    starts = [o.outage_started for o in snap.records if o.outage_started is not None]
    assert len(starts) > 0.9 * len(snap.records)
    assert all(s.tzinfo is not None for s in starts)
    assert len({s.hour for s in starts}) >= 6
    returns = [o.estimated_return for o in snap.records if o.estimated_return is not None]
    assert any(r > now for r in returns)


async def test_live_outages_are_placed_conservatively(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    snap = await ElevatorOutagesAdapter(client=client, settings=settings).fetch()

    placed = [o for o in snap.records if o.lat is not None]
    # measured 133/136 = 97.8% on 2026-09-09; the floor allows for upstream renames
    assert len(placed) / len(snap.records) >= 0.85, (
        f"only {len(placed)}/{len(snap.records)} outages matched a GTFS station; "
        f"unplaced: {sorted({o.station for o in snap.records if o.lat is None})}"
    )
    assert all(o.lon is not None for o in placed)
    assert all(in_nyc_bbox(o.lat, o.lon) for o in placed if o.lat is not None and o.lon is not None)
    # an unplaced outage is unplaced on both axes -- never half a coordinate
    assert all(o.lon is None for o in snap.records if o.lat is None)
    # The match is a pure function of (station name, routes), so the same pair always
    # resolves to the same point. Keyed on the pair and NOT on the name alone, because
    # a bare name genuinely does not identify a station: live on 2026-09-09, "125 St"
    # on the 1 and "125 St" on A/C/B/D are 600 m apart, "Church Av" on B/Q and on 2/5
    # are 1.1 km apart, and resolving them to different points is the correct answer.
    by_key: dict[tuple[str, tuple[str, ...]], set[tuple[float, float]]] = {}
    for o in placed:
        assert o.lat is not None and o.lon is not None
        by_key.setdefault((o.station, tuple(o.routes)), set()).add((o.lat, o.lon))
    assert all(len(points) == 1 for points in by_key.values())
