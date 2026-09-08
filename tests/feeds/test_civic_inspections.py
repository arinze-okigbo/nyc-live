"""InspectionsAdapter: live shape test, fixture replay, and offline behaviour tests."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from nyc_live.config import Settings
from nyc_live.contracts import (
    ErrorKind,
    FeedName,
    FeedUnavailable,
    RestaurantInspection,
    now_utc,
)
from nyc_live.feeds.civic import InspectionsAdapter
from nyc_live.feeds.socrata import DATASET_INSPECTIONS, NYC_TZ, soda_resource_url
from nyc_live.http import make_client

FIXTURE = Path(__file__).parent.parent / "fixtures" / "civic" / "inspections_page.json"


def _load_fixture(path: Path) -> Any:
    if not path.exists():
        pytest.skip(f"fixture not recorded yet: {path}")
    return json.loads(path.read_text())


def _row(camis: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "camis": camis,
        "dba": "TEST DINER",
        "boro": "Manhattan",
        "building": "1",
        "street": "BROADWAY",
        "zipcode": "10001",
        "cuisine_description": "American",
        "inspection_date": "2026-09-01T00:00:00.000",
        "action": "Violations were cited in the following area(s).",
        "violation_code": "10F",
        "violation_description": "Non-food contact surface improperly constructed.",
        "critical_flag": "Not Critical",
        "score": "12",
        "grade": "A",
        "grade_date": "2026-09-01T00:00:00.000",
        "inspection_type": "Cycle Inspection / Initial Inspection",
        "latitude": "40.7484",
        "longitude": "-73.9857",
    }
    base.update(extra)
    return base


def _adapter(settings: Settings, **overrides: Any) -> tuple[InspectionsAdapter, httpx.AsyncClient]:
    client = make_client(settings)
    adapter = InspectionsAdapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    for k, v in overrides.items():
        setattr(adapter, k, v)
    return adapter, client


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_inspections_last_90_days(settings: Settings) -> None:
    async with make_client(settings) as client:
        snap = await InspectionsAdapter(client=client, settings=settings).fetch()
    assert snap.feed is FeedName.DOHMH_INSPECTIONS
    assert len(snap.records) > 100
    assert all(r.lat is not None and r.lon is not None for r in snap.records)
    cutoff = now_utc() - timedelta(days=91)
    dated = [r for r in snap.records if r.inspection_date is not None]
    assert len(dated) == len(snap.records)
    assert all(r.inspection_date is not None and r.inspection_date >= cutoff for r in dated)


# ---------------------------------------------------------------------------
# fixture replay
# ---------------------------------------------------------------------------


async def test_replay_inspections_fixture(settings: Settings) -> None:
    body = _load_fixture(FIXTURE)
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_INSPECTIONS)
    async with client, respx.mock(assert_all_called=True) as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json=body))
        snap = await adapter.fetch()
    assert 0 < len(snap.records) <= len(body)
    assert all(isinstance(r, RestaurantInspection) for r in snap.records)
    assert all(r.lat is not None and r.lon is not None for r in snap.records)
    assert all(
        r.inspection_date is None or r.inspection_date.utcoffset() == timedelta(0)
        for r in snap.records
    )


# ---------------------------------------------------------------------------
# behaviour (synthetic inline bodies)
# ---------------------------------------------------------------------------


async def test_inspections_where_is_90_days_and_geocoded(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_INSPECTIONS)
    async with client, respx.mock() as mock:
        route = mock.get(url).mock(return_value=httpx.Response(200, json=[_row("1")]))
        before = now_utc()
        snap = await adapter.fetch()
    params = httpx.QueryParams(route.calls.last.request.url.query)
    where = params["$where"]
    assert "latitude IS NOT NULL" in where
    assert "longitude IS NOT NULL" in where
    literal = where.split("'")[1]
    got = datetime.fromisoformat(literal)
    expected = datetime.fromisoformat(
        (before - timedelta(days=90)).astimezone(NYC_TZ).strftime("%Y-%m-%dT%H:%M:%S")
    )
    assert abs((got - expected).total_seconds()) < 5
    assert params["$order"].startswith("inspection_date DESC")
    rec = snap.records[0]
    assert rec.camis == "1"
    assert rec.score == 12
    assert rec.cuisine == "American"
    # 2026-09-01 midnight EDT -> 04:00 UTC
    assert rec.inspection_date == datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    assert rec.grade_date == datetime(2026, 9, 1, 4, 0, tzinfo=UTC)


async def test_inspections_drop_unlocated_and_out_of_bbox(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_INSPECTIONS)
    body = [
        _row("keep"),
        _row("nolat", latitude=None, longitude=None),
        _row("blank", latitude="", longitude=""),
        _row("zero", latitude="0", longitude="0"),
        _row("boston", latitude="42.3601", longitude="-71.0589"),
    ]
    with caplog.at_level(logging.INFO, logger="nyc_live.feeds.socrata"):
        async with client, respx.mock() as mock:
            mock.get(url).mock(return_value=httpx.Response(200, json=body))
            snap = await adapter.fetch()
    assert [r.camis for r in snap.records] == ["keep"]
    messages = [rec.message for rec in caplog.records]
    assert any("dropped 2 rows without coordinates" in m for m in messages)
    assert any("dropped 2 rows outside the NYC bbox" in m for m in messages)


async def test_inspections_nulls_stay_none(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_INSPECTIONS)
    body = [
        {
            "camis": "9",
            "inspection_date": "2026-09-01T00:00:00.000",
            "latitude": "40.7484",
            "longitude": "-73.9857",
            "score": "",
        }
    ]
    async with client, respx.mock() as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json=body))
        snap = await adapter.fetch()
    rec = snap.records[0]
    assert rec.dba is None
    assert rec.score is None
    assert rec.grade is None
    assert rec.grade_date is None
    assert rec.violation_code is None


async def test_inspections_page_while_full(settings: Settings) -> None:
    adapter, client = _adapter(settings, page_size=2)
    url = soda_resource_url(settings, DATASET_INSPECTIONS)

    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(httpx.QueryParams(request.url.query)["$offset"])
        if offset == 0:
            return httpx.Response(200, json=[_row("a"), _row("b")])
        return httpx.Response(200, json=[_row("c")])

    async with client, respx.mock() as mock:
        route = mock.get(url).mock(side_effect=respond)
        snap = await adapter.fetch()
    assert route.call_count == 2
    assert [r.camis for r in snap.records] == ["a", "b", "c"]


async def test_inspections_all_unlocated_is_loud(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_INSPECTIONS)
    async with client, respx.mock() as mock:
        mock.get(url).mock(
            return_value=httpx.Response(200, json=[_row("x", latitude=None, longitude=None)])
        )
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE


async def test_inspections_404_is_not_found(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_INSPECTIONS)
    async with client, respx.mock() as mock:
        mock.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert exc_info.value.kind is ErrorKind.NOT_FOUND


async def test_inspections_5xx_retries(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_INSPECTIONS)
    async with client, respx.mock() as mock:
        route = mock.get(url).mock(
            side_effect=[httpx.Response(500), httpx.Response(200, json=[_row("1")])]
        )
        snap = await adapter.fetch()
    assert route.call_count == 2
    assert len(snap.records) == 1
