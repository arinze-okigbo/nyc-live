"""Feed routing table for the dashboard API.

One `FeedRoute` per dashboard endpoint. Every handler reads through
`nyc_live.services` (the feed registry, the geo helpers, the derived subway and
density views) and returns a `contracts.Envelope` untouched: the HTTP layer only
serialises it with `model_dump(mode="json")`, so `status`, `fetched_at`,
`stale_after`, `error`, `query`, `total_before_filter` and `truncated` reach the
browser exactly as the service layer produced them.

Nothing here fetches an upstream. `CachedFeed.get()` decides whether the TTL has
expired, and a failed refresh becomes `status="stale"` (last good snapshot plus
the error) or `status="error"` (no records at all). The dashboard never
substitutes a placeholder for either.

`density` is served by `nyc_live.services.density_now`, which is the same
function `nyc_vision.service` re-exports; there is one aggregation path so the
MCP tools and this dashboard cannot disagree. It is imported from
`nyc_live.services` because `nyc-vision` is not a declared dependency of
`nyc-dash`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from nyc_live import services as svc_api
from nyc_live.contracts import (
    Camera,
    Envelope,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    GeoQuery,
    now_utc,
)
from nyc_live.services import Services

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Params:
    """Query arguments common to every feed endpoint (and to `/api/stream`)."""

    query: GeoQuery | None = None
    limit: int | None = None
    stop_id: str | None = None
    camera_id: str | None = None
    complaint_type: str | None = None
    horizon_s: int = 1800
    window_s: int = 300


Handler = Callable[[Services, Params], Awaitable[Envelope[Any]]]


@dataclass(frozen=True)
class FeedRoute:
    key: str
    feed: FeedName
    label: str
    handler: Handler
    geo: bool = True
    default_limit: int | None = None
    default_radius_m: float = 1000.0
    aliases: tuple[str, ...] = field(default=())
    description: str = ""


# --------------------------------------------------------------------------- helpers


def _error_envelope(feed: FeedName, message: str, kind: ErrorKind) -> Envelope[Any]:
    return Envelope[BaseModel](
        feed=feed,
        status="error",
        fetched_at=None,
        stale_after=None,
        records=[],
        error=FeedUnavailable(feed, message, kind=kind).to_model(),
    )


def not_registered(feed: FeedName) -> Envelope[Any]:
    """Honest envelope for a feed whose adapter module is absent from this process."""
    return _error_envelope(
        feed,
        f"{feed.value} is not registered in this process; its adapter module was not loaded",
        ErrorKind.INTERNAL,
    )


def crashed(feed: FeedName, exc: BaseException) -> Envelope[Any]:
    """Honest envelope for an unexpected failure inside a handler (never a blank layer)."""
    return _error_envelope(feed, f"{type(exc).__name__}: {exc}", ErrorKind.INTERNAL)


async def cached(svc: Services, feed: FeedName) -> Envelope[Any]:
    if feed not in svc.registry:
        return not_registered(feed)
    return await svc.registry[feed].get()


def _keep(env: Envelope[Any], predicate: Callable[[Any], bool]) -> Envelope[Any]:
    """Attribute filter on a non-error envelope, preserving `total_before_filter`."""
    if env.status == "error":
        return env
    total = env.total_before_filter if env.total_before_filter is not None else len(env.records)
    return env.model_copy(
        update={"records": [r for r in env.records if predicate(r)], "total_before_filter": total}
    )


def health_payload(svc: Services) -> dict[str, Any]:
    """`FeedRegistry.health()` plus the store state, in the same shape as the MCP tool."""
    store = svc.store
    return {
        "checked_at": now_utc().isoformat(),
        "store": {
            "path": str(svc.settings.duckdb_path),
            "open": store is not None,
            "read_only": store.read_only if store is not None else None,
        },
        "feeds": [h.model_dump(mode="json") for h in svc.registry.health()],
    }


def _plain(feed: FeedName) -> Handler:
    async def handler(svc: Services, p: Params) -> Envelope[Any]:
        return svc_api.nearby(await cached(svc, feed), p.query, limit=p.limit)

    return handler


# --------------------------------------------------------------------------- handlers


async def _subway_arrivals(svc: Services, p: Params) -> Envelope[Any]:
    trips, stops = await asyncio.gather(
        cached(svc, FeedName.MTA_SUBWAY), cached(svc, FeedName.MTA_SUBWAY_STOPS)
    )
    return svc_api.subway_arrivals(
        trips,
        stops,
        query=p.query,
        stop_id=p.stop_id,
        horizon=timedelta(seconds=max(p.horizon_s, 0)),
        limit=p.limit,
    )


async def _subway_alerts(svc: Services, p: Params) -> Envelope[Any]:
    env = await cached(svc, FeedName.MTA_SUBWAY_ALERTS)
    if p.query is not None:
        stops = await cached(svc, FeedName.MTA_SUBWAY_STOPS)
        env = svc_api.alerts_near(env, stops, p.query)
    return svc_api.nearby(env, None, limit=p.limit)


async def _nyc_311(svc: Services, p: Params) -> Envelope[Any]:
    env = await cached(svc, FeedName.NYC_311)
    if p.complaint_type:
        needle = p.complaint_type.lower()
        env = _keep(env, lambda r: needle in r.complaint_type.lower())
    return svc_api.nearby(env, p.query, limit=p.limit)


async def _density(svc: Services, p: Params) -> Envelope[Any]:
    cameras: list[Camera] = []
    if FeedName.DOT_CAMERAS in svc.registry:
        snap = svc.registry[FeedName.DOT_CAMERAS].snapshot
        cameras = list(snap.records) if snap is not None else []
    return svc_api.density_now(
        svc.store,
        p.query,
        window=timedelta(seconds=max(p.window_s, 1)),
        camera_id=p.camera_id,
        limit=p.limit,
        cameras=cameras,
    )


async def _camera_density_history(svc: Services, p: Params) -> Envelope[Any]:
    """One camera's density trend, bucketed for a client-side chart. Requires `camera_id`."""
    if not p.camera_id:
        return _error_envelope(
            FeedName.DENSITY,
            "camera_id is required for camera_density_history",
            ErrorKind.INTERNAL,
        )
    cameras: list[Camera] = []
    if FeedName.DOT_CAMERAS in svc.registry:
        snap = svc.registry[FeedName.DOT_CAMERAS].snapshot
        cameras = list(snap.records) if snap is not None else []
    return svc_api.camera_density_history(
        svc.store,
        p.camera_id,
        since_s=max(p.window_s, 1),
        limit=p.limit,
        cameras=cameras,
    )


