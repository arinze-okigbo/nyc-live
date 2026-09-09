"""Nyc311Adapter: live shape test, fixture replay, and offline behaviour tests.

Behaviour tests build tiny synthetic rows inline; they exercise parsing/paging
logic and are not fixtures of upstream data. The replay test loads a recorded
fixture (see tests/fixtures/civic/RECORD.md) and skips if it is missing.
"""

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
from nyc_live.contracts import ErrorKind, FeedName, FeedUnavailable, ServiceRequest, now_utc
from nyc_live.feeds.civic import Nyc311Adapter
from nyc_live.feeds.socrata import (
    DATASET_311,
    NYC_311_STALENESS_CEILING,
    format_floating_timestamp,
    parse_floating_timestamp,
    soda_resource_url,
)
from nyc_live.http import make_client

FIXTURE = Path(__file__).parent.parent / "fixtures" / "civic" / "nyc311_page.json"


def _load_fixture(path: Path) -> Any:
    if not path.exists():
        pytest.skip(f"fixture not recorded yet: {path}")
    return json.loads(path.read_text())


def _row(key: str, created: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "unique_key": key,
        "created_date": created,
        "agency": "NYPD",
        "complaint_type": "Noise - Residential",
        "descriptor": "Loud Music/Party",
        "status": "In Progress",
        "borough": "BROOKLYN",
        "incident_zip": "11211",
        "latitude": "40.7128",
        "longitude": "-74.0060",
    }
    base.update(extra)
    return base


def _adapter(settings: Settings, **overrides: Any) -> tuple[Nyc311Adapter, httpx.AsyncClient]:
    client = make_client(settings)
    adapter = Nyc311Adapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    for k, v in overrides.items():
        setattr(adapter, k, v)
    return adapter, client


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_311_most_recent(settings: Settings) -> None:
    """The feed queries by recency, not a fixed 24h window (erm2-nwe9 publish lag has been
    observed live at 37.6h+), so the live assertion is against the staleness ceiling instead
    of a strict 24h/25h cutoff -- see NYC_311_STALENESS_CEILING."""
    async with make_client(settings) as client:
        snap = await Nyc311Adapter(client=client, settings=settings).fetch()
    assert snap.feed is FeedName.NYC_311
    assert len(snap.records) > 100
    cutoff = now_utc() - NYC_311_STALENESS_CEILING
    assert snap.records[0].created_at >= cutoff, "newest row must clear the staleness ceiling"
    assert all(r.created_at.tzinfo is not None for r in snap.records)
    assert snap.records == sorted(snap.records, key=lambda r: r.created_at, reverse=True)
    located = [r for r in snap.records if r.lat is not None]
    assert len(located) > 50


# ---------------------------------------------------------------------------
# fixture replay
# ---------------------------------------------------------------------------


async def test_replay_311_fixture(settings: Settings) -> None:
    body = _load_fixture(FIXTURE)
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    async with client, respx.mock(assert_all_called=True) as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json=body))
        snap = await adapter.fetch()
    assert 0 < len(snap.records) <= len(body)
    assert all(isinstance(r, ServiceRequest) for r in snap.records)
    assert all(r.created_at.utcoffset() == timedelta(0) for r in snap.records)
    assert snap.stale_after == snap.fetched_at + adapter.ttl


# ---------------------------------------------------------------------------
# behaviour (synthetic inline bodies)
# ---------------------------------------------------------------------------


def test_floating_timestamp_is_new_york_local_converted_to_utc() -> None:
    # 2026-09-08 is EDT (UTC-4)
    assert parse_floating_timestamp("2026-09-08T09:15:00.000") == datetime(
        2026, 9, 8, 13, 15, tzinfo=UTC
    )
    # 2026-01-15 is EST (UTC-5)
    assert parse_floating_timestamp("2026-01-15T09:15:00.000") == datetime(
        2026, 1, 15, 14, 15, tzinfo=UTC
    )
    assert parse_floating_timestamp(None) is None
    assert parse_floating_timestamp("") is None
    with pytest.raises(ValueError):
        parse_floating_timestamp(12345)


