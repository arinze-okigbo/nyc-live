"""MTA Bus Time adapter stub (deferred feed, key-gated on ``MTA_BUS_TIME_API_KEY``).

MTA Bus Time exposes SIRI VehicleMonitoring at
``https://bustime.mta.info/api/siri/vehicle-monitoring.json?key=<KEY>&version=2``
(bustime.mta.info, key required, free from https://bustime.mta.info/wiki/Developers/Index).
Phase 1 ships only the registry-visible stub: without the key ``is_configured()`` is False
and ``fetch()`` raises ``FeedNotConfigured`` so the cache and ``nyc-smoke`` skip it cleanly.
With the key present, ``fetch()`` still raises (``ErrorKind.INTERNAL``, "not implemented")
rather than returning an empty or invented snapshot. The key is never logged or put in
error messages.
"""

from __future__ import annotations

from datetime import timedelta

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
)

BUS_TIME_ENV_VAR = "MTA_BUS_TIME_API_KEY"
BUS_TIME_VEHICLE_MONITORING_URL = "https://bustime.mta.info/api/siri/vehicle-monitoring.json"


class BusPositionsAdapter:
    name: FeedName = FeedName.MTA_BUS
    ttl: timedelta = DEFAULT_TTL[FeedName.MTA_BUS]
    source_url = BUS_TIME_VEHICLE_MONITORING_URL

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def is_configured(self) -> bool:
        return bool((self.settings.mta_bus_time_api_key or "").strip())

    async def fetch(self) -> Snapshot[BusVehicle]:
        if not self.is_configured():
            raise FeedNotConfigured(self.name, BUS_TIME_ENV_VAR)
        raise FeedUnavailable(
            self.name,
            f"{BUS_TIME_ENV_VAR} is set but the MTA Bus Time adapter is deferred "
            "(SIRI VehicleMonitoring parsing not implemented yet)",
            kind=ErrorKind.INTERNAL,
            url=self.source_url,
        )
