"""WeatherAdapter: live shape test, fixture replay, and offline behaviour tests.

Behaviour tests use small inline weather.gov-shaped bodies (synthetic logic
inputs, not fixtures). The replay test loads the recorded Central Park chain
from tests/fixtures/civic/ and skips naming any missing file.
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
from nyc_live.contracts import ErrorKind, FeedName, FeedUnavailable, WeatherReport, now_utc
from nyc_live.feeds.civic import WeatherAdapter
from nyc_live.feeds.weather import (
    DEFAULT_LOCATIONS,
    Location,
    convert_quantity,
    forecast_temperature_c,
)
from nyc_live.http import make_client

FIXTURES = Path(__file__).parent.parent / "fixtures" / "civic"
FIXTURE_POINTS = FIXTURES / "weather_points_central_park.json"
FIXTURE_STATIONS = FIXTURES / "weather_stations_central_park.json"
FIXTURE_OBSERVATION = FIXTURES / "weather_observation_latest.json"
FIXTURE_FORECAST = FIXTURES / "weather_forecast.json"

CENTRAL_PARK = DEFAULT_LOCATIONS[0]


def _load_fixtures(*paths: Path) -> list[Any]:
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        pytest.skip("fixture not recorded yet: " + ", ".join(missing))
    return [json.loads(p.read_text()) for p in paths]


def _adapter(
    settings: Settings, locations: tuple[Location, ...] | None = None
) -> tuple[WeatherAdapter, httpx.AsyncClient]:
    client = make_client(settings)
    adapter = WeatherAdapter(client=client, settings=settings)
    adapter.backoff_s = 0.0
    if locations is not None:
        adapter.locations = locations
    return adapter, client


def _points_url(settings: Settings, loc: Location) -> str:
    return f"{settings.weather_base}/points/{loc.lat:.4f},{loc.lon:.4f}"


# -- synthetic bodies --------------------------------------------------------


def _points_body(base: str, station_tag: str) -> dict[str, Any]:
    return {
        "properties": {
            "observationStations": f"{base}/gridpoints/OKX/33,37/stations?tag={station_tag}",
            "forecast": f"{base}/gridpoints/OKX/33,37/forecast?tag={station_tag}",
        }
    }


def _stations_body(station_id: str, name: str, lat: float, lon: float) -> dict[str, Any]:
    return {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"stationIdentifier": station_id, "name": name},
            }
        ]
    }


def _observation_body(**overrides: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "timestamp": "2026-09-08T12:51:00+00:00",
        "textDescription": "Mostly Cloudy",
        "temperature": {"unitCode": "wmoUnit:degC", "value": 21.7},
        "dewpoint": {"unitCode": "wmoUnit:degC", "value": 15.0},
        "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 65.4},
        "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 14.76},
        "windGust": {"unitCode": "wmoUnit:km_h-1", "value": None},
        "windDirection": {"unitCode": "wmoUnit:degree_(angle)", "value": 180},
        "barometricPressure": {"unitCode": "wmoUnit:Pa", "value": 101690},
        "visibility": {"unitCode": "wmoUnit:m", "value": 16090},
        "precipitationLastHour": {"unitCode": "wmoUnit:mm", "value": None},
    }
    props.update(overrides)
    return {"properties": props}


def _forecast_body() -> dict[str, Any]:
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
                    "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 20},
                    "windSpeed": "5 to 10 mph",
                    "shortForecast": "Partly Sunny",
                },
                {
                    "name": "Tonight",
                    "startTime": "2026-09-08T18:00:00-04:00",
                    "endTime": "2026-09-09T06:00:00-04:00",
                    "isDaytime": False,
                    "temperature": 15,
                    "temperatureUnit": "C",
                    "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": None},
                    "windSpeed": "5 mph",
                    "shortForecast": "Mostly Clear",
                },
            ]
        }
    }


def _mock_station_chain(
    mock: respx.MockRouter,
    settings: Settings,
    loc: Location,
    station_id: str,
    *,
    lat: float,
    lon: float,
    observation: dict[str, Any] | None = None,
) -> None:
    base = settings.weather_base
    mock.get(_points_url(settings, loc)).mock(
        return_value=httpx.Response(200, json=_points_body(base, station_id))
    )
    mock.get(f"{base}/gridpoints/OKX/33,37/stations", params={"tag": station_id}).mock(
        return_value=httpx.Response(
            200, json=_stations_body(station_id, f"{loc.name} station", lat, lon)
        )
    )
    mock.get(f"{base}/stations/{station_id}/observations/latest").mock(
        return_value=httpx.Response(200, json=observation or _observation_body())
    )
    mock.get(f"{base}/gridpoints/OKX/33,37/forecast", params={"tag": station_id}).mock(
        return_value=httpx.Response(200, json=_forecast_body())
    )


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_weather_one_report_per_station(settings: Settings) -> None:
    async with make_client(settings) as client:
        snap = await WeatherAdapter(client=client, settings=settings).fetch()
    assert snap.feed is FeedName.WEATHER
    assert 1 <= len(snap.records) <= len(DEFAULT_LOCATIONS)
    assert len({r.station_id for r in snap.records}) == len(snap.records)
    cutoff = now_utc() - timedelta(hours=3)
    assert any(r.observation.observed_at >= cutoff for r in snap.records)
    assert all(r.forecast for r in snap.records)
    for r in snap.records:
        if r.observation.temperature_c is not None:
            assert -40 < r.observation.temperature_c < 50


# ---------------------------------------------------------------------------
# fixture replay (Central Park chain)
# ---------------------------------------------------------------------------


async def test_replay_weather_fixture(settings: Settings) -> None:
    points, stations, observation, forecast = _load_fixtures(
        FIXTURE_POINTS, FIXTURE_STATIONS, FIXTURE_OBSERVATION, FIXTURE_FORECAST
    )
    stations_url = points["properties"]["observationStations"]
    forecast_url = points["properties"]["forecast"]
    station_id = stations["features"][0]["properties"]["stationIdentifier"]
    adapter, client = _adapter(settings, locations=(CENTRAL_PARK,))
    base = settings.weather_base
    async with client, respx.mock(assert_all_called=True) as mock:
        mock.get(_points_url(settings, CENTRAL_PARK)).mock(
            return_value=httpx.Response(200, json=points)
        )
        mock.get(stations_url).mock(return_value=httpx.Response(200, json=stations))
        mock.get(f"{base}/stations/{station_id}/observations/latest").mock(
            return_value=httpx.Response(200, json=observation)
        )
        mock.get(forecast_url).mock(return_value=httpx.Response(200, json=forecast))
        snap = await adapter.fetch()
    assert len(snap.records) == 1
    report = snap.records[0]
    assert isinstance(report, WeatherReport)
    assert report.station_id == station_id
    assert report.observation.observed_at.tzinfo is not None
    assert len(report.forecast) == len(forecast["properties"]["periods"])
    assert snap.upstream_generated_at == report.observation.observed_at


# ---------------------------------------------------------------------------
# behaviour (synthetic inline bodies)
# ---------------------------------------------------------------------------


def test_convert_quantity_contract_units_and_nulls() -> None:
    assert convert_quantity({"unitCode": "wmoUnit:degC", "value": 21.7}, "temperature_c") == 21.7
    assert convert_quantity({"unitCode": "wmoUnit:degF", "value": 68}, "temperature_c") == 20.0
    assert convert_quantity({"unitCode": "wmoUnit:km_h-1", "value": 14.76}, "speed_kmh") == 14.76
    assert convert_quantity({"unitCode": "wmoUnit:m_s-1", "value": 10}, "speed_kmh") == 36.0
    assert convert_quantity({"unitCode": "wmoUnit:Pa", "value": 101690}, "pressure_pa") == 101690
    assert convert_quantity({"unitCode": "wmoUnit:hPa", "value": 1016.9}, "pressure_pa") == 101690
    assert convert_quantity({"unitCode": "wmoUnit:m", "value": 16090}, "length_m") == 16090
    assert convert_quantity({"unitCode": "wmoUnit:mm", "value": 0.5}, "length_mm") == 0.5
    assert convert_quantity({"unitCode": "unit:degC", "value": 1}, "temperature_c") == 1.0
    # nulls stay None, never filled
    assert convert_quantity({"unitCode": "wmoUnit:degC", "value": None}, "temperature_c") is None
    assert convert_quantity(None, "temperature_c") is None
    with pytest.raises(ValueError):
        convert_quantity({"unitCode": "wmoUnit:furlong", "value": 1}, "length_m")
    with pytest.raises(ValueError):
        convert_quantity({"unitCode": "wmoUnit:degC", "value": "warm"}, "temperature_c")


def test_forecast_temperature_units() -> None:
    assert forecast_temperature_c(68, "F") == 20.0
    assert forecast_temperature_c(32, "F") == 0.0
    assert forecast_temperature_c(15, "C") == 15.0
    assert forecast_temperature_c(None, "F") is None
    with pytest.raises(ValueError):
        forecast_temperature_c(10, "K")


async def test_weather_builds_one_report_per_station_with_converted_units(
    settings: Settings,
) -> None:
    adapter, client = _adapter(settings)
    async with client, respx.mock(assert_all_called=True) as mock:
        _mock_station_chain(mock, settings, DEFAULT_LOCATIONS[0], "KNYC", lat=40.78, lon=-73.97)
        _mock_station_chain(mock, settings, DEFAULT_LOCATIONS[1], "KLGA", lat=40.78, lon=-73.88)
        _mock_station_chain(mock, settings, DEFAULT_LOCATIONS[2], "KJFK", lat=40.64, lon=-73.76)
        snap = await adapter.fetch()
    assert [r.station_id for r in snap.records] == ["KNYC", "KLGA", "KJFK"]
    report = snap.records[0]
    assert report.station_name == "Central Park station"
    assert (report.lat, report.lon) == (40.78, -73.97)
    obs = report.observation
    assert obs.observed_at == datetime(2026, 9, 8, 12, 51, tzinfo=UTC)
    assert obs.text == "Mostly Cloudy"
    assert obs.temperature_c == 21.7
    assert obs.dewpoint_c == 15.0
    assert obs.humidity_pct == 65.4
    assert obs.wind_speed_kmh == 14.76
    assert obs.wind_gust_kmh is None
    assert obs.wind_direction_deg == 180
    assert obs.pressure_pa == 101690
    assert obs.visibility_m == 16090
    assert obs.precip_last_hour_mm is None
    assert len(report.forecast) == 2
    afternoon, tonight = report.forecast
    assert afternoon.temperature_c == 20.0  # 68 F
    assert afternoon.is_daytime is True
    assert afternoon.precip_probability_pct == 20
    assert afternoon.wind_speed == "5 to 10 mph"
    assert afternoon.start == datetime(2026, 9, 8, 17, 0, tzinfo=UTC)
    assert tonight.temperature_c == 15.0
    assert tonight.precip_probability_pct is None
    assert snap.upstream_generated_at == obs.observed_at
    assert snap.stale_after == snap.fetched_at + adapter.ttl


async def test_weather_sends_user_agent_and_geojson_accept(settings: Settings) -> None:
    adapter, client = _adapter(settings, locations=(CENTRAL_PARK,))
    async with client, respx.mock() as mock:
        _mock_station_chain(mock, settings, CENTRAL_PARK, "KNYC", lat=40.78, lon=-73.97)
        await adapter.fetch()
        req = mock.calls[0].request
    assert req.headers["User-Agent"] == settings.user_agent
    assert req.headers["Accept"] == "application/geo+json"


async def test_weather_all_null_observation_stays_null(settings: Settings) -> None:
    adapter, client = _adapter(settings, locations=(CENTRAL_PARK,))
    nulls = {
        key: {"unitCode": unit, "value": None}
        for key, unit in {
            "temperature": "wmoUnit:degC",
            "dewpoint": "wmoUnit:degC",
            "relativeHumidity": "wmoUnit:percent",
            "windSpeed": "wmoUnit:km_h-1",
            "windGust": "wmoUnit:km_h-1",
            "windDirection": "wmoUnit:degree_(angle)",
            "barometricPressure": "wmoUnit:Pa",
            "visibility": "wmoUnit:m",
            "precipitationLastHour": "wmoUnit:mm",
        }.items()
    }
    async with client, respx.mock() as mock:
        _mock_station_chain(
            mock,
            settings,
            CENTRAL_PARK,
            "KNYC",
            lat=40.78,
            lon=-73.97,
            observation=_observation_body(textDescription=None, **nulls),
        )
        snap = await adapter.fetch()
    obs = snap.records[0].observation
    assert obs.text is None
    assert all(
        getattr(obs, f) is None
        for f in (
            "temperature_c",
            "dewpoint_c",
            "humidity_pct",
            "wind_speed_kmh",
            "wind_gust_kmh",
            "wind_direction_deg",
            "pressure_pa",
            "visibility_m",
            "precip_last_hour_mm",
        )
    )


async def test_weather_one_station_failing_is_skipped_and_logged(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    adapter, client = _adapter(settings)
    with caplog.at_level(logging.WARNING, logger="nyc_live.feeds.weather"):
        async with client, respx.mock() as mock:
            _mock_station_chain(mock, settings, DEFAULT_LOCATIONS[0], "KNYC", lat=40.78, lon=-73.97)
            mock.get(_points_url(settings, DEFAULT_LOCATIONS[1])).mock(
                return_value=httpx.Response(404)
            )
            _mock_station_chain(mock, settings, DEFAULT_LOCATIONS[2], "KJFK", lat=40.64, lon=-73.76)
            snap = await adapter.fetch()
    assert [r.station_id for r in snap.records] == ["KNYC", "KJFK"]
    assert any("skipping LaGuardia" in rec.message for rec in caplog.records)


async def test_weather_points_404_for_every_location_is_not_found(settings: Settings) -> None:
    adapter, client = _adapter(settings)
    async with client, respx.mock() as mock:
        for loc in DEFAULT_LOCATIONS:
            mock.get(_points_url(settings, loc)).mock(return_value=httpx.Response(404))
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert exc_info.value.kind is ErrorKind.NOT_FOUND
    assert exc_info.value.upstream_status == 404
    assert "Central Park" in exc_info.value.message


async def test_weather_5xx_retries_then_succeeds(settings: Settings) -> None:
    adapter, client = _adapter(settings, locations=(CENTRAL_PARK,))
    base = settings.weather_base
    async with client, respx.mock() as mock:
        _mock_station_chain(mock, settings, CENTRAL_PARK, "KNYC", lat=40.78, lon=-73.97)
        route = mock.get(_points_url(settings, CENTRAL_PARK)).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(500),
                httpx.Response(200, json=_points_body(base, "KNYC")),
            ]
        )
        snap = await adapter.fetch()
    assert route.call_count == 3
    assert snap.records[0].station_id == "KNYC"


async def test_weather_5xx_exhausted_is_upstream_http(settings: Settings) -> None:
    adapter, client = _adapter(settings, locations=(CENTRAL_PARK,))
    async with client, respx.mock() as mock:
        route = mock.get(_points_url(settings, CENTRAL_PARK)).mock(return_value=httpx.Response(500))
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert route.call_count == settings.http_retries + 1
    assert exc_info.value.kind is ErrorKind.UPSTREAM_HTTP


async def test_weather_unknown_unit_is_parse_error(settings: Settings) -> None:
    adapter, client = _adapter(settings, locations=(CENTRAL_PARK,))
    bad = _observation_body(temperature={"unitCode": "wmoUnit:furlong", "value": 3})
    async with client, respx.mock() as mock:
        _mock_station_chain(
            mock, settings, CENTRAL_PARK, "KNYC", lat=40.78, lon=-73.97, observation=bad
        )
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE


async def test_weather_station_outside_bbox_is_rejected(settings: Settings) -> None:
    adapter, client = _adapter(settings, locations=(CENTRAL_PARK,))
    # observation/forecast routes are deliberately never reached
    async with client, respx.mock(assert_all_called=False) as mock:
        _mock_station_chain(mock, settings, CENTRAL_PARK, "KBOS", lat=42.36, lon=-71.01)
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "outside the NYC bbox" in exc_info.value.message


async def test_weather_empty_station_list_is_parse_error(settings: Settings) -> None:
    adapter, client = _adapter(settings, locations=(CENTRAL_PARK,))
    base = settings.weather_base
    async with client, respx.mock() as mock:
        mock.get(_points_url(settings, CENTRAL_PARK)).mock(
            return_value=httpx.Response(200, json=_points_body(base, "X"))
        )
        mock.get(f"{base}/gridpoints/OKX/33,37/stations", params={"tag": "X"}).mock(
            return_value=httpx.Response(200, json={"features": []})
        )
        with pytest.raises(FeedUnavailable) as exc_info:
            await adapter.fetch()
    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE
