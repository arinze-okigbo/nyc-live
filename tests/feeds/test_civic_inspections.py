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

FIXTURES = Path(__file__).parent.parent / "fixtures" / "civic"
FIXTURE = FIXTURES / "inspections_page.json"
SAME_DAY_FIXTURE = FIXTURES / "inspections_same_day_graded_and_admin.json"


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
    camis = [r.camis for r in snap.records]
    assert len(set(camis)) == len(camis), "the live snapshot must be one record per restaurant"
    assert sum(len(r.violations) for r in snap.records) > len(snap.records), (
        "collapsing must keep the discarded rows' violations, not drop them"
    )
    assert all(
        r.violation_code in {v.code for v in r.violations}
        for r in snap.records
        if r.violation_code is not None
    )


@pytest.mark.live
async def test_live_inspections_are_stable_across_two_fetches(settings: Settings) -> None:
    """The collapsed row must not flap between violations on refresh.

    Two fetches a TTL apart would be slow, so this reuses one adapter's rows via a
    second adapter instance; both hit the real dataset and must agree on the winner
    for every restaurant present in both snapshots.
    """
    async with make_client(settings) as client:
        first = await InspectionsAdapter(client=client, settings=settings).fetch()
        second = await InspectionsAdapter(client=client, settings=settings).fetch()
    left = {r.camis: r for r in first.records}
    right = {r.camis: r for r in second.records}
    shared = left.keys() & right.keys()
    assert len(shared) > 100
    differing = [c for c in shared if left[c] != right[c]]
    assert not differing, f"{len(differing)} restaurants changed winning row between fetches"


# ---------------------------------------------------------------------------
# fixture replay
# ---------------------------------------------------------------------------


async def _replay(settings: Settings, body: Any) -> list[RestaurantInspection]:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_INSPECTIONS)
    async with client, respx.mock(assert_all_called=True) as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json=body))
        snap = await adapter.fetch()
    return snap.records


async def test_replay_inspections_fixture(settings: Settings) -> None:
    body = _load_fixture(FIXTURE)
    records = await _replay(settings, body)
    assert 0 < len(records) <= len(body)
    assert all(isinstance(r, RestaurantInspection) for r in records)
    assert all(r.lat is not None and r.lon is not None for r in records)
    assert all(
        r.inspection_date is None or r.inspection_date.utcoffset() == timedelta(0) for r in records
    )


async def test_replay_fixture_is_one_record_per_restaurant(settings: Settings) -> None:
    """The recorded page is 40 violation rows for 12 restaurants; the snapshot is 12 records."""
    body = _load_fixture(FIXTURE)
    fixture_camis = {row["camis"] for row in body}
    assert len(fixture_camis) < len(body), "fixture must contain duplicate camis to be a regression"
    records = await _replay(settings, body)
    camis = [r.camis for r in records]
    assert len(camis) == len(set(camis))
    assert set(camis) == fixture_camis, "collapsing must not lose a restaurant"


async def test_replay_fixture_keeps_the_most_critical_violation(settings: Settings) -> None:
    """Within one visit the surviving row is the Critical violation, lowest code first."""
    body = _load_fixture(FIXTURE)
    records = {r.camis: r for r in await _replay(settings, body)}
    by_camis: dict[str, list[dict[str, Any]]] = {}
    for row in body:
        by_camis.setdefault(row["camis"], []).append(row)
    for camis, rows in by_camis.items():
        critical = sorted(
            (r for r in rows if r.get("critical_flag") == "Critical"),
            key=lambda r: r.get("violation_code") or "",
        )
        if not critical:
            continue
        assert records[camis].violation_code == critical[0]["violation_code"]
        assert records[camis].critical_flag == "Critical"


async def test_replay_fixture_carries_every_violation_of_the_winning_visit(
    settings: Settings,
) -> None:
    """No information is destroyed: each record's `violations` covers all its rows."""
    body = _load_fixture(FIXTURE)
    records = {r.camis: r for r in await _replay(settings, body)}
    by_camis: dict[str, list[dict[str, Any]]] = {}
    for row in body:
        by_camis.setdefault(row["camis"], []).append(row)
    for camis, rows in by_camis.items():
        record = records[camis]
        same_visit = [r for r in rows if r["inspection_date"] == rows[0]["inspection_date"]]
        expected = {r["violation_code"] for r in same_visit if r.get("violation_code")}
        assert {v.code for v in record.violations} == expected
        if record.violation_code is None:
            continue  # a clean visit: nothing cited on the winning row to mirror
        # the mirrored top-level pair is part of the list, not excluded from it
        assert record.violation_code == record.violations[0].code
        assert record.violation_description == record.violations[0].description


async def test_replay_same_day_graded_inspection_beats_the_administrative_one(
    settings: Settings,
) -> None:
    """Real case: one restaurant, one date, two inspections -- only one of them graded.

    camis 50188367 was inspected twice on 2026-08-17: a Pre-permit (Operational)
    inspection that cited nothing (grade A, score 0, null violation_code) and an
    Administrative Miscellaneous one with three violations. Ranking on violation
    code alone would keep an administrative row and report the restaurant as
    ungraded with no score; the score/grade preference keeps the graded row.
    """
    body = _load_fixture(SAME_DAY_FIXTURE)
    records = await _replay(settings, body)
    assert len(records) == 1
    winner = records[0]
    assert winner.camis == "50188367"
    assert winner.grade == "A"
    assert winner.score == 0
    assert winner.inspection_type == "Pre-permit (Operational) / Initial Inspection"


