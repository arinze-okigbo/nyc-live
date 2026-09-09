"""nyc-mcp: FastMCP server exposing NYC live civic data as tools.

Every data tool returns a `nyc_live.contracts.Envelope` serialised with
`model_dump(mode="json")`:

    {"feed": "...", "status": "fresh" | "stale" | "error",
     "fetched_at": ..., "stale_after": ..., "records": [...],
     "error": null | {"kind": ..., "message": ..., ...},
     "query": null | {"lat", "lon", "radius_m"},
     "total_before_filter": N, "truncated": bool}

* `fresh`  the snapshot is younger than the feed's TTL.
* `stale`  the TTL expired and the last refresh failed; `records` is the last good
           snapshot (see `fetched_at`) and `error` explains why it could not be refreshed.
* `error`  no usable data; `records` is empty and `error` says why. Tools never
           return fabricated or silently empty results.

Tool results are never flagged `isError` for feed failures; the envelope `status`
is the signal. `isError` is reserved for invalid arguments.

Construction: `create_server()` builds a server whose lifespan opens the shared
`Services` (one httpx client, the feed registry, the camera frame source, and the
DuckDB store) on first session and closes them on shutdown. Tests pass a
pre-built `Services` so no network or on-disk DuckDB is touched.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import TextContent
from pydantic import BaseModel

from nyc_live import services as svc_api
from nyc_live.config import Settings
from nyc_live.contracts import (
    Camera,
    CameraFrame,
    Envelope,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    GeoQuery,
    now_utc,
)
from nyc_live.services import Services

log = logging.getLogger(__name__)

SERVER_NAME = "nyc-live"

INSTRUCTIONS = """\
Live New York City civic data: DOT traffic cameras (list + JPEG frames), MTA subway
arrivals and alerts, MTA bus positions, subway route polylines, Citi Bike station
status, 311 service requests, NWS weather, and a read-only DuckDB warehouse of
collected telemetry. Every tool returns an envelope with
`status` ("fresh", "stale", or "error"), `fetched_at`, `stale_after`, `records`, and
`error`. Always check `status`: "stale" means the records are the last good snapshot and
`error` says why the refresh failed; "error" means there is no usable data. Pass `lat`,
`lon`, and `radius_m` (metres) to filter located records by distance; results come back
nearest first with `distance_m` set. Coordinates are WGS84; Manhattan is roughly
lat 40.70-40.88, lon -74.02 to -73.91.
"""

TOOL_NAMES: tuple[str, ...] = (
    "list_cameras",
    "nearby_cameras",
    "get_camera_frame",
    "subway_arrivals",
    "subway_alerts",
    "subway_route_shapes",
    "bus_positions",
    "citibike_status",
    "nearby_311",
    "weather_now",
    "query_warehouse",
    "feed_health",
    "density_now",
    "density_history",
    "camera_density_history",
)


class _State:
    """Holds the Services for the tools; built lazily by the lifespan unless injected."""

    def __init__(self, services: Services | None, settings: Settings | None) -> None:
        self.services = services
        self.settings = settings
        self.owned = services is None

    def get(self) -> Services:
        if self.services is None:
            self.services = svc_api.build_services(self.settings)
        return self.services

    async def close(self) -> None:
        if self.owned and self.services is not None:
            await self.services.aclose()
            self.services = None


def _dump(env: Envelope[Any]) -> dict[str, Any]:
    return env.model_dump(mode="json")


def _query(lat: float | None, lon: float | None, radius_m: float | None) -> GeoQuery | None:
    try:
        return svc_api.geo_query(lat, lon, radius_m)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # pydantic range validation on GeoQuery
        raise ToolError(f"invalid lat/lon/radius_m: {exc}") from exc


def _error_env(
    feed: FeedName, message: str, kind: ErrorKind, model: type[BaseModel]
) -> Envelope[Any]:
    return Envelope[model](  # type: ignore[valid-type]
        feed=feed,
        status="error",
        fetched_at=None,
        stale_after=None,
        records=[],
        error=FeedUnavailable(feed, message, kind=kind).to_model(),
    )


def _frame_result(env: Envelope[Any], frame: CameraFrame | None) -> ToolResult:
    payload = _dump(env)
    text = TextContent(type="text", text=json.dumps(payload))
    if frame is None:
        return ToolResult(content=[text], structured_content=payload)
    image = Image(data=frame.data, format="jpeg").to_image_content(mime_type=frame.content_type)
    return ToolResult(content=[image, text], structured_content=payload)


def _keep(env: Envelope[Any], predicate: Any) -> Envelope[Any]:
    """Apply an attribute filter to a non-error envelope, preserving total_before_filter."""
    if env.status == "error":
        return env
    total = env.total_before_filter if env.total_before_filter is not None else len(env.records)
    return env.model_copy(
        update={"records": [r for r in env.records if predicate(r)], "total_before_filter": total}
    )


def create_server(services: Services | None = None, *, settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server. Pass `services` to bypass the lifespan (tests)."""
    state = _State(services, settings)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            svc = state.get()
            log.info(
                "nyc-mcp ready: %d feeds, store=%s",
                len(svc.registry.names()),
                "none" if svc.store is None else ("read-only" if svc.store.read_only else "rw"),
            )
            yield {"services": svc}
        finally:
            await state.close()

    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan)
    _register_camera_tools(mcp, state)
    _register_subway_tools(mcp, state)
    _register_civic_tools(mcp, state)
    _register_store_tools(mcp, state)
    return mcp


