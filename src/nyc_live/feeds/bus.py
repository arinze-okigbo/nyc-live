"""MTA Bus Time adapter stub (deferred feed, key-gated on ``MTA_BUS_TIME_API_KEY``).

MTA Bus Time exposes SIRI VehicleMonitoring at ``{settings.mta_bus_time_base}/
vehicle-monitoring.json`` (bustime.mta.info, key required, free from
https://bustime.mta.info/wiki/Developers/Index). The base is read from ``Settings``
every time ``source_url`` is evaluated, exactly like the subway adapters resolve
``settings.mta_gtfs_base`` / ``settings.mta_static_gtfs_url``, so
``NYC_LIVE_MTA_BUS_TIME_BASE`` can redirect or kill this feed and nothing is bound at
import. ``BUS_TIME_VEHICLE_MONITORING_URL`` below is only the documented default.

Phase 1 ships only the registry-visible stub: without the key ``is_configured()`` is False
and ``fetch()`` raises ``FeedNotConfigured`` so the cache and ``nyc-smoke`` skip it cleanly.
With the key present, ``fetch()`` still raises (``ErrorKind.INTERNAL``, "not implemented")
rather than returning an empty or invented snapshot.

The key is never logged or put in error messages, and never appears in ``source_url``:
the real SIRI request carries ``key`` and ``version`` as query parameters, which belong in
the httpx ``params=`` argument at request time, not in the reportable URL. See
``vehicle_monitoring_url`` before implementing the real fetch.
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
VEHICLE_MONITORING_PATH = "vehicle-monitoring.json"
BUS_TIME_VEHICLE_MONITORING_URL = "https://bustime.mta.info/api/siri/vehicle-monitoring.json"
"""Documented default only, composed from the default of ``Settings.mta_bus_time_base``.
Nothing resolves the endpoint from this constant; ``source_url`` reads settings when read."""


def vehicle_monitoring_url(base: str) -> str:
    """Reportable SIRI VehicleMonitoring URL for ``base``. Never carries the API key.

    IMPLEMENTORS: the live request also needs ``key=<MTA_BUS_TIME_API_KEY>&version=2``.
    Pass those to httpx as ``params=`` at request time and keep them out of this string.
    The return value is what goes into logs, ``Snapshot.source_url`` and
    ``FeedUnavailable.url``, so putting the key in here would leak it into every one of
    them at once.
    """
    return f"{base.rstrip('/')}/{VEHICLE_MONITORING_PATH}"


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
        raise FeedUnavailable(
            self.name,
            f"{BUS_TIME_ENV_VAR} is set but the MTA Bus Time adapter is deferred "
            "(SIRI VehicleMonitoring parsing not implemented yet)",
            kind=ErrorKind.INTERNAL,
            url=self.source_url,  # key-free by construction; see vehicle_monitoring_url
        )