# --------------------------------------------------------------------------- table

ROUTES: tuple[FeedRoute, ...] = (
    FeedRoute(
        key="dot_cameras",
        feed=FeedName.DOT_CAMERAS,
        label="DOT cameras",
        handler=_plain(FeedName.DOT_CAMERAS),
        aliases=("cameras",),
        description="NYC DOT traffic camera list (Envelope[Camera]); 10 minute TTL.",
    ),
    FeedRoute(
        key="density",
        feed=FeedName.DENSITY,
        label="Camera density",
        handler=_density,
        default_limit=500,
        description=(
            "Pedestrian / vehicle density per camera from nyc-vision detections "
            "(Envelope[CameraDensity]). Until nyc-vision has written density_samples this "
            'is status="error", kind="not_configured"; the heatmap layer is then hidden.'
        ),
    ),
    FeedRoute(
        key="camera_density_history",
        feed=FeedName.DENSITY,
        label="Camera density trend",
        handler=_camera_density_history,
        geo=False,
        default_limit=500,
        description=(
            "Per-bucket person/vehicle density trend for one camera "
            "(Envelope[CameraDensity]), for the map's camera-click trend chart. Requires "
            "camera_id; window_s sets how far back to look (default 300s at this HTTP "
            "layer — pass window_s=3600 for the last hour). Bucket size is chosen "
            'automatically (about 120 points). status="error", kind="not_configured" '
            "until nyc-vision has written density_samples for this camera; "
            'kind="internal" means camera_id was missing.'
        ),
    ),
    FeedRoute(
        key="subway_arrivals",
        feed=FeedName.MTA_SUBWAY,
        label="Subway",
        handler=_subway_arrivals,
        default_limit=1000,
        default_radius_m=500,
        aliases=("subway",),
        description=(
            "Upcoming subway arrivals (Envelope[SubwayArrival]) joined to the static stop "
            "list, so every record carries the stop's coordinates. Accepts stop_id and "
            "horizon_s."
        ),
    ),
    FeedRoute(
        key="mta_subway",
        feed=FeedName.MTA_SUBWAY,
        label="Subway trips (raw)",
        handler=_plain(FeedName.MTA_SUBWAY),
        geo=False,
        default_limit=500,
        aliases=("subway_trips",),
        description=(
            "Raw GTFS-realtime trip updates (Envelope[SubwayTrip]). Trips have no "
            "coordinates, so lat/lon filtering is not supported here; use /api/subway."
        ),
    ),
    FeedRoute(
        key="mta_subway_shapes",
        feed=FeedName.MTA_SUBWAY_SHAPES,
        label="Subway route shapes",
        handler=_plain(FeedName.MTA_SUBWAY_SHAPES),
        geo=False,
        default_limit=None,
        aliases=("subway_shapes",),
        description=(
            "Static GTFS route polylines (Envelope[SubwayRouteShape]); 24 hour TTL. "
            "Each record is one shape_id's ordered (lat, lon) points -- a route has many "
            "shapes (branches, express/local, direction), never one polyline per route. "
            "No top-level lat/lon, so geo filtering is not supported here."
        ),
    ),
    FeedRoute(
        key="mta_subway_stops",
        feed=FeedName.MTA_SUBWAY_STOPS,
        label="Subway stops",
        handler=_plain(FeedName.MTA_SUBWAY_STOPS),
        aliases=("subway_stops", "stops"),
        description="Static GTFS stops (Envelope[SubwayStop]); 24 hour TTL.",
    ),
    FeedRoute(
        key="mta_subway_alerts",
        feed=FeedName.MTA_SUBWAY_ALERTS,
        label="Subway alerts",
        handler=_subway_alerts,
        default_limit=200,
        default_radius_m=800,
        aliases=("subway_alerts", "alerts"),
        description=(
            "Active service alerts (Envelope[SubwayAlert]). With lat/lon the join goes "
            "through the stop list; if that feed is down the alerts come back unfiltered "
            "and `query` is left null."
        ),
    ),
    FeedRoute(
        key="citibike",
        feed=FeedName.CITIBIKE,
        label="Citi Bike",
        handler=_plain(FeedName.CITIBIKE),
        default_limit=2500,
        aliases=("bikes",),
        description="Citi Bike station status from GBFS (Envelope[BikeStation]); 60 s TTL.",
    ),
    FeedRoute(
        key="nyc_311",
        feed=FeedName.NYC_311,
        label="311 requests",
        handler=_nyc_311,
        default_limit=1000,
        aliases=("311",),
        description=(
            "Recent 311 service requests (Envelope[ServiceRequest]); 5 minute TTL. "
            "`complaint_type` is a case-insensitive substring match."
        ),
    ),
    FeedRoute(
        key="dohmh_inspections",
        feed=FeedName.DOHMH_INSPECTIONS,
        label="Restaurant inspections",
        handler=_plain(FeedName.DOHMH_INSPECTIONS),
        default_limit=500,
        aliases=("inspections",),
        description="DOHMH restaurant inspections (Envelope[RestaurantInspection]); 6 hour TTL.",
    ),
    FeedRoute(
        key="weather",
        feed=FeedName.WEATHER,
        label="Weather",
        handler=_plain(FeedName.WEATHER),
        default_limit=10,
        default_radius_m=50_000,
        description="NWS observations + short forecast (Envelope[WeatherReport]); 5 minute TTL.",
    ),
    FeedRoute(
        key="mta_bus",
        feed=FeedName.MTA_BUS,
        label="Buses",
        handler=_plain(FeedName.MTA_BUS),
        default_limit=1000,
        aliases=("bus",),
        description=(
            "Key-gated MTA Bus Time positions (Envelope[BusVehicle]). Without "
            'MTA_BUS_TIME_API_KEY this is status="error", kind="not_configured".'
        ),
    ),
    FeedRoute(
        key="ny511_cameras",
        feed=FeedName.NY511_CAMERAS,
        label="511NY cameras",
        handler=_plain(FeedName.NY511_CAMERAS),
        default_limit=1000,
        aliases=("ny511",),
        description=(
            "Key-gated 511NY camera list (Envelope[Camera]). Without NY511_API_KEY this is "
            'status="error", kind="not_configured".'
        ),
    ),
)

ROUTE_BY_KEY: dict[str, FeedRoute] = {}
for _route in ROUTES:
    ROUTE_BY_KEY[_route.key] = _route
    for _alias in _route.aliases:
        ROUTE_BY_KEY[_alias] = _route

FEED_KEYS: tuple[str, ...] = tuple(r.key for r in ROUTES)
"""Canonical endpoint names, in dashboard order."""

DEFAULT_STREAM_KEYS: tuple[str, ...] = (
    "subway_arrivals",
    "density",
    "nyc_311",
    "citibike",
    "weather",
    "dot_cameras",
)
"""What `/api/stream` pushes when no `feeds` argument is given: the map layers."""