# ---------------------------------------------------------------------------- cameras


def _register_camera_tools(mcp: FastMCP, state: _State) -> None:
    async def cameras() -> Envelope[Camera]:
        return await state.get().registry[FeedName.DOT_CAMERAS].get()

    @mcp.tool
    async def list_cameras(
        *,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 1000,
        limit: int = 200,
        online_only: bool = False,
    ) -> dict[str, Any]:
        """List NYC DOT traffic cameras (id, name, lat/lon, online flag, image URL).

        Returns an Envelope[Camera]. The camera list refreshes every 10 minutes
        (`stale_after`); camera ids are upstream UUIDs that can rotate, so never cache
        them across days. With `lat`/`lon`, only cameras within `radius_m` are returned,
        nearest first with `distance_m` set; otherwise the first `limit` cameras.
        `status="stale"` means the list could not be refreshed and these are the last
        known cameras (see `fetched_at`); `status="error"` means no list is available
        and `error.message` says why (e.g. upstream blocked). Use `get_camera_frame`
        with a returned `id` to see the current image.
        """
        env = svc_api.nearby(await cameras(), _query(lat, lon, radius_m), limit=None)
        if online_only:
            env = _keep(env, lambda c: c.is_online)
        return _dump(svc_api.nearby(env, None, limit=limit))

    @mcp.tool
    async def nearby_cameras(
        *, lat: float, lon: float, radius_m: float = 1000, limit: int = 20
    ) -> dict[str, Any]:
        """Cameras within `radius_m` metres of a point, nearest first (Envelope[Camera]).

        Same data and freshness as `list_cameras` (10 minute TTL) but `lat`/`lon` are
        required. `distance_m` is set on every record. `status="stale"` means the camera
        list is the last good snapshot; `status="error"` means the list is unavailable
        and `error.message` explains why.
        """
        return _dump(svc_api.nearby(await cameras(), _query(lat, lon, radius_m), limit=limit))

    @mcp.tool
    async def get_camera_frame(
        *,
        camera_id: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 1000,
    ) -> ToolResult:
        """Fetch the current JPEG frame from one DOT camera as an image block.

        Give a `camera_id` (from `list_cameras` / `nearby_cameras`) or a `lat`/`lon`, in
        which case the nearest online camera within `radius_m` is used. Returns the image
        as MCP image content plus an Envelope[CameraFrame] with `camera_id`,
        `fetched_at`, `stale_after` (about 2 s later: DOT frames refresh every ~2 s and
        the server never fetches one camera faster than that), `content_type`,
        `byte_size`, `width`, `height`. Frames are held in memory only and never written
        to disk. `status="error"` with `error.kind="not_found"` means the id is unknown
        or rotated (re-run `list_cameras`); other kinds mean the upstream failed and no
        image is returned.
        """
        svc = state.get()
        query = _query(lat, lon, radius_m)
        if camera_id is None:
            if query is None:
                raise ToolError("give camera_id, or lat and lon to pick the nearest camera")
            cams = await cameras()
            if cams.status == "error":
                return _frame_result(cams, None)
            cam = svc_api.nearest_camera(cams, query)
            if cam is None:
                env = _error_env(
                    FeedName.DOT_CAMERA_FRAMES,
                    f"no online camera within {query.radius_m:.0f} m of "
                    f"({query.lat:.5f}, {query.lon:.5f}); widen radius_m",
                    ErrorKind.NOT_FOUND,
                    CameraFrame,
                )
                return _frame_result(env.model_copy(update={"query": query}), None)
            camera_id = cam.id
        frame_env = await svc_api.camera_frame(svc.frames, camera_id, store=svc.store)
        frame = frame_env.records[0] if frame_env.records else None
        return _frame_result(frame_env, frame)


