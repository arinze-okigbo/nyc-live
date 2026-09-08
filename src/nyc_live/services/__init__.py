"""Service layer: pure functions over Envelopes + the registry builder.

Consumed by nyc-mcp (tools) and nyc-dash (API). Nothing here fetches an upstream
directly; feeds are read through `FeedRegistry` / `CachedFeed` only.
"""

from nyc_live.services.cameras import camera_frame, nearest_camera, persist_frame_telemetry
from nyc_live.services.density import density_history, density_now
from nyc_live.services.nearby import geo_query, nearby
from nyc_live.services.registry import (
    Services,
    build_registry,
    build_services,
    open_services,
    open_store_for_reader,
)
from nyc_live.services.subway import alerts_near, subway_arrivals
from nyc_live.services.warehouse import warehouse

__all__ = [
    "Services",
    "alerts_near",
    "build_registry",
    "build_services",
    "camera_frame",
    "density_history",
    "density_now",
    "geo_query",
    "nearby",
    "nearest_camera",
    "open_services",
    "open_store_for_reader",
    "persist_frame_telemetry",
    "subway_arrivals",
    "warehouse",
]
