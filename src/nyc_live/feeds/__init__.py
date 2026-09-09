"""Feed adapter registry.

Adapters are discovered from ADAPTER_SPECS by module path + class name so that
the four feed-* agents never edit a shared registry file. Each agent creates
exactly the module(s) listed for it below. Missing modules are skipped with a
warning so the tree stays runnable mid-Phase-1; a module that exists but fails
to import is a hard error.

Constructor convention (not a Protocol, but every adapter must follow it):

    Adapter(client: httpx.AsyncClient, settings: Settings)
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from nyc_live.config import Settings
from nyc_live.contracts import FeedAdapter, FeedName

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdapterSpec:
    module: str
    cls: str
    feed: FeedName
    owner: str  # agent that owns the module


ADAPTER_SPECS: tuple[AdapterSpec, ...] = (
    AdapterSpec(
        "nyc_live.feeds.cameras", "CameraListAdapter", FeedName.DOT_CAMERAS, "feed-cameras"
    ),
    AdapterSpec(
        "nyc_live.feeds.ny511", "Ny511CamerasAdapter", FeedName.NY511_CAMERAS, "feed-cameras"
    ),
    AdapterSpec(
        "nyc_live.feeds.transit", "SubwayTripsAdapter", FeedName.MTA_SUBWAY, "feed-transit"
    ),
    AdapterSpec(
        "nyc_live.feeds.transit", "SubwayAlertsAdapter", FeedName.MTA_SUBWAY_ALERTS, "feed-transit"
    ),
    AdapterSpec(
        "nyc_live.feeds.transit", "SubwayStopsAdapter", FeedName.MTA_SUBWAY_STOPS, "feed-transit"
    ),
    AdapterSpec(
        "nyc_live.feeds.transit",
        "SubwayShapesAdapter",
        FeedName.MTA_SUBWAY_SHAPES,
        "feed-transit",
    ),
    AdapterSpec("nyc_live.feeds.bus", "BusPositionsAdapter", FeedName.MTA_BUS, "feed-transit"),
    AdapterSpec(
        "nyc_live.feeds.micromobility", "CitiBikeAdapter", FeedName.CITIBIKE, "feed-micromobility"
    ),
    AdapterSpec("nyc_live.feeds.civic", "Nyc311Adapter", FeedName.NYC_311, "feed-civic"),
    AdapterSpec(
        "nyc_live.feeds.civic", "InspectionsAdapter", FeedName.DOHMH_INSPECTIONS, "feed-civic"
    ),
    AdapterSpec("nyc_live.feeds.civic", "WeatherAdapter", FeedName.WEATHER, "feed-civic"),
)

FRAME_SOURCE_SPEC = ("nyc_live.feeds.cameras", "CameraFrameSource")
"""Owned by feed-cameras. Implements contracts.FrameSource."""


def load_adapters(
    client: httpx.AsyncClient, settings: Settings, *, strict: bool = False
) -> list[FeedAdapter[Any]]:
    """Instantiate every adapter whose module exists. `strict=True` fails on any missing module."""
    adapters: list[FeedAdapter[Any]] = []
    for spec in ADAPTER_SPECS:
        try:
            module = importlib.import_module(spec.module)
        except ModuleNotFoundError as exc:
            if exc.name == spec.module:
                msg = f"{spec.module} not present yet (owner: {spec.owner})"
                if strict:
                    raise RuntimeError(msg) from exc
                log.warning(msg)
                continue
            raise
        cls = getattr(module, spec.cls)
        adapter = cls(client=client, settings=settings)
        if adapter.name != spec.feed:
            raise RuntimeError(
                f"{spec.module}.{spec.cls}.name is {adapter.name!r}, expected {spec.feed!r}"
            )
        adapters.append(adapter)
    return adapters


def load_frame_source(client: httpx.AsyncClient, settings: Settings) -> Any:
    module_name, cls_name = FRAME_SOURCE_SPEC
    module = importlib.import_module(module_name)
    return getattr(module, cls_name)(client=client, settings=settings)