# ---------------------------------------------------------------------------- subway


def _register_subway_tools(mcp: FastMCP, state: _State) -> None:
    @mcp.tool
    async def subway_arrivals(
        *,
        stop_id: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 500,
        horizon_s: int = 1800,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Upcoming subway arrivals (Envelope[SubwayArrival]), soonest first.

        Built from the MTA GTFS-realtime trip updates (30 s TTL) joined to the static
        stop list. Filter by `stop_id` (a GTFS stop id such as "127" for Times Sq-42 St
        or a platform id like "127N"), by `lat`/`lon`/`radius_m` (all platforms within
        the radius, `distance_m` set), or both. Each record has `route_id`,
        `direction` ("N"/"S"), `arrival` (UTC), `eta_s` seconds from now, and the stop's
        name and coordinates. Only arrivals within `horizon_s` seconds are included.
        `status="stale"` means the realtime feed could not be refreshed and ETAs come
        from the last good snapshot at `fetched_at` (treat them as approximate);
        `status="error"` means neither the trips nor the stops feed is usable.
        """
        registry = state.get().registry
        trips, stops = await asyncio.gather(
            registry[FeedName.MTA_SUBWAY].get(), registry[FeedName.MTA_SUBWAY_STOPS].get()
        )
        env = svc_api.subway_arrivals(
            trips,
            stops,
            query=_query(lat, lon, radius_m),
            stop_id=stop_id,
            horizon=timedelta(seconds=max(horizon_s, 0)),
            limit=limit,
        )
        return _dump(env)

    @mcp.tool
    async def subway_alerts(
        *,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 800,
        route_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Active MTA subway service alerts (Envelope[SubwayAlert]); 60 s TTL.

        Each record has `header`, `description`, `routes`, `stop_ids`, `effect`, and
        `active_from`/`active_until`. `route_id` (e.g. "A", "7") keeps alerts naming that
        route. With `lat`/`lon`, alerts are kept when they name a route or stop served
        within `radius_m` (the join goes through the static stop list; if that list is
        down the alerts are returned unfiltered and `query` is left null).
        `status="stale"` means these are the last alerts fetched at `fetched_at`;
        `status="error"` means the alerts feed is unavailable.
        """
        registry = state.get().registry
        query = _query(lat, lon, radius_m)
        env = await registry[FeedName.MTA_SUBWAY_ALERTS].get()
        if query is not None:
            stops = await registry[FeedName.MTA_SUBWAY_STOPS].get()
            env = svc_api.alerts_near(env, stops, query)
        if route_id is not None:
            wanted = route_id.upper()
            env = _keep(env, lambda a: wanted in a.routes)
        return _dump(svc_api.nearby(env, None, limit=limit))

    @mcp.tool
    async def subway_route_shapes(
        *, route_id: str | None = None, limit: int = 500
    ) -> dict[str, Any]:
        """Static GTFS route polylines for drawing subway lines on a map (Envelope[SubwayRouteShape]); 24 hour TTL.

        Each record is one `shape_id`'s ordered `points` list of `(lat, lon)` pairs plus
        `route_id` and `direction` ("N"/"S"). A route (e.g. "1", "A") has MANY shapes --
        branches, express/local segments, both directions, from 2 up to 35 in the live
        bundle -- never one polyline per route; group by `route_id` to draw all of a
        route's lines, or use `shape_id` to draw one exact path. A shape has no single
        lat/lon of its own (it is a polyline, not a point), so `lat`/`lon`/`radius_m`
        geo-filtering is not offered here -- filter by `route_id` instead (e.g. "1" for
        every shape of the 1 train; case-sensitive as GTFS publishes it). `status="stale"`
        means these are the last-fetched shapes (rare, given the 24 h TTL) and `error`
        says why the refresh failed; `status="error"` means the static GTFS bundle could
        not be fetched or parsed.
        """
        env = await state.get().registry[FeedName.MTA_SUBWAY_SHAPES].get()
        if route_id is not None:
            env = _keep(env, lambda s: s.route_id == route_id)
        return _dump(svc_api.nearby(env, None, limit=limit))

    @mcp.tool
    async def bus_positions(
        *,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 1000,
        route_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Live MTA bus positions (Envelope[BusVehicle]) from SIRI VehicleMonitoring; 30 s TTL.

        Each record is one bus's current `lat`/`lon` plus `vehicle_id`, `route_id`,
        `trip_id`, `bearing`, `timestamp`, and -- present on about 99.9% of active buses,
        `null` on the rest -- `next_stop_id`, `next_stop_name`, `next_stop_eta`,
        `next_stop_distance_m`, `stops_away`, and `occupancy` (a free-text string such as
        "manySeatsAvailable"). `route_id` keeps buses whose raw SIRI line ref contains the
        given text case-insensitively (e.g. "M15" matches the upstream's "MTA NYCT_M15").
        With `lat`/`lon`, only buses within `radius_m` are returned, nearest first with
        `distance_m` set; otherwise the first `limit` of all active buses system-wide.
        This feed is key-gated on `MTA_BUS_TIME_API_KEY`: until that env var is set,
        every call returns `status="error"` with `error.kind="not_configured"` and no
        records -- that is expected, not a bug. `status="stale"` means the last good
        positions are being served (see `fetched_at`) and `error` explains why the
        refresh failed; any other `status="error"` means MTA Bus Time is unreachable or
        rejected the key.
        """
        env = await state.get().registry[FeedName.MTA_BUS].get()
        if route_id is not None:
            wanted = route_id.upper()
            env = _keep(env, lambda b: b.route_id is not None and wanted in b.route_id.upper())
        return _dump(svc_api.nearby(env, _query(lat, lon, radius_m), limit=limit))


# ---------------------------------------------------------------------------- civic


def _register_civic_tools(mcp: FastMCP, state: _State) -> None:
    @mcp.tool
    async def citibike_status(
        *,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 1000,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Citi Bike station status (Envelope[BikeStation]) from the GBFS feed; 60 s TTL.

        Each record has `name`, `capacity`, `bikes_available`, `ebikes_available`,
        `docks_available`, `is_renting`, `is_returning`, `last_reported`, and lat/lon.
        With `lat`/`lon`, stations within `radius_m` nearest first (`distance_m` set);
        without, the first `limit` of ~2,000 stations. `status="stale"` means the counts
        are from the last good snapshot at `fetched_at`; `status="error"` means GBFS is
        unreachable and `error.message` explains why.
        """
        env = await state.get().registry[FeedName.CITIBIKE].get()
        return _dump(svc_api.nearby(env, _query(lat, lon, radius_m), limit=limit))

    @mcp.tool
    async def nearby_311(
        *,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 1000,
        limit: int = 50,
        complaint_type: str | None = None,
    ) -> dict[str, Any]:
        """Recent NYC 311 service requests (Envelope[ServiceRequest]); 5 minute TTL.

        Records carry `created_at`, `closed_at`, `agency`, `complaint_type`, `descriptor`,
        `status`, `borough`, `incident_address`, and lat/lon when the city geocoded the
        request (ungeocoded requests are dropped when a geo filter is applied).
        `complaint_type` is a case-insensitive substring match (e.g. "noise").
        `status="stale"` means the last good snapshot is being served (see `fetched_at`
        and `error`); `status="error"` means the Socrata endpoint is unavailable.
        """
        env = await state.get().registry[FeedName.NYC_311].get()
        if complaint_type:
            needle = complaint_type.lower()
            env = _keep(env, lambda r: needle in r.complaint_type.lower())
        return _dump(svc_api.nearby(env, _query(lat, lon, radius_m), limit=limit))

    @mcp.tool
    async def weather_now(
        *,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 50_000,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Current NWS observations, short forecast, and active alerts for NYC (Envelope[WeatherReport]).

        One record per weather station (Central Park, LaGuardia, JFK, Newark, ...) with the
        latest `observation` (temperature_c, humidity_pct, wind_speed_kmh, text, ...), a
        short `forecast` list, and `alerts` -- any currently active NWS alerts.weather.gov
        entries for the area (heat advisories, flood warnings, etc.), each with `event`,
        `headline`, `severity`, `effective`, and `expires`. An empty `alerts` list is the
        normal case (no alert issued), not a failure; always check it for severe weather.
        5 minute TTL. With `lat`/`lon`, stations within `radius_m` nearest first; the
        default radius is 50 km so the nearest station is always included.
        `status="stale"` means the observation is from the last good fetch at
        `fetched_at`; `status="error"` means api.weather.gov is unavailable.
        """
        env = await state.get().registry[FeedName.WEATHER].get()
        return _dump(svc_api.nearby(env, _query(lat, lon, radius_m), limit=limit))


# ---------------------------------------------------------------------------- store-backed


def _register_store_tools(mcp: FastMCP, state: _State) -> None:
    def camera_fallback() -> list[Camera]:
        snap = state.get().registry[FeedName.DOT_CAMERAS].snapshot
        return list(snap.records) if snap else []

    @mcp.tool
    async def query_warehouse(*, sql: str, max_rows: int = 500) -> dict[str, Any]:
        """Run a read-only SQL query against the local DuckDB telemetry warehouse.

        Returns an Envelope[WarehouseResult] with one record: `columns`, `rows`,
        `row_count`, `truncated` (true when more than `max_rows` matched), `elapsed_ms`.
        Only a single SELECT/WITH statement over these tables is allowed: cameras,
        camera_frame_fetches, density_samples, feed_fetches, bike_station_status,
        weather_observations, service_requests_311, subway_positions. DML/DDL and other
        tables are rejected with `status="error"`, `error.kind="internal"`, and the reason
        in `error.message`. Timestamps are returned as ISO-8601 UTC strings. Results are
        computed at call time (`stale_after == fetched_at`), so re-run rather than cache.
        The warehouse is populated by nyc-vision and the feed telemetry; tables may be
        empty on a fresh install.
        """
        store = state.get().store
        return _dump(svc_api.warehouse(store, sql, max_rows=max(1, min(max_rows, 5000))))

    @mcp.tool
    async def feed_health() -> dict[str, Any]:
        """Health of every registered feed plus the DuckDB store, without fetching anything.

        Returns `{"checked_at", "store": {"path", "open", "read_only"}, "feeds": [...]}`
        where each feed has `feed`, `configured` (false for key-gated feeds whose env var
        is unset), `status` ("fresh", "stale", "error", or "never_fetched" if no tool has
        touched it yet), `ttl_s`, `last_ok_at`, `last_error`, `consecutive_failures`, and
        `record_count`. Call this first when other tools return `status="error"` to see
        whether the problem is one feed or all of them.
        """
        svc = state.get()
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

    @mcp.tool
    async def density_now(
        *,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 1000,
        camera_id: str | None = None,
        window_s: int = 300,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Current pedestrian / vehicle density per camera from nyc-vision detections.

        Returns an Envelope[CameraDensity]: for each camera with samples in the trailing
        `window_s` seconds, `person_mean`, `vehicle_mean`, `person_max`, `vehicle_max`
        (counts per frame), `sample_count` (frames), `latest_ts`, name and lat/lon.
        Aggregates only; no imagery is stored. 60 s TTL. Until nyc-vision (Phase 3) has
        written `density_samples` rows this returns `status="error"` with
        `error.kind="not_configured"` and a message saying so; that is expected, not a
        bug. `error.kind="internal"` means the DuckDB file could not be opened.
        """
        env = svc_api.density_now(
            state.get().store,
            _query(lat, lon, radius_m),
            window=timedelta(seconds=max(window_s, 1)),
            camera_id=camera_id,
            limit=limit,
            cameras=camera_fallback(),
        )
        return _dump(env)

    @mcp.tool
    async def density_history(
        *,
        camera_id: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 1000,
        hours: float = 24,
        bucket_s: int = 900,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Density time series per camera (Envelope[CameraDensity], one record per bucket).

        Buckets the trailing `hours` of nyc-vision samples into `bucket_s`-second windows
        per camera (`window_start`/`window_end`), with per-bucket person/vehicle mean and
        max. Filter by `camera_id` and/or `lat`/`lon`/`radius_m`; `limit` caps the number
        of (camera, bucket) rows and sets `truncated`. Rows are ordered by camera then
        time. Until nyc-vision (Phase 3) has produced samples this returns
        `status="error"`, `error.kind="not_configured"`; `error.kind="internal"` means
        the DuckDB file could not be opened.
        """
        env = svc_api.density_history(
            state.get().store,
            _query(lat, lon, radius_m),
            camera_id=camera_id,
            window=timedelta(hours=max(hours, 0.01)),
            bucket=timedelta(seconds=max(bucket_s, 1)),
            limit=limit,
            cameras=camera_fallback(),
        )
        return _dump(env)

    @mcp.tool
    async def camera_density_history(
        *,
        camera_id: str,
        since_s: int = 3600,
        bucket_s: int | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Density trend for one camera, ready for a trend chart (Envelope[CameraDensity]).

        One record per time bucket over the trailing `since_s` seconds (default 1 hour)
        for `camera_id`, each with `person_mean`, `vehicle_mean`, `person_max`,
        `vehicle_max` (counts per frame), `sample_count` (frames), `window_start`,
        `window_end`, and `latest_ts`. Bucket size defaults to roughly `since_s / 120`
        (minimum 30 s) so a chart gets about 120 points; pass `bucket_s` to override.
        This is `density_history` narrowed to one required camera with chart-friendly
        bucketing; use `density_history` directly for multi-camera or geo-filtered
        queries. `status="error"`, `error.kind="not_configured"` until nyc-vision has
        written `density_samples` for this camera; `error.kind="internal"` means
        `camera_id` was empty or the DuckDB file could not be opened.
        """
        env = svc_api.camera_density_history(
            state.get().store,
            camera_id,
            since_s=max(since_s, 1),
            bucket_s=bucket_s,
            limit=limit,
            cameras=camera_fallback(),
        )
        return _dump(env)
