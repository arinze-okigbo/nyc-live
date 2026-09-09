"""MTA Bus Time adapter: SIRI VehicleMonitoring positions, key-gated on ``MTA_BUS_TIME_API_KEY``.

Upstream (key required, free from https://bustime.mta.info/wiki/Developers/Index)
-----------------------------------------------------------------------------------
``GET {settings.mta_bus_time_base}/vehicle-monitoring.json?key=...&version=2`` returns
every active bus system-wide (~1,380 vehicles observed live on 2026-09-08). The base is
read from ``Settings`` every time ``source_url`` is evaluated, exactly like the subway
adapters resolve ``settings.mta_gtfs_base``, so ``NYC_LIVE_MTA_BUS_TIME_BASE`` can
redirect or kill this feed and nothing is bound at import.

Response shape (verified live, not invented):
    Siri.ServiceDelivery.VehicleMonitoringDelivery[].VehicleActivity[] -- each has
    ``RecordedAtTime`` (ISO 8601 with a numeric UTC offset, parses directly with
    ``datetime.fromisoformat`` on 3.12) and ``MonitoredVehicleJourney`` carrying
    ``VehicleRef`` / ``LineRef`` (raw SIRI refs, e.g. ``"MTA NYCT_7516"`` /
    ``"MTA NYCT_B54"`` -- the ``{OperatorRef}_{id}`` prefix is kept verbatim rather than
    stripped, since no contract or consumer here specifies the bare GTFS form),
    ``VehicleLocation.{Latitude,Longitude}``, optional ``Bearing``, and
    ``FramedVehicleJourneyRef.DatedVehicleJourneyRef`` (trip id; absent on some
    vehicles, in which case ``trip_id`` is left ``None``).

An invalid key does not fail the HTTP request (still 200); MTA embeds the failure as
``VehicleMonitoringDelivery[].ErrorCondition`` instead, which is checked explicitly and
raised as ``FeedUnavailable`` rather than silently parsed into zero records.

The key is never logged or put in error messages, and never appears in ``source_url``:
it is sent as a query parameter via ``get_with_retry``'s ``params=``, not embedded in
the URL string that ends up in ``Snapshot.source_url`` / ``FeedUnavailable.url``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    BusVehicle,
    ErrorKind,
    FeedName,
    FeedNotConfigured,
    FeedUnavailable,
    Snapshot,
    now_utc,
)
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import get_with_retry

log = logging.getLogger(__name__)

BUS_TIME_ENV_VAR = "MTA_BUS_TIME_API_KEY"
SIRI_VERSION = "2"
VEHICLE_MONITORING_PATH = "vehicle-monitoring.json"
BUS_TIME_VEHICLE_MONITORING_URL = "https://bustime.mta.info/api/siri/vehicle-monitoring.json"
"""Documented default only, composed from the default of ``Settings.mta_bus_time_base``.
Nothing resolves the endpoint from this constant; ``source_url`` reads settings when read."""


def vehicle_monitoring_url(base: str) -> str:
    """Reportable SIRI VehicleMonitoring URL for ``base``. Never carries the API key.

    The live request also sends ``key=<MTA_BUS_TIME_API_KEY>&version=2`` as httpx
    ``params=`` at request time, kept out of this string so the key never leaks into
    logs, ``Snapshot.source_url``, or ``FeedUnavailable.url``.
    """
    return f"{base.rstrip('/')}/{VEHICLE_MONITORING_PATH}"


def _parse_recorded_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@dataclass
class _ParseStats:
    total: int = 0
    dropped_missing_fields: int = 0
    dropped_out_of_bbox: int = 0


def _map_vehicle(activity: Mapping[str, Any]) -> BusVehicle | None:
    mvj = activity.get("MonitoredVehicleJourney")
    if not isinstance(mvj, Mapping):
        return None
    vehicle_ref = mvj.get("VehicleRef")
    location = mvj.get("VehicleLocation")
    if not vehicle_ref or not isinstance(location, Mapping):
        return None
    lat, lon = location.get("Latitude"), location.get("Longitude")
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None
    framed = mvj.get("FramedVehicleJourneyRef")
    trip_id = framed.get("DatedVehicleJourneyRef") if isinstance(framed, Mapping) else None
    bearing = mvj.get("Bearing")
    route_id = mvj.get("LineRef")
    return BusVehicle(
        lat=float(lat),
        lon=float(lon),
        vehicle_id=str(vehicle_ref),
        route_id=str(route_id) if route_id else None,
        trip_id=str(trip_id) if trip_id else None,
        bearing=float(bearing) if isinstance(bearing, int | float) else None,
        timestamp=_parse_recorded_at(activity.get("RecordedAtTime")),
    )


def _error_description(error: Mapping[str, Any]) -> str:
    description = error.get("Description")
    if description:
        return str(description)
    other = error.get("OtherError")
    if isinstance(other, Mapping) and other.get("ErrorText"):
        return str(other["ErrorText"])
    return "unknown SIRI ErrorCondition"


def parse_vehicle_monitoring(
    body: Any, *, feed: FeedName, url: str, upstream_status: int
) -> tuple[list[BusVehicle], _ParseStats]:
    """Map a VehicleMonitoring JSON body into ``BusVehicle`` records.

    Raises ``FeedUnavailable`` (``UPSTREAM_PARSE``) if the envelope itself doesn't match
    the documented shape, or (``UPSTREAM_HTTP``) if MTA embedded an ``ErrorCondition``
    (e.g. a rejected key) -- that happens on an HTTP 200, so it must be checked
    explicitly rather than relying on the status code.
    """
    try:
        deliveries = body["Siri"]["ServiceDelivery"]["VehicleMonitoringDelivery"]
    except (KeyError, TypeError, IndexError) as exc:
        raise FeedUnavailable(
            feed,
            f"response from {url} is missing Siri.ServiceDelivery.VehicleMonitoringDelivery: {exc}",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
            upstream_status=upstream_status,
        ) from exc
    if not isinstance(deliveries, list):
        raise FeedUnavailable(
            feed,
            f"VehicleMonitoringDelivery in response from {url} is not a list",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
            upstream_status=upstream_status,
        )

    stats = _ParseStats()
    records: list[BusVehicle] = []
    for delivery in deliveries:
        if not isinstance(delivery, Mapping):
            continue
        error = delivery.get("ErrorCondition")
        if error:
            raise FeedUnavailable(
                feed,
                f"upstream rejected the request: {_error_description(error)}",
                kind=ErrorKind.UPSTREAM_HTTP,
                url=url,
                upstream_status=upstream_status,
            )
        for activity in delivery.get("VehicleActivity", []):
            stats.total += 1
            record = _map_vehicle(activity) if isinstance(activity, Mapping) else None
            if record is None:
                stats.dropped_missing_fields += 1
                continue
            if not in_nyc_bbox(record.lat, record.lon):
                stats.dropped_out_of_bbox += 1
                continue
            records.append(record)
    return records, stats


class BusPositionsAdapter:
    name: FeedName = FeedName.MTA_BUS
    ttl: timedelta = DEFAULT_TTL[FeedName.MTA_BUS]

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    @property
    def source_url(self) -> str:
        """Key-free endpoint URL, resolved from settings at read time (safe to log)."""
        return vehicle_monitoring_url(self.settings.mta_bus_time_base)

    def is_configured(self) -> bool:
        return bool((self.settings.mta_bus_time_api_key or "").strip())

    async def fetch(self) -> Snapshot[BusVehicle]:
        if not self.is_configured():
            raise FeedNotConfigured(self.name, BUS_TIME_ENV_VAR)
        key = (self.settings.mta_bus_time_api_key or "").strip()
        url = self.source_url
        started = time.perf_counter()
        resp = await get_with_retry(
            self.client,
            url,
            feed=self.name,
            retries=self.settings.http_retries,
            params={"key": key, "version": SIRI_VERSION},
        )
        try:
            body = resp.json()
        except ValueError as exc:
            raise FeedUnavailable(
                self.name,
                f"response from {url} is not JSON: {exc}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=url,
                upstream_status=resp.status_code,
            ) from exc
        records, stats = parse_vehicle_monitoring(
            body, feed=self.name, url=url, upstream_status=resp.status_code
        )
        dropped = stats.dropped_missing_fields + stats.dropped_out_of_bbox
        if dropped:
            log.warning(
                "mta_bus: dropped %d of %d vehicles (%d missing required fields, "
                "%d outside NYC bbox)",
                dropped,
                stats.total,
                stats.dropped_missing_fields,
                stats.dropped_out_of_bbox,
            )
        if not records:
            raise FeedUnavailable(
                self.name,
                f"{stats.total} vehicle activities decoded but zero mapped to NYC bus "
                "positions; MTA buses are never all offline, treating as an upstream fault",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=url,
                upstream_status=resp.status_code,
            )
        try:
            response_ts = body["Siri"]["ServiceDelivery"].get("ResponseTimestamp")
        except (KeyError, TypeError):
            response_ts = None
        fetched_at = now_utc()
        return Snapshot[BusVehicle](
            feed=self.name,
            fetched_at=fetched_at,
            stale_after=fetched_at + self.ttl,
            source_url=url,
            records=records,
            upstream_generated_at=_parse_recorded_at(response_ts),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