def test_format_floating_timestamp_renders_local_time() -> None:
    assert format_floating_timestamp(datetime(2026, 9, 8, 13, 15, tzinfo=UTC)) == (
        "2026-09-08T09:15:00"
    )


async def test_311_query_has_no_time_window_and_orders_desc(settings: Settings) -> None:
    """Recency-only query: no ``$where`` cutoff, ordered newest first, single page.

    This is the fix for erm2-nwe9's real publish lag (observed 37.6h live on
    2026-09-09): a fixed 24h ``$where`` window returns zero rows whenever lag
    exceeds it, which is a false "feed is down". Ordering by created_date DESC
    with no time filter always returns whatever is actually newest.
    """
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    recent = format_floating_timestamp(now_utc())
    async with client, respx.mock() as mock:
        route = mock.get(url).mock(return_value=httpx.Response(200, json=[_row("1", recent)]))
        snap = await adapter.fetch()
    params = httpx.QueryParams(route.calls.last.request.url.query)
    assert params["$order"].startswith("created_date DESC")
    assert int(params["$limit"]) <= 20000
    assert params["$offset"] == "0"
    assert "$where" not in params
    assert route.call_count == 1
    assert snap.records[0].unique_key == "1"


async def test_311_stale_newest_row_is_loud(settings: Settings) -> None:
    """A newest row past the staleness ceiling means the pipeline is genuinely stalled,
    not routine publish lag -- the feed must fail loud rather than serve it as current."""
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    ancient = format_floating_timestamp(now_utc() - timedelta(days=30))
    async with client, respx.mock() as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json=[_row("1", ancient)]))
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "staleness ceiling" in exc_info.value.message


async def test_311_lagging_but_within_ceiling_still_succeeds(settings: Settings) -> None:
    """Publish lag well past 24h (the old fixed window), but inside the staleness
    ceiling, must still be served -- this is exactly the live bug being fixed."""
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    lagged = format_floating_timestamp(now_utc() - timedelta(hours=37, minutes=36))
    async with client, respx.mock() as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json=[_row("1", lagged)]))
        snap = await adapter.fetch()
    assert snap.records[0].unique_key == "1"


