"""weather.gov adapter: one ``WeatherReport`` per NYC observation station.

No key, but the ``User-Agent`` set by ``nyc_live.http.make_client`` is mandatory.
Flow per default location (Central Park, LaGuardia, JFK):

1. ``GET /points/{lat},{lon}`` -> ``properties.observationStations`` (URL) and
   ``properties.forecast`` (URL). Station ids are looked up, never assumed.
2. ``GET <observationStations>`` -> first feature is the nearest station.
3. ``GET /stations/{id}/observations/latest``, ``GET <forecast>``, and
   ``GET /alerts/active?point={lat},{lon}`` concurrently.

Units are converted to the contract (C, km/h, Pa, m, mm). weather.gov returns
``null`` values often; they stay ``None``, never filled.

Active alerts (``/alerts/active``) are a GeoJSON FeatureCollection; an empty
``features`` array is the normal, common case (no advisory/warning right now)
and produces an empty ``WeatherReport.alerts`` list, not an error. A genuine
HTTP/parse failure of the alerts endpoint fails the whole per-location report
the same way an observation or forecast failure does -- it is fetched inside
the same ``asyncio.gather`` and is not special-cased to swallow errors.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    Snapshot,
    WeatherAlert,
    WeatherAlertSeverity,
    WeatherForecastPeriod,
    WeatherObservation,
    WeatherReport,
    now_utc,
)
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

FEED = FeedName.WEATHER


@dataclass(frozen=True)
class Location:
    name: str
    lat: float
    lon: float


DEFAULT_LOCATIONS: tuple[Location, ...] = (
    Location("Central Park", 40.7789, -73.9692),
    Location("LaGuardia", 40.7769, -73.8740),
    Location("JFK", 40.6413, -73.7781),
)
"""Lookup points, not station ids. The nearest station is whatever /points returns first."""


# ---------------------------------------------------------------------------
# Unit conversion
# ---------------------------------------------------------------------------

_IDENTITY: Callable[[float], float] = lambda x: x  # noqa: E731

_CONVERSIONS: dict[str, dict[str, Callable[[float], float]]] = {
    "temperature_c": {
        "degC": _IDENTITY,
        "degF": lambda f: (f - 32.0) * 5.0 / 9.0,
        "K": lambda k: k - 273.15,
    },
    "speed_kmh": {
        "km_h-1": _IDENTITY,
        "m_s-1": lambda v: v * 3.6,
        "mi_h-1": lambda v: v * 1.609344,
        "kn": lambda v: v * 1.852,
    },
    "pressure_pa": {
        "Pa": _IDENTITY,
        "hPa": lambda v: v * 100.0,
        "kPa": lambda v: v * 1000.0,
        "mbar": lambda v: v * 100.0,
    },
    "length_m": {
        "m": _IDENTITY,
        "km": lambda v: v * 1000.0,
        "mi": lambda v: v * 1609.344,
        "ft": lambda v: v * 0.3048,
    },
    "length_mm": {
        "mm": _IDENTITY,
        "m": lambda v: v * 1000.0,
        "in": lambda v: v * 25.4,
    },
    "percent": {"percent": _IDENTITY},
    "angle_deg": {"degree_(angle)": _IDENTITY, "deg": _IDENTITY},
}


def _strip_unit(unit_code: str) -> str:
    # "wmoUnit:degC" / "unit:degC" / "nwsUnit:s" -> "degC"
    return unit_code.split(":", 1)[1] if ":" in unit_code else unit_code


def convert_quantity(quantity: object, target: str) -> float | None:
    """Convert a weather.gov ``{"unitCode", "value"}`` object into the contract unit.

    ``None`` / missing / null value -> ``None`` (never filled). Unknown unit -> ValueError.
    """
    if quantity is None:
        return None
    if not isinstance(quantity, Mapping):
        raise ValueError(f"expected a quantity object for {target}, got {type(quantity).__name__}")
    value = quantity.get("value")
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"non-numeric value for {target}: {value!r}")
    unit_code = quantity.get("unitCode")
    if not isinstance(unit_code, str):
        raise ValueError(f"missing unitCode for {target}")
    table = _CONVERSIONS[target]
    unit = _strip_unit(unit_code)
    try:
        convert = table[unit]
    except KeyError:
        raise ValueError(f"unknown unit {unit_code!r} for {target}") from None
    return round(convert(float(value)), 3)


def forecast_temperature_c(temperature: object, unit: object) -> float | None:
    """Forecast periods carry a bare number plus ``temperatureUnit`` ("F" or "C")."""
    if temperature is None:
        return None
    if not isinstance(temperature, int | float) or isinstance(temperature, bool):
        raise ValueError(f"non-numeric forecast temperature: {temperature!r}")
    if unit == "F":
        return round((float(temperature) - 32.0) * 5.0 / 9.0, 1)
    if unit == "C":
        return float(temperature)
    raise ValueError(f"unknown forecast temperatureUnit {unit!r}")


def _parse_iso(raw: object, field: str) -> datetime:
    if not isinstance(raw, str):
        raise ValueError(f"{field}: expected ISO timestamp string, got {type(raw).__name__}")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"{field}: timestamp {raw!r} has no offset")
    return parsed


def _props(body: object, what: str) -> dict[str, Any]:
    if not isinstance(body, Mapping) or not isinstance(body.get("properties"), Mapping):
        raise ValueError(f"{what}: body has no `properties` object")
    return dict(body["properties"])


# ---------------------------------------------------------------------------
# Parsers (pure; raise ValueError on shape problems)
# ---------------------------------------------------------------------------


def parse_observation(body: object) -> WeatherObservation:
    p = _props(body, "observation")
    text = p.get("textDescription")
    return WeatherObservation(
        observed_at=_parse_iso(p.get("timestamp"), "observation.timestamp"),
        text=str(text) if text else None,
        temperature_c=convert_quantity(p.get("temperature"), "temperature_c"),
        dewpoint_c=convert_quantity(p.get("dewpoint"), "temperature_c"),
        humidity_pct=convert_quantity(p.get("relativeHumidity"), "percent"),
        wind_speed_kmh=convert_quantity(p.get("windSpeed"), "speed_kmh"),
        wind_gust_kmh=convert_quantity(p.get("windGust"), "speed_kmh"),
        wind_direction_deg=convert_quantity(p.get("windDirection"), "angle_deg"),
        pressure_pa=convert_quantity(p.get("barometricPressure"), "pressure_pa"),
        visibility_m=convert_quantity(p.get("visibility"), "length_m"),
        precip_last_hour_mm=convert_quantity(p.get("precipitationLastHour"), "length_mm"),
    )


def parse_forecast(body: object) -> list[WeatherForecastPeriod]:
    p = _props(body, "forecast")
    periods = p.get("periods")
    if not isinstance(periods, list):
        raise ValueError("forecast: `properties.periods` is not a list")
    out: list[WeatherForecastPeriod] = []
    for raw in periods:
        if not isinstance(raw, Mapping):
            raise ValueError("forecast: period is not an object")
        pop = raw.get("probabilityOfPrecipitation")
        wind = raw.get("windSpeed")
        out.append(
            WeatherForecastPeriod(
                name=str(raw.get("name") or ""),
                start=_parse_iso(raw.get("startTime"), "forecast.startTime"),
                end=_parse_iso(raw.get("endTime"), "forecast.endTime"),
                is_daytime=bool(raw.get("isDaytime")),
                temperature_c=forecast_temperature_c(
                    raw.get("temperature"), raw.get("temperatureUnit")
                ),
                short_forecast=str(raw.get("shortForecast") or ""),
                precip_probability_pct=convert_quantity(pop, "percent") if pop else None,
                wind_speed=str(wind) if wind else None,
            )
        )
    return out


def _alert_severity(raw: object) -> WeatherAlertSeverity:
    """Map NWS's ``severity`` string onto the contract enum.

    NWS's own CAP profile guarantees this field is always one of Extreme /
    Severe / Moderate / Minor / Unknown -- ``WeatherAlertSeverity.UNKNOWN`` is
    one of *their* defined values, not a bucket we invented. So an
    unrecognized string here means the upstream sent something outside its
    own published spec, not that we're missing data. Rather than lose an
    otherwise-valid observation + forecast (and every *other* active alert)
    over one alert's one surprising field, we log loudly and fall back to the
    real "Unknown" member. This is narrower than this file's other parsers:
    ``convert_quantity``/``forecast_temperature_c`` still raise on an unknown
    *unit*, because guessing there would silently fabricate a wrong number --
    there is no such risk in recording "severity unrecognized" honestly.
    """
    try:
        return WeatherAlertSeverity(raw)
    except ValueError:
        log.warning("weather: unrecognized alert severity %r; recording as Unknown", raw)
        return WeatherAlertSeverity.UNKNOWN


def parse_alerts(body: object) -> list[WeatherAlert]:
    if not isinstance(body, Mapping):
        raise ValueError("alerts: body is not an object")
    features = body.get("features")
    if not isinstance(features, list):
        raise ValueError("alerts: `features` is not a list")
    out: list[WeatherAlert] = []
    for raw in features:
        if not isinstance(raw, Mapping):
            raise ValueError("alerts: feature is not an object")
        props = raw.get("properties")
        if not isinstance(props, Mapping):
            raise ValueError("alerts: feature has no `properties` object")
        alert_id = props.get("id")
        if not isinstance(alert_id, str) or not alert_id:
            raise ValueError("alerts: feature has no `id`")
        event = props.get("event")
        if not isinstance(event, str) or not event:
            raise ValueError(f"alerts: feature {alert_id!r} has no `event`")
        headline = props.get("headline")
        urgency = props.get("urgency")
        area_desc = props.get("areaDesc")
        expires = props.get("expires")
        out.append(
            WeatherAlert(
                id=alert_id,
                event=event,
                headline=str(headline) if headline else None,
                severity=_alert_severity(props.get("severity")),
                urgency=str(urgency) if urgency else None,
                area_desc=str(area_desc) if area_desc else None,
                effective=_parse_iso(props.get("effective"), f"alerts[{alert_id}].effective"),
                expires=_parse_iso(expires, f"alerts[{alert_id}].expires") if expires else None,
            )
        )
    return out


@dataclass(frozen=True)
class _Station:
    id: str
    name: str
    lat: float
    lon: float


def parse_first_station(body: object) -> _Station:
    """First feature of an ``observationStations`` collection = nearest station."""
    if not isinstance(body, Mapping):
        raise ValueError("stations: body is not an object")
    features = body.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("stations: `features` is empty")
    first = features[0]
    if not isinstance(first, Mapping):
        raise ValueError("stations: first feature is not an object")
    props = first.get("properties")
    geom = first.get("geometry")
    if not isinstance(props, Mapping) or not isinstance(geom, Mapping):
        raise ValueError("stations: first feature lacks properties/geometry")
    coords = geom.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        raise ValueError("stations: first feature has no [lon, lat] coordinates")
    station_id = props.get("stationIdentifier")
    if not isinstance(station_id, str) or not station_id:
        raise ValueError("stations: first feature has no stationIdentifier")
    return _Station(
        id=station_id,
        name=str(props.get("name") or station_id),
        lat=float(coords[1]),
        lon=float(coords[0]),
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class WeatherAdapter:
    name: FeedName = FEED
    backoff_s: float = 0.5

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self.ttl: timedelta = DEFAULT_TTL[self.name]
        self.locations: tuple[Location, ...] = DEFAULT_LOCATIONS
        self._limiter = RateLimiter(self.ttl)

    def is_configured(self) -> bool:
        return True

    @property
    def base(self) -> str:
        return self._settings.weather_base.rstrip("/")

    @property
    def source_url(self) -> str:
        return f"{self.base}/points/{{lat}},{{lon}}"

    async def fetch(self) -> Snapshot[WeatherReport]:
        await self._limiter.wait(self.name.value)
        try:
            return await self._fetch_once()
        except BaseException:
            # A failed attempt must not hold the cadence floor against the next try:
            # otherwise the second refresh after any failure sleeps the whole 300 s TTL
            # inside the caller's refresh lock. This covers every exit from _fetch_once,
            # including the all-stations-failed path (which raises after three GETs) and
            # cancellation.
            self._limiter.forget(self.name.value)
            raise

    async def _fetch_once(self) -> Snapshot[WeatherReport]:
        started = time.perf_counter()
        fetched_at = now_utc()
        results = await asyncio.gather(
            *(self._report_for(loc) for loc in self.locations), return_exceptions=True
        )
        reports: list[WeatherReport] = []
        errors: list[FeedUnavailable] = []
        seen: set[str] = set()
        for loc, result in zip(self.locations, results, strict=True):
            if isinstance(result, WeatherReport):
                if result.station_id in seen:
                    log.info(
                        "weather: %s resolved to duplicate station %s", loc.name, result.station_id
                    )
                    continue
                seen.add(result.station_id)
                reports.append(result)
            elif isinstance(result, FeedUnavailable):
                log.warning("weather: skipping %s: %s", loc.name, result.message)
                errors.append(result)
            elif isinstance(result, BaseException):
                # gather() only hands back exceptions raised inside _report_for; anything
                # not already wrapped is a programming error, surface it loudly.
                raise result
        if not reports:
            first = errors[0] if errors else None
            raise FeedUnavailable(
                self.name,
                "no weather station could be fetched: "
                + "; ".join(
                    f"{loc.name}: {err.message}"
                    for loc, err in zip(self.locations, errors, strict=False)
                ),
                kind=first.kind if first else ErrorKind.INTERNAL,
                url=first.url if first else self.source_url,
                upstream_status=first.upstream_status if first else None,
                retry_after_s=first.retry_after_s if first else None,
            )
        latest = max(r.observation.observed_at for r in reports)
        return Snapshot[WeatherReport](
            feed=self.name,
            fetched_at=fetched_at,
            stale_after=fetched_at + self.ttl,
            source_url=self.source_url,
            records=reports,
            upstream_generated_at=latest,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    # -- per-location pipeline ------------------------------------------------

    async def _get_json(self, url: str) -> Any:
        resp = await get_with_retry(
            self._client,
            url,
            feed=self.name,
            retries=self._settings.http_retries,
            headers={"Accept": "application/geo+json"},
            backoff_s=self.backoff_s,
        )
        try:
            return resp.json()
        except ValueError as exc:
            raise FeedUnavailable(
                self.name,
                f"non-JSON body from {url}: {exc}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=url,
                upstream_status=resp.status_code,
            ) from exc

    async def _report_for(self, loc: Location) -> WeatherReport:
        points_url = f"{self.base}/points/{loc.lat:.4f},{loc.lon:.4f}"
        try:
            points = _props(await self._get_json(points_url), "points")
            stations_url = points.get("observationStations")
            forecast_url = points.get("forecast")
            if not isinstance(stations_url, str) or not isinstance(forecast_url, str):
                raise ValueError("points: missing observationStations / forecast URLs")
            station = parse_first_station(await self._get_json(stations_url))
            if not in_nyc_bbox(station.lat, station.lon):
                raise ValueError(
                    f"station {station.id} at ({station.lat}, {station.lon}) is outside the NYC bbox"
                )
            obs_url = f"{self.base}/stations/{station.id}/observations/latest"
            alerts_url = f"{self.base}/alerts/active?point={loc.lat:.4f},{loc.lon:.4f}"
            obs_body, forecast_body, alerts_body = await asyncio.gather(
                self._get_json(obs_url), self._get_json(forecast_url), self._get_json(alerts_url)
            )
            return WeatherReport(
                lat=station.lat,
                lon=station.lon,
                station_id=station.id,
                station_name=station.name,
                observation=parse_observation(obs_body),
                forecast=parse_forecast(forecast_body),
                alerts=parse_alerts(alerts_body),
            )
        except FeedUnavailable:
            raise
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            raise FeedUnavailable(
                self.name,
                f"{loc.name}: {exc}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=points_url,
            ) from exc


__all__ = [
    "DEFAULT_LOCATIONS",
    "Location",
    "WeatherAdapter",
    "convert_quantity",
    "forecast_temperature_c",
    "parse_alerts",
    "parse_forecast",
    "parse_observation",
]
