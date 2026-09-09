"""AirQualityAdapter: live shape test, fixture replay, and offline behaviour tests.

Replay tests load real recorded Open-Meteo responses from tests/fixtures/civic/
and skip (naming the file) if one has not been recorded. Behaviour tests that
need a null pollutant or an out-of-bbox coordinate mutate a *copy* of the real
recorded body in-test and say so at the call site -- no invented file is ever
passed off as a recording.

RECORDING THE FIXTURES
----------------------
These commands belong in tests/fixtures/civic/RECORD.md; they live here this
cycle only because RECORD.md is in another lane's file set. Move them next cycle.
Run from the repo root; `python3` is used instead of `jq` only to pretty-print.

```bash
UA='nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)'
OUT=tests/fixtures/civic
BASE='https://air-quality-api.open-meteo.com/v1/air-quality'
VARS='us_aqi,pm2_5,pm10,ozone,nitrogen_dioxide'

# 1. The five borough anchors the adapter actually requests, in one
#    multi-coordinate call -> air_quality_boroughs.json (kept whole; 5 entries).
curl -sS -A "$UA" -G "$BASE" \
  --data-urlencode 'latitude=40.7829,40.8448,40.6782,40.7282,40.5795' \
  --data-urlencode 'longitude=-73.9654,-73.8648,-73.9442,-73.7949,-74.1502' \
  --data-urlencode "current=$VARS" \
  | python3 -m json.tool > "$OUT/air_quality_boroughs.json"

# 2. Grid collapse: LaGuardia and the Bronx, two points ~8 km apart that
#    Open-Meteo snaps onto the SAME cell -> air_quality_grid_collapse.json.
curl -sS -A "$UA" -G "$BASE" \
  --data-urlencode 'latitude=40.7769,40.8448' \
  --data-urlencode 'longitude=-73.8740,-73.8648' \
  --data-urlencode "current=$VARS" \
  | python3 -m json.tool > "$OUT/air_quality_grid_collapse.json"

# 3. A real 400 body (unknown variable) -> air_quality_error_400.json.
curl -sS -A "$UA" -G "$BASE" \
  --data-urlencode 'latitude=40.7829' --data-urlencode 'longitude=-73.9654' \
  --data-urlencode 'current=us_aqi,not_a_real_variable' \
  | python3 -m json.tool > "$OUT/air_quality_error_400.json"
```
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    AirQualityReading,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    now_utc,
)
from nyc_live.feeds.air import (
    CURRENT_VARIABLES,
    DEFAULT_SAMPLE_POINTS,
    FAILURE_RETRY_FLOOR,
    AirQualityAdapter,
    dedupe_by_grid_point,
    parse_air_quality,
    parse_observed_at,
    parse_reading,
)
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import make_client

FIXTURES = Path(__file__).parent.parent / "fixtures" / "civic"
FIXTURE_BOROUGHS = FIXTURES / "air_quality_boroughs.json"
FIXTURE_GRID_COLLAPSE = FIXTURES / "air_quality_grid_collapse.json"
FIXTURE_ERROR_400 = FIXTURES / "air_quality_error_400.json"

MAX_OBSERVATION_AGE = timedelta(hours=3)
"""Upstream publishes hourly; anything older than this means the feed is stalled."""


def _load(path: Path) -> Any:
    if not path.exists():
        pytest.skip(f"fixture not recorded yet: {path}")
    return json.loads(path.read_text())


def _adapter(settings: Settings) -> tuple[AirQualityAdapter, httpx.AsyncClient]:
    client = make_client(settings)
    adapter = AirQualityAdapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    return adapter, client


def _route(settings: Settings) -> Any:
    return respx.get(settings.air_quality_base)


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------


def test_parse_observed_at_attaches_utc_to_a_naive_gmt_timestamp() -> None:
    # Arrange: exactly what Open-Meteo sends -- naive string, offset alongside.
    raw, offset = "2026-09-09T20:00", 0

    # Act
    observed_at = parse_observed_at(raw, offset, "entry[0]")

    # Assert
    assert observed_at.tzinfo is not None
    assert observed_at.utcoffset() == timedelta(0)
    assert observed_at == datetime(2026, 9, 9, 20, 0, tzinfo=UTC)


def test_parse_observed_at_converts_a_non_utc_offset_rather_than_assuming_local() -> None:
    # A naive 20:00 with a -4 h offset is 00:00 UTC the next day, not 20:00 UTC.
    observed_at = parse_observed_at("2026-09-09T20:00", -14400, "entry[0]")

    assert observed_at == datetime(2026, 9, 10, 0, 0, tzinfo=UTC)


def test_parse_observed_at_accepts_an_already_aware_timestamp() -> None:
    observed_at = parse_observed_at("2026-09-09T16:00-04:00", 0, "entry[0]")

    assert observed_at == datetime(2026, 9, 9, 20, 0, tzinfo=UTC)


def test_parse_observed_at_refuses_to_guess_a_zone_for_a_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="refusing to guess the zone"):
        parse_observed_at("2026-09-09T20:00", None, "entry[0]")


def test_parse_reading_keeps_nulls_as_none() -> None:
    # Mutated copy of the first entry of the real recording: every pollutant nulled.
    entry = copy.deepcopy(_load(FIXTURE_BOROUGHS)[0])
    for field in CURRENT_VARIABLES:
        entry["current"][field] = None

    reading = parse_reading(entry, "entry[0]")

    assert reading.us_aqi is None
    assert reading.pm2_5 is None
    assert reading.pm10 is None
    assert reading.ozone is None
    assert reading.nitrogen_dioxide is None
    assert reading.observed_at.tzinfo is not None  # the timestamp is still real


def test_parse_reading_rejects_a_non_numeric_pollutant() -> None:
    entry = copy.deepcopy(_load(FIXTURE_BOROUGHS)[0])
    entry["current"]["pm2_5"] = "twelve"

    with pytest.raises(ValueError, match="pm2_5"):
        parse_reading(entry, "entry[0]")


def test_parse_air_quality_reads_the_recorded_borough_response() -> None:
    body = _load(FIXTURE_BOROUGHS)

    readings, interval_s = parse_air_quality(body)

    assert len(readings) == len(body)
    assert interval_s == 3600.0  # upstream's own hourly cadence
    for reading, entry in zip(readings, body, strict=True):
        assert reading.lat == entry["latitude"]
        assert reading.lon == entry["longitude"]
        assert reading.us_aqi == entry["current"]["us_aqi"]
        assert reading.pm2_5 == entry["current"]["pm2_5"]


def test_parse_air_quality_accepts_the_single_coordinate_object_form() -> None:
    # A one-coordinate request answers with a bare object, not an array.
    body = _load(FIXTURE_BOROUGHS)[0]

    readings, _ = parse_air_quality(body)

    assert len(readings) == 1


def test_parse_air_quality_rejects_an_empty_array() -> None:
    with pytest.raises(ValueError, match="empty array"):
        parse_air_quality([])


def test_parse_air_quality_surfaces_an_upstream_error_body() -> None:
    # The real recorded 400 body, in case it ever arrives with a 200.
    with pytest.raises(ValueError, match="upstream reported an error"):
        parse_air_quality(_load(FIXTURE_ERROR_400))


# ---------------------------------------------------------------------------
# Grid snapping
# ---------------------------------------------------------------------------


def test_recorded_response_proves_coordinates_are_snapped_not_echoed() -> None:
    readings, _ = parse_air_quality(_load(FIXTURE_BOROUGHS))
    requested = {(p.lat, p.lon) for p in DEFAULT_SAMPLE_POINTS}

    # Every returned point is a grid cell, never the coordinate that was asked for.
    assert requested.isdisjoint({(r.lat, r.lon) for r in readings})
    manhattan = readings[0]
    assert manhattan.lat == pytest.approx(40.8, abs=1e-4)
    assert manhattan.lon == pytest.approx(-74.0, abs=1e-4)


def test_dedupe_collapses_two_requests_that_snapped_onto_one_cell() -> None:
    # LaGuardia and the Bronx, ~8 km apart, both answered from (40.800003, -73.9).
    body = _load(FIXTURE_GRID_COLLAPSE)
    readings, _ = parse_air_quality(body)
    assert len(readings) == 2
    assert (readings[0].lat, readings[0].lon) == (readings[1].lat, readings[1].lon)

    kept = dedupe_by_grid_point(readings)

    assert len(kept) == 1
    assert kept[0] == readings[0]


def test_dedupe_keeps_distinct_cells_even_when_their_values_agree() -> None:
    readings, _ = parse_air_quality(_load(FIXTURE_BOROUGHS))

    kept = dedupe_by_grid_point(readings)

    # The five borough anchors resolve to five distinct cells, and several of them
    # carried identical numbers in the recorded hour. Identical values are not a
    # duplicate sample; collapsing them would erase the spatial layer.
    assert len(kept) == len(readings)
    assert len({(r.lat, r.lon) for r in kept}) == len(kept)
    assert len({r.us_aqi for r in kept}) < len(kept)


def test_default_sample_points_are_one_per_borough() -> None:
    assert len(DEFAULT_SAMPLE_POINTS) == 5
    assert all(in_nyc_bbox(p.lat, p.lon) for p in DEFAULT_SAMPLE_POINTS)


# ---------------------------------------------------------------------------
# Adapter: replay
# ---------------------------------------------------------------------------


@respx.mock
async def test_replay_borough_response_returns_one_reading_per_grid_cell(
    settings: Settings,
) -> None:
    body = _load(FIXTURE_BOROUGHS)
    _route(settings).mock(return_value=httpx.Response(200, json=body))
    adapter, client = _adapter(settings)

    async with client:
        snapshot = await adapter.fetch()

    assert snapshot.feed is FeedName.AIR_QUALITY
    assert len(snapshot.records) == len(body)
    assert all(isinstance(r, AirQualityReading) for r in snapshot.records)
    assert snapshot.upstream_generated_at == datetime(2026, 9, 9, 20, 0, tzinfo=UTC)
    assert snapshot.stale_after == snapshot.fetched_at + DEFAULT_TTL[FeedName.AIR_QUALITY]
    assert snapshot.source_url.startswith(settings.air_quality_base)
    assert snapshot.latency_ms is not None


@respx.mock
async def test_replay_sends_one_request_for_all_five_points(settings: Settings) -> None:
    route = _route(settings).mock(return_value=httpx.Response(200, json=_load(FIXTURE_BOROUGHS)))
    adapter, client = _adapter(settings)

    async with client:
        await adapter.fetch()

    assert route.call_count == 1
    request_params = route.calls[0].request.url.params
    assert request_params["latitude"].split(",") == [f"{p.lat:.4f}" for p in DEFAULT_SAMPLE_POINTS]
    assert request_params["longitude"].split(",") == [f"{p.lon:.4f}" for p in DEFAULT_SAMPLE_POINTS]
    assert request_params["current"] == ",".join(CURRENT_VARIABLES)


@respx.mock
async def test_replay_grid_collapse_yields_a_single_record(settings: Settings) -> None:
    _route(settings).mock(return_value=httpx.Response(200, json=_load(FIXTURE_GRID_COLLAPSE)))
    adapter, client = _adapter(settings)

    async with client:
        snapshot = await adapter.fetch()

    assert len(snapshot.records) == 1


@respx.mock
async def test_out_of_bbox_grid_points_are_dropped(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # Mutated copy of the real recording: one entry moved to Albany.
    body = copy.deepcopy(_load(FIXTURE_BOROUGHS))
    body[0]["latitude"] = 42.6526
    body[0]["longitude"] = -73.7562
    _route(settings).mock(return_value=httpx.Response(200, json=body))
    adapter, client = _adapter(settings)

    with caplog.at_level("WARNING", logger="nyc_live.feeds.air"):
        async with client:
            snapshot = await adapter.fetch()

    assert len(snapshot.records) == len(body) - 1
    assert all(in_nyc_bbox(r.lat, r.lon) for r in snapshot.records)
    assert "outside the NYC bbox" in caplog.text


@respx.mock
async def test_every_point_out_of_bbox_raises_instead_of_returning_empty(
    settings: Settings,
) -> None:
    # Mutated copy of the real recording: all five entries moved to Albany.
    body = copy.deepcopy(_load(FIXTURE_BOROUGHS))
    for entry in body:
        entry["latitude"] = 42.6526
        entry["longitude"] = -73.7562
    _route(settings).mock(return_value=httpx.Response(200, json=body))
    adapter, client = _adapter(settings)

    async with client:
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()

    assert excinfo.value.kind is ErrorKind.UPSTREAM_PARSE


# ---------------------------------------------------------------------------
# Adapter: failure modes
# ---------------------------------------------------------------------------


@respx.mock
async def test_http_404_is_fatal_and_loud(settings: Settings) -> None:
    _route(settings).mock(return_value=httpx.Response(404, text="not found"))
    adapter, client = _adapter(settings)

    async with client:
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()

    assert excinfo.value.kind is ErrorKind.NOT_FOUND
    assert excinfo.value.upstream_status == 404


@respx.mock
async def test_http_400_with_the_real_error_body_is_fatal(settings: Settings) -> None:
    _route(settings).mock(return_value=httpx.Response(400, json=_load(FIXTURE_ERROR_400)))
    adapter, client = _adapter(settings)

    async with client:
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()

    assert excinfo.value.upstream_status == 400
    assert excinfo.value.kind is ErrorKind.UPSTREAM_HTTP


@respx.mock
async def test_repeated_5xx_raises_upstream_http(settings: Settings) -> None:
    route = _route(settings).mock(return_value=httpx.Response(503, text="unavailable"))
    adapter, client = _adapter(settings)

    async with client:
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()

    assert excinfo.value.kind is ErrorKind.UPSTREAM_HTTP
    assert route.call_count == settings.http_retries + 1


@respx.mock
async def test_429_reports_rate_limited(settings: Settings) -> None:
    _route(settings).mock(return_value=httpx.Response(429, headers={"Retry-After": "120"}))
    adapter, client = _adapter(settings)

    async with client:
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()

    assert excinfo.value.kind is ErrorKind.RATE_LIMITED
    assert excinfo.value.retry_after_s == 120.0


@respx.mock
async def test_non_json_body_reports_a_parse_error(settings: Settings) -> None:
    _route(settings).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    adapter, client = _adapter(settings)

    async with client:
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()

    assert excinfo.value.kind is ErrorKind.UPSTREAM_PARSE


# ---------------------------------------------------------------------------
# Adapter: rate limiting
# ---------------------------------------------------------------------------


@respx.mock
async def test_failure_releases_the_hour_floor_but_keeps_a_short_retry_floor(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A down upstream must neither block the next refresh for an hour nor be hammered."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    _route(settings).mock(return_value=httpx.Response(503, text="unavailable"))
    adapter, client = _adapter(settings)

    async with client:
        for _ in range(2):
            with pytest.raises(FeedUnavailable):
                await adapter.fetch()

    ttl_s = DEFAULT_TTL[FeedName.AIR_QUALITY].total_seconds()
    # The 1 h cadence floor was released after the first failure...
    assert not any(s > FAILURE_RETRY_FLOOR.total_seconds() for s in slept), slept
    assert not any(s >= ttl_s for s in slept), slept
    # ...but the 60 s failure floor was applied before the second attempt.
    assert any(s == pytest.approx(FAILURE_RETRY_FLOOR.total_seconds(), abs=1.0) for s in slept)


@respx.mock
async def test_success_holds_the_full_ttl_cadence(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    _route(settings).mock(return_value=httpx.Response(200, json=_load(FIXTURE_BOROUGHS)))
    adapter, client = _adapter(settings)

    async with client:
        await adapter.fetch()
        await adapter.fetch()

    ttl_s = DEFAULT_TTL[FeedName.AIR_QUALITY].total_seconds()
    assert any(s == pytest.approx(ttl_s, abs=1.0) for s in slept), slept


def test_adapter_is_always_configured(settings: Settings) -> None:
    adapter = AirQualityAdapter(client=make_client(settings), settings=settings)

    assert adapter.is_configured() is True
    assert adapter.name is FeedName.AIR_QUALITY
    assert adapter.ttl == DEFAULT_TTL[FeedName.AIR_QUALITY]
    assert adapter.base == settings.air_quality_base


@respx.mock
async def test_base_url_is_redirectable_by_env(tmp_path: Path) -> None:
    """NYC_LIVE_AIR_QUALITY_BASE must actually move the upstream, not be baked in."""
    redirected = Settings(
        _env_file=None,  # type: ignore[call-arg]
        NYC_LIVE_DATA_DIR=tmp_path / "data",
        NYC_LIVE_AIR_QUALITY_BASE="https://air.example.test/v1/air-quality",
    )
    route = respx.get("https://air.example.test/v1/air-quality").mock(
        return_value=httpx.Response(200, json=_load(FIXTURE_BOROUGHS))
    )
    adapter, client = _adapter(redirected)

    async with client:
        snapshot = await adapter.fetch()

    assert route.call_count == 1
    assert snapshot.source_url.startswith("https://air.example.test/v1/air-quality")


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_air_quality_returns_real_readings(settings: Settings) -> None:
    adapter, client = _adapter(settings)

    async with client:
        snapshot = await adapter.fetch()

    assert snapshot.feed is FeedName.AIR_QUALITY
    assert snapshot.records, "Open-Meteo returned no usable NYC grid point"
    assert len(snapshot.records) == len({(r.lat, r.lon) for r in snapshot.records})

    requested = {(p.lat, p.lon) for p in DEFAULT_SAMPLE_POINTS}
    graded = [r for r in snapshot.records if r.us_aqi is not None]
    assert graded, "no reading carried a us_aqi"

    for reading in snapshot.records:
        assert in_nyc_bbox(reading.lat, reading.lon)
        # The contract says lat/lon are the sampled grid point, not the request.
        assert (reading.lat, reading.lon) not in requested
        assert reading.observed_at.tzinfo is not None
        assert reading.observed_at.utcoffset() == timedelta(0)
        age = now_utc() - reading.observed_at
        assert -timedelta(minutes=5) <= age <= MAX_OBSERVATION_AGE, (
            f"observation at {reading.observed_at} is {age} old"
        )

    for reading in graded:
        assert isinstance(reading.us_aqi, int)
        assert 0 <= reading.us_aqi <= 500

    for reading in snapshot.records:
        for value in (reading.pm2_5, reading.pm10, reading.ozone, reading.nitrogen_dioxide):
            assert value is None or value >= 0

    assert snapshot.stale_after > snapshot.fetched_at
    assert snapshot.upstream_generated_at is not None