async def test_311_pages_while_page_is_full(settings: Settings) -> None:
    """Production caps Nyc311Adapter at ``max_pages = 1`` (a recency query never needs more
    than one page); this test overrides it to exercise the shared $offset paging mechanics.
    """
    adapter, client = _adapter(settings, page_size=3, max_pages=3)
    url = soda_resource_url(settings, DATASET_311)
    recent = format_floating_timestamp(now_utc())
    pages = {
        0: [_row(f"a{i}", recent) for i in range(3)],
        3: [_row(f"b{i}", recent) for i in range(3)],
        6: [_row("c0", recent)],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(httpx.QueryParams(request.url.query)["$offset"])
        return httpx.Response(200, json=pages[offset])

    async with client, respx.mock() as mock:
        route = mock.get(url).mock(side_effect=respond)
        snap = await adapter.fetch()
    assert route.call_count == 3
    assert [r.unique_key for r in snap.records] == ["a0", "a1", "a2", "b0", "b1", "b2", "c0"]


async def test_311_paging_stops_at_max_pages(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    adapter, client = _adapter(settings, page_size=2, max_pages=2)
    url = soda_resource_url(settings, DATASET_311)

    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(httpx.QueryParams(request.url.query)["$offset"])
        return httpx.Response(
            200, json=[_row(f"k{offset + i}", "2026-09-08T09:15:00.000") for i in range(2)]
        )

    with caplog.at_level(logging.WARNING, logger="nyc_live.feeds.socrata"):
        async with client, respx.mock() as mock:
            route = mock.get(url).mock(side_effect=respond)
            snap = await adapter.fetch()
    assert route.call_count == 2
    assert len(snap.records) == 4
    assert any("stopped paging" in rec.message for rec in caplog.records)


async def test_app_token_header_only_when_configured(settings: Settings) -> None:
    url = soda_resource_url(settings, DATASET_311)
    body = [_row("1", "2026-09-08T09:15:00.000")]

    with_token = settings.model_copy(update={"socrata_app_token": "secret-token"})
    adapter, client = _adapter(with_token)
    async with client, respx.mock() as mock:
        route = mock.get(url).mock(return_value=httpx.Response(200, json=body))
        await adapter.fetch()
    assert route.calls.last.request.headers["X-App-Token"] == "secret-token"

    without = settings.model_copy(update={"socrata_app_token": None})
    adapter, client = _adapter(without)
    async with client, respx.mock() as mock:
        route = mock.get(url).mock(return_value=httpx.Response(200, json=body))
        await adapter.fetch()
    assert "X-App-Token" not in route.calls.last.request.headers
    assert route.calls.last.request.headers["User-Agent"] == settings.user_agent


async def test_311_keeps_unlocated_rows_and_drops_out_of_bbox(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    body = [
        _row("located", "2026-09-08T09:15:00.000"),
        _row("unlocated", "2026-09-08T09:14:00.000", latitude=None, longitude=None),
        _row("blank", "2026-09-08T09:13:00.000", latitude="", longitude=""),
        _row("chicago", "2026-09-08T09:12:00.000", latitude="41.8781", longitude="-87.6298"),
        _row("zero", "2026-09-08T09:11:00.000", latitude="0", longitude="0"),
    ]
    with caplog.at_level(logging.INFO, logger="nyc_live.feeds.socrata"):
        async with client, respx.mock() as mock:
            mock.get(url).mock(return_value=httpx.Response(200, json=body))
            snap = await adapter.fetch()
    keys = [r.unique_key for r in snap.records]
    assert keys == ["located", "unlocated", "blank"]
    unlocated = [r for r in snap.records if r.lat is None]
    assert len(unlocated) == 2
    assert any("dropped 2 rows outside the NYC bbox" in rec.message for rec in caplog.records)


async def test_311_optional_fields_stay_none(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    body = [
        {
            "unique_key": "42",
            "created_date": "2026-09-08T09:15:00.000",
            "agency": "DSNY",
            "complaint_type": "Dirty Condition",
        }
    ]
    async with client, respx.mock() as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json=body))
        snap = await adapter.fetch()
    rec = snap.records[0]
    assert rec.closed_at is None
    assert rec.descriptor is None
    assert rec.status is None
    assert rec.borough is None
    assert rec.lat is None and rec.lon is None


async def test_311_404_is_not_found(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    async with client, respx.mock() as mock:
        mock.get(url).mock(return_value=httpx.Response(404, text="dataset gone"))
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert exc_info.value.kind is ErrorKind.NOT_FOUND
    assert exc_info.value.upstream_status == 404


async def test_311_5xx_retries_then_succeeds(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    async with client, respx.mock() as mock:
        route = mock.get(url).mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(502, text="bad gateway"),
                httpx.Response(200, json=[_row("1", "2026-09-08T09:15:00.000")]),
            ]
        )
        snap = await adapter.fetch()
    assert route.call_count == 3
    assert len(snap.records) == 1


async def test_311_5xx_exhausted_is_upstream_http(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    async with client, respx.mock() as mock:
        route = mock.get(url).mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert route.call_count == settings.http_retries + 1
    assert exc_info.value.kind is ErrorKind.UPSTREAM_HTTP
    assert exc_info.value.upstream_status == 500


async def test_311_empty_result_is_loud(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    async with client, respx.mock() as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json=[]))
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE


async def test_311_non_array_body_is_parse_error(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    url = soda_resource_url(settings, DATASET_311)
    async with client, respx.mock() as mock:
        mock.get(url).mock(return_value=httpx.Response(200, json={"error": True}))
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE
