"""nyc-dash: FastAPI app serving the live NYC dashboard over the nyc-live service layer.

Endpoints
---------
* `GET /api` — the route table (key, label, feed, geo support, description).
* `GET /api/<feed>` — the feed's `contracts.Envelope`, serialised unchanged:
  `status`, `fetched_at`, `stale_after`, `records`, `error`, `query`,
  `total_before_filter`, `truncated`. Optional `lat`/`lon`/`radius_m`/`limit`
  wherever the service layer supports filtering, plus per-feed extras
  (`stop_id`, `horizon_s`, `complaint_type`, `camera_id`, `window_s`).
  A down feed is HTTP 200 with `status="error"`, not an HTTP error: the browser
  needs the envelope to render the layer's status pill. HTTP 4xx is reserved for
  a bad request (unknown feed key, lat without lon, geo filter on a feed whose
  records have no coordinates).
* `GET /api/health` — `FeedRegistry.health()` plus the DuckDB store state.
* `GET /api/stream` — Server-Sent Events; see `nyc_dash.stream` for the wire
  format and the polling fallback.
* `/` — the static single-page dashboard (`static/index.html`, `app.js`, `style.css`).

This process never touches an upstream directly: it reads `nyc_live.services`
only, and every refresh decision belongs to `CachedFeed`. Records are never
fabricated; a feed with no usable data comes back empty with the error attached.

Construction: `create_app()` builds the `Services` (one httpx client, the feed
registry, the frame source, the DuckDB store) in the lifespan and closes them on
shutdown. Tests pass a pre-built `Services` so no network or on-disk DuckDB is
touched. `nyc_dash.app:app` is the import string for `uvicorn --reload`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from nyc_dash import api
from nyc_dash.api import FEED_KEYS, ROUTE_BY_KEY, ROUTES, FeedRoute, Params
from nyc_dash.stream import (
    DEFAULT_INTERVAL_S,
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    SSE_HEADERS,
    event_stream,
)
from nyc_live import services as svc_api
from nyc_live.config import Settings
from nyc_live.contracts import Envelope, GeoQuery
from nyc_live.services import Services

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

TITLE = "nyc-dash"
DESCRIPTION = (
    "Live NYC civic data over the nyc-live service layer. Every /api/<feed> response is a "
    'contracts.Envelope: check `status` ("fresh", "stale", "error"). "stale" means `records` '
    "is the last good snapshot taken at `fetched_at` and `error` says why it could not be "
    'refreshed; "error" means there is no usable data and `records` is empty. Nothing is ever '
    "filled in with placeholder data."
)


class _State:
    """Holds the Services for the routes; built lazily by the lifespan unless injected."""

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


def _envelope_response(env: Envelope[Any]) -> JSONResponse:
    return JSONResponse(env.model_dump(mode="json"), headers={"cache-control": "no-store"})


def _resolve(feed: str) -> FeedRoute:
    route = ROUTE_BY_KEY.get(feed)
    if route is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown feed {feed!r}; available feeds: {', '.join(FEED_KEYS)}",
        )
    return route


def _params(
    route: FeedRoute,
    *,
    lat: float | None,
    lon: float | None,
    radius_m: float | None,
    limit: int | None,
    stop_id: str | None = None,
    camera_id: str | None = None,
    complaint_type: str | None = None,
    horizon_s: int = 1800,
    window_s: int = 300,
) -> Params:
    try:
        query = svc_api.geo_query(lat, lon, radius_m if radius_m is not None else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if query is not None and radius_m is None:
        query = GeoQuery(lat=query.lat, lon=query.lon, radius_m=route.default_radius_m)
    if query is not None and not route.geo:
        raise HTTPException(
            status_code=400,
            detail=(
                f"feed {route.key!r} has no located records; lat/lon filtering is not "
                "supported for it"
            ),
        )
    return Params(
        query=query,
        limit=limit if limit is not None else route.default_limit,
        stop_id=stop_id,
        camera_id=camera_id,
        complaint_type=complaint_type,
        horizon_s=horizon_s,
        window_s=window_s,
    )


def create_app(
    services: Services | None = None,
    *,
    settings: Settings | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    """Build the dashboard app. Pass `services` to bypass the lifespan (tests)."""
    state = _State(services, settings)
    static = static_dir if static_dir is not None else STATIC_DIR

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            svc = state.get()
            log.info(
                "nyc-dash ready: %d feeds, store=%s",
                len(svc.registry.names()),
                "none" if svc.store is None else ("read-only" if svc.store.read_only else "rw"),
            )
            yield
        finally:
            await state.close()

    app = FastAPI(title=TITLE, description=DESCRIPTION, version="0.1.0", lifespan=lifespan)
    app.state.dash = state

    @app.get("/api", summary="List the dashboard feed endpoints")
    async def index() -> JSONResponse:
        return JSONResponse(
            {
                "feeds": [
                    {
                        "key": r.key,
                        "label": r.label,
                        "feed": r.feed.value,
                        "path": f"/api/{r.key}",
                        "geo": r.geo,
                        "aliases": list(r.aliases),
                        "default_limit": r.default_limit,
                        "description": r.description,
                    }
                    for r in ROUTES
                ],
                "health": "/api/health",
                "stream": "/api/stream",
                "stream_default_feeds": list(api.DEFAULT_STREAM_KEYS),
            }
        )

    @app.get("/api/health", summary="Feed registry health (never fetches anything)")
    async def health() -> JSONResponse:
        return JSONResponse(api.health_payload(state.get()), headers={"cache-control": "no-store"})

    @app.get("/api/stream", summary="Server-Sent Events push of every dashboard feed")
    async def stream(
        request: Request,
        *,
        feeds: str | None = Query(
            None, description="Comma-separated feed keys; defaults to the map layers."
        ),
        interval_s: float = Query(
            DEFAULT_INTERVAL_S, ge=MIN_INTERVAL_S, le=MAX_INTERVAL_S, description="Cycle period."
        ),
        cycles: int = Query(
            0, ge=0, le=100_000, description="Stop after N cycles; 0 streams until disconnect."
        ),
        lat: float | None = Query(None, ge=-90, le=90),
        lon: float | None = Query(None, ge=-180, le=180),
        radius_m: float | None = Query(None, gt=0, le=50_000),
        limit: int | None = Query(None, ge=1, le=50_000),
    ) -> StreamingResponse:
        keys = (
            [k.strip() for k in feeds.split(",") if k.strip()]
            if feeds is not None
            else list(api.DEFAULT_STREAM_KEYS)
        )
        if not keys:
            raise HTTPException(status_code=400, detail="feeds must name at least one feed")
        # geo / limit defaults differ per feed, so each route streams with its own Params
        pairs = [
            (r, _params(r, lat=lat, lon=lon, radius_m=radius_m, limit=limit))
            for r in (_resolve(k) for k in keys)
        ]
        body = event_stream(request, state.get, pairs, interval_s=interval_s, cycles=cycles)
        return StreamingResponse(body, media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/api/{feed}", summary="One feed's Envelope, unchanged")
    async def feed_endpoint(
        feed: str,
        *,
        lat: float | None = Query(None, ge=-90, le=90),
        lon: float | None = Query(None, ge=-180, le=180),
        radius_m: float | None = Query(None, gt=0, le=50_000),
        limit: int | None = Query(None, ge=1, le=50_000),
        stop_id: str | None = Query(None, description="subway_arrivals: platform or station id"),
        horizon_s: int = Query(1800, ge=0, le=86_400, description="subway_arrivals: ETA horizon"),
        complaint_type: str | None = Query(None, description="nyc_311: substring match"),
        camera_id: str | None = Query(None, description="density: one camera"),
        window_s: int = Query(300, ge=1, le=86_400, description="density: trailing window"),
    ) -> JSONResponse:
        route = _resolve(feed)
        params = _params(
            route,
            lat=lat,
            lon=lon,
            radius_m=radius_m,
            limit=limit,
            stop_id=stop_id,
            camera_id=camera_id,
            complaint_type=complaint_type,
            horizon_s=horizon_s,
            window_s=window_s,
        )
        try:
            env = await route.handler(state.get(), params)
        except Exception as exc:  # one broken handler must not take down the API
            log.exception("handler for %s failed", route.key)
            env = api.crashed(route.feed, exc)
        return _envelope_response(env)

    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="static")
    else:  # pragma: no cover - only when the package is installed without its assets
        log.warning("static directory %s is missing; only the API is served", static)

    return app


app = create_app()
"""Module-level app for `uvicorn nyc_dash.app:app --reload`. Services are built in the lifespan."""
