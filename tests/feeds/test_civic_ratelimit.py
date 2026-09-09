"""Regression tests: a failed fetch must not hold the per-feed cadence floor.

Every civic adapter arms its ``RateLimiter`` before the request. ``wait()``
records the attempt whatever happens next, so before the fix the *second*
refresh after a failure slept a whole TTL (300 s for 311 and weather, 6 h for
inspections) inside the caller's refresh lock and blocked every reader of that
feed. These tests drive each adapter twice against a failing upstream and
assert the second call answers promptly, and separately assert the success-path
throttle still works.

Bodies here are tiny synthetic weather.gov / SoDA shapes (logic inputs, not
recorded fixtures).
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx

from nyc_live.config import Settings
from nyc_live.contracts import FeedUnavailable
from nyc_live.feeds.civic import InspectionsAdapter, Nyc311Adapter, WeatherAdapter
from nyc_live.feeds.socrata import DATASET_311, DATASET_INSPECTIONS, soda_resource_url
from nyc_live.feeds.weather import Location
from nyc_live.http import RateLimiter, make_client

RETRY_BUDGET_S = 2.0
"""How long a caller may wait for the retry after a failed fetch. TTLs are 300 s / 21600 s."""

THROTTLE_S = 0.3
"""Short cadence floor injected to prove the success path is still throttled."""

type _Adapter = Nyc311Adapter | InspectionsAdapter | WeatherAdapter


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _second_fetch_elapsed(adapter: _Adapter, label: str) -> float:
    """Fail once, then time the next attempt. Fails the test instead of hanging a TTL."""
    with pytest.raises(FeedUnavailable):
        await adapter.fetch()
    started = time.perf_counter()
    try:
        async with asyncio.timeout(RETRY_BUDGET_S):
            with pytest.raises(FeedUnavailable):
                await adapter.fetch()
    except TimeoutError:
        pytest.fail(
            f"{label}: the retry after a failed fetch did not answer within {RETRY_BUDGET_S} s; "
            f"the rate limiter is still holding the {adapter.ttl.total_seconds():.0f} s "
            "cadence floor from the failed attempt"
        )
    elapsed = time.perf_counter() - started
    assert elapsed < RETRY_BUDGET_S
    print(f"[cadence] {label}: retry after failure took {elapsed * 1000:.1f} ms")
    return elapsed


def _boom(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _row_311(key: str) -> dict[str, Any]:
    return {
        "unique_key": key,
        "created_date": "2026-09-08T09:15:00.000",
        "agency": "NYPD",
        "complaint_type": "Noise - Residential",
        "status": "Open",
        "latitude": "40.7128",
        "longitude": "-74.0060",
    }


def _row_inspection(camis: str) -> dict[str, Any]:
    return {
        "camis": camis,
        "dba": "TEST DINER",
        "boro": "Manhattan",
        "inspection_date": "2026-08-01T00:00:00.000",
        "action": "Violations were cited in the following area(s).",
        "violation_code": "10F",
        "critical_flag": "Not Critical",
        "score": "12",
        "latitude": "40.7128",
        "longitude": "-74.0060",
    }


def _weather_points(base: str) -> dict[str, Any]:
    return {
        "properties": {
            "observationStations": f"{base}/gridpoints/OKX/33,37/stations",
            "forecast": f"{base}/gridpoints/OKX/33,37/forecast",
        }
    }


def _weather_stations() -> dict[str, Any]:
    return {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [-73.9692, 40.7789]},
                "properties": {"stationIdentifier": "KNYC", "name": "Central Park"},
            }
        ]
    }


def _weather_observation() -> dict[str, Any]:
    return {
        "properties": {
            "timestamp": "2026-09-08T12:51:00+00:00",
            "textDescription": "Mostly Cloudy",
            "temperature": {"unitCode": "wmoUnit:degC", "value": 21.7},
            "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 14.76},
        }
    }


def _weather_forecast() -> dict[str, Any]:
    return {
        "properties": {
            "periods": [
                {
                    "name": "This Afternoon",
                    "startTime": "2026-09-08T13:00:00-04:00",
                    "endTime": "2026-09-08T18:00:00-04:00",
                    "isDaytime": True,
                    "temperature": 68,
                    "temperatureUnit": "F",
                    "shortForecast": "Partly Sunny",
                }
            ]
        }
    }


def _mock_weather_chain(mock: respx.MockRouter, base: str, loc: Location) -> None:
    mock.get(f"{base}/points/{loc.lat:.4f},{loc.lon:.4f}").mock(
        return_value=httpx.Response(200, json=_weather_points(base))
    )
    mock.get(f"{base}/gridpoints/OKX/33,37/stations").mock(
        return_value=httpx.Response(200, json=_weather_stations())
    )
    mock.get(f"{base}/stations/KNYC/observations/latest").mock(
        return_value=httpx.Response(200, json=_weather_observation())
    )
    mock.get(f"{base}/gridpoints/OKX/33,37/forecast").mock(
        return_value=httpx.Response(200, json=_weather_forecast())
    )


# ---------------------------------------------------------------------------
# 311: transport failure, and the parse failures that happen after a good GET
# ---------------------------------------------------------------------------


async def test_311_transport_failure_releases_the_cadence_floor(settings: Settings) -> None:
    client = make_client(settings)
    adapter = Nyc311Adapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    async with client, respx.mock() as mock:
        mock.get(soda_resource_url(settings, DATASET_311)).mock(side_effect=_boom)
        await _second_fetch_elapsed(adapter, "nyc_311 transport error")


async def test_311_empty_window_releases_the_cadence_floor(settings: Settings) -> None:
    """`[]` is a successful GET followed by FeedUnavailable(UPSTREAM_PARSE) -- a naive
    fix around the GET alone would still hold the floor here."""
    client = make_client(settings)
    adapter = Nyc311Adapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    async with client, respx.mock() as mock:
        route = mock.get(soda_resource_url(settings, DATASET_311)).mock(
            return_value=httpx.Response(200, json=[])
        )
        await _second_fetch_elapsed(adapter, "nyc_311 empty window")
    assert route.call_count == 2


async def test_311_non_array_body_releases_the_cadence_floor(settings: Settings) -> None:
    client = make_client(settings)
    adapter = Nyc311Adapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    async with client, respx.mock() as mock:
        mock.get(soda_resource_url(settings, DATASET_311)).mock(
            return_value=httpx.Response(200, json={"error": True, "message": "query timed out"})
        )
        await _second_fetch_elapsed(adapter, "nyc_311 non-array body")


async def test_311_failure_on_a_later_page_releases_the_cadence_floor(settings: Settings) -> None:
    """Socrata pages with `$offset`; the failure lands on page 3 after two good pages.

    Production caps Nyc311Adapter at `max_pages = 1` (a recency query never needs more than
    one page), so this test raises the cap explicitly to exercise mid-paging failure recovery.
    """
    client = make_client(settings)
    adapter = Nyc311Adapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    adapter.page_size = 2
    adapter.max_pages = 3

    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(httpx.QueryParams(request.url.query)["$offset"])
        if offset < 4:
            return httpx.Response(200, json=[_row_311(f"{offset}"), _row_311(f"{offset + 1}")])
        return httpx.Response(503, text="service unavailable")

    async with client, respx.mock() as mock:
        mock.get(soda_resource_url(settings, DATASET_311)).mock(side_effect=respond)
        await _second_fetch_elapsed(adapter, "nyc_311 failure on page 3")


async def test_311_success_path_is_still_throttled(settings: Settings) -> None:
    client = make_client(settings)
    adapter = Nyc311Adapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    adapter._limiter = RateLimiter(timedelta(seconds=THROTTLE_S))
    async with client, respx.mock() as mock:
        mock.get(soda_resource_url(settings, DATASET_311)).mock(
            return_value=httpx.Response(200, json=[_row_311("1")])
        )
        assert (await adapter.fetch()).records
        started = time.perf_counter()
        assert (await adapter.fetch()).records
        elapsed = time.perf_counter() - started
    assert elapsed >= THROTTLE_S * 0.9, "two successes inside the floor must still be throttled"
    print(f"[cadence] nyc_311: second success waited {elapsed * 1000:.1f} ms")


# ---------------------------------------------------------------------------
# inspections (6 h TTL -- the worst case)
# ---------------------------------------------------------------------------


async def test_inspections_5xx_releases_the_cadence_floor(settings: Settings) -> None:
    client = make_client(settings)
    adapter = InspectionsAdapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    async with client, respx.mock() as mock:
        mock.get(soda_resource_url(settings, DATASET_INSPECTIONS)).mock(
            return_value=httpx.Response(502, text="bad gateway")
        )
        await _second_fetch_elapsed(adapter, "dohmh_inspections 502")


async def test_inspections_unmappable_rows_release_the_cadence_floor(settings: Settings) -> None:
    """Rows arrive but none map (no coordinates) -> FeedUnavailable after a good GET."""
    client = make_client(settings)
    adapter = InspectionsAdapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    row = _row_inspection("1") | {"latitude": "", "longitude": ""}
    async with client, respx.mock() as mock:
        mock.get(soda_resource_url(settings, DATASET_INSPECTIONS)).mock(
            return_value=httpx.Response(200, json=[row])
        )
        await _second_fetch_elapsed(adapter, "dohmh_inspections unmappable rows")


async def test_inspections_success_path_is_still_throttled(settings: Settings) -> None:
    client = make_client(settings)
    adapter = InspectionsAdapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    adapter._limiter = RateLimiter(timedelta(seconds=THROTTLE_S))
    async with client, respx.mock() as mock:
        mock.get(soda_resource_url(settings, DATASET_INSPECTIONS)).mock(
            return_value=httpx.Response(200, json=[_row_inspection("1")])
        )
        assert (await adapter.fetch()).records
        started = time.perf_counter()
        assert (await adapter.fetch()).records
        elapsed = time.perf_counter() - started
    assert elapsed >= THROTTLE_S * 0.9, "two successes inside the floor must still be throttled"
    print(f"[cadence] dohmh_inspections: second success waited {elapsed * 1000:.1f} ms")


# ---------------------------------------------------------------------------
# weather (fans out over three locations; only an all-fail raises)
# ---------------------------------------------------------------------------


async def test_weather_all_stations_failing_releases_the_cadence_floor(settings: Settings) -> None:
    """Individual station failures are swallowed; the raise happens after three GETs."""
    client = make_client(settings)
    adapter = WeatherAdapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    base = settings.weather_base.rstrip("/")
    async with client, respx.mock() as mock:
        route = mock.get(url__startswith=f"{base}/points/").mock(side_effect=_boom)
        await _second_fetch_elapsed(adapter, "weather all stations down")
    assert route.call_count > len(adapter.locations)


async def test_weather_parse_failure_releases_the_cadence_floor(settings: Settings) -> None:
    """A 200 with no `properties` for every location: parse failure after good GETs."""
    client = make_client(settings)
    adapter = WeatherAdapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    base = settings.weather_base.rstrip("/")
    async with client, respx.mock() as mock:
        mock.get(url__startswith=f"{base}/points/").mock(
            return_value=httpx.Response(200, json={"nope": 1})
        )
        await _second_fetch_elapsed(adapter, "weather points body unparseable")


async def test_weather_success_path_is_still_throttled(settings: Settings) -> None:
    client = make_client(settings)
    adapter = WeatherAdapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    loc = Location("Central Park", 40.7789, -73.9692)
    adapter.locations = (loc,)
    adapter._limiter = RateLimiter(timedelta(seconds=THROTTLE_S))
    base = settings.weather_base.rstrip("/")
    async with client, respx.mock() as mock:
        _mock_weather_chain(mock, base, loc)
        assert (await adapter.fetch()).records
        started = time.perf_counter()
        assert (await adapter.fetch()).records
        elapsed = time.perf_counter() - started
    assert elapsed >= THROTTLE_S * 0.9, "two successes inside the floor must still be throttled"
    print(f"[cadence] weather: second success waited {elapsed * 1000:.1f} ms")