async def test_replay_same_day_keeps_the_other_inspections_violations(settings: Settings) -> None:
    """The graded row wins but the three Administrative Miscellaneous violations survive.

    The winning row cited nothing itself, so this is the case where collapsing would
    otherwise throw away every violation the restaurant got that day.
    """
    body = _load_fixture(SAME_DAY_FIXTURE)
    winner = (await _replay(settings, body))[0]
    assert winner.violation_code is None
    assert [v.code for v in winner.violations] == ["19-07", "20-04", "20-08"]
    assert all(v.description for v in winner.violations)
    assert {v.critical_flag for v in winner.violations} == {"Not Critical"}


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


async def test_inspections_newest_inspection_date_wins(settings: Settings) -> None:
    body = [
        _row("A", inspection_date="2026-06-02T00:00:00.000", violation_code="02A"),
        _row("A", inspection_date="2026-09-01T00:00:00.000", violation_code="10F"),
        _row("A", inspection_date="2026-07-15T00:00:00.000", violation_code="04L"),
    ]
    records = await _replay(settings, body)
    assert [r.camis for r in records] == ["A"]
    assert records[0].inspection_date == datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    assert records[0].violation_code == "10F"


async def test_inspections_undated_row_loses_to_a_dated_one(settings: Settings) -> None:
    body = [
        _row("A", inspection_date=None, violation_code="02A"),
        _row("A", inspection_date="2026-09-01T00:00:00.000", violation_code="10F"),
    ]
    records = await _replay(settings, body)
    assert records[0].violation_code == "10F"


async def test_inspections_tiebreak_is_stable_under_row_order(settings: Settings) -> None:
    """Same rows in any upstream order must collapse to the same record.

    This is the anti-flapping guarantee: Socrata's ordering within a visit is not
    something the adapter may depend on, so the winner is decided client-side.
    """
    rows = [
        _row("A", critical_flag="Not Critical", violation_code="10F"),
        _row("A", critical_flag="Critical", violation_code="06C"),
        _row("A", critical_flag="Critical", violation_code="02G"),
        _row("A", critical_flag="Not Critical", violation_code="08A"),
    ]
    forward = await _replay(settings, rows)
    backward = await _replay(settings, list(reversed(rows)))
    rotated = await _replay(settings, rows[2:] + rows[:2])
    assert forward == backward == rotated
    assert forward[0].violation_code == "02G"
    assert forward[0].critical_flag == "Critical"


async def test_inspections_violations_are_ordered_by_the_same_rank(settings: Settings) -> None:
    """`violations` uses the tie-break order, so it cannot reshuffle between refreshes."""
    rows = [
        _row("A", critical_flag="Not Critical", violation_code="10F"),
        _row("A", critical_flag="Critical", violation_code="06C"),
        _row("A", critical_flag="Critical", violation_code="02G"),
        _row("A", critical_flag="Not Critical", violation_code="08A"),
    ]
    forward = await _replay(settings, rows)
    backward = await _replay(settings, list(reversed(rows)))
    assert forward == backward
    assert [v.code for v in forward[0].violations] == ["02G", "06C", "08A", "10F"]
    assert forward[0].violation_code == forward[0].violations[0].code


async def test_inspections_violations_are_scoped_to_the_winning_visit(settings: Settings) -> None:
    body = [
        _row("A", inspection_date="2026-09-01T00:00:00.000", violation_code="02G"),
        _row("A", inspection_date="2026-09-01T00:00:00.000", violation_code="10F"),
        _row("A", inspection_date="2026-06-02T00:00:00.000", violation_code="04L"),
    ]
    records = await _replay(settings, body)
    assert [v.code for v in records[0].violations] == ["02G", "10F"]


async def test_inspections_clean_visit_has_no_violations(settings: Settings) -> None:
    """A row citing nothing is DOHMH's "no violations" marker, not a violation."""
    body = [
        _row("A", violation_code=None, violation_description=None, critical_flag="Not Applicable")
    ]
    records = await _replay(settings, body)
    assert records[0].violations == []


async def test_inspections_duplicate_published_rows_list_one_violation(settings: Settings) -> None:
    """DOHMH publishes byte-identical duplicate rows (48 of 25,418 live on 2026-09-09)."""
    body = [_row("A", violation_code="02G"), _row("A", violation_code="02G")]
    records = await _replay(settings, body)
    assert [v.code for v in records[0].violations] == ["02G"]


async def test_inspections_logs_how_many_rows_it_collapsed(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    body = [_row("A", violation_code="02G"), _row("A", violation_code="10F"), _row("B")]
    with caplog.at_level(logging.INFO, logger="nyc_live.feeds.socrata"):
        records = await _replay(settings, body)
    assert len(records) == 2
    assert any(
        "collapsed 3 violation rows into 2 restaurants (1 duplicate rows dropped)" in r.getMessage()
        for r in caplog.records
    )


async def test_inspections_snapshot_is_ordered_newest_first(settings: Settings) -> None:
    body = [
        _row("B", inspection_date="2026-07-01T00:00:00.000"),
        _row("A", inspection_date="2026-09-01T00:00:00.000"),
        _row("C", inspection_date="2026-09-01T00:00:00.000"),
    ]
    records = await _replay(settings, body)
    assert [r.camis for r in records] == ["A", "C", "B"]


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
