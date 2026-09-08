"""Server-Sent Events push for the dashboard, plus the documented polling fallback.

Wire format (`text/event-stream`, one blank line between events):

    event: ready
    data: {"feeds": ["subway_arrivals", ...], "interval_s": 15.0, "server_time": "..."}

    event: subway_arrivals
    data: {"feed": "mta_subway", "status": "fresh", ...}      <- the Envelope, unchanged

    event: health
    data: {"checked_at": ..., "store": {...}, "feeds": [...]}  <- same as /api/health

    : keepalive                                               <- comment, every ~15 s

One event per feed per cycle, then a `health` event, then a wait of `interval_s`.
A feed that fails is pushed as its own `status="error"` envelope: the cycle keeps
going, so one dead feed never stops the others from updating.

The stream does not fetch upstreams itself; it calls the same handlers as
`/api/<feed>`, and `CachedFeed` still enforces each feed's TTL. Polling faster
than a TTL just re-serves the cached snapshot.

POLLING FALLBACK
----------------
`/api/stream` is an optimisation, not a requirement. Every event name is also a
`GET /api/<feed>` endpoint returning the identical envelope, so a client that
cannot hold an EventSource open (no SSE support, a buffering proxy, a stream
error) polls those endpoints on a timer instead. `static/app.js` does exactly
that: it switches to `setInterval` polling on the first `EventSource` error and
shows "polling" in the connection pill.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from starlette.requests import Request

from nyc_dash.api import FeedRoute, Params, crashed, health_payload
from nyc_live.contracts import now_utc
from nyc_live.services import Services

log = logging.getLogger(__name__)

SSE_HEADERS = {
    "cache-control": "no-store",
    "connection": "keep-alive",
    "x-accel-buffering": "no",  # tell nginx not to buffer the stream
}

DEFAULT_INTERVAL_S = 15.0
MIN_INTERVAL_S = 0.5
MAX_INTERVAL_S = 300.0
_SLICE_S = 0.25
_KEEPALIVE_S = 15.0


def format_event(event: str, data: Any) -> str:
    payload = json.dumps(data, separators=(",", ":"))
    body = "".join(f"data: {line}\n" for line in payload.split("\n"))
    return f"event: {event}\n{body}\n"


async def _wait(request: Request, seconds: float) -> AsyncIterator[str]:
    """Sleep in slices, emitting keepalive comments and bailing out on disconnect."""
    waited = 0.0
    since_keepalive = 0.0
    while waited < seconds:
        if await request.is_disconnected():
            return
        step = min(_SLICE_S, seconds - waited)
        await asyncio.sleep(step)
        waited += step
        since_keepalive += step
        if since_keepalive >= _KEEPALIVE_S and waited < seconds:
            since_keepalive = 0.0
            yield ": keepalive\n\n"


async def event_stream(
    request: Request,
    get_services: Callable[[], Services],
    feeds: Sequence[tuple[FeedRoute, Params]],
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    cycles: int = 0,
) -> AsyncIterator[str]:
    """Yield SSE frames until the client disconnects (or `cycles` cycles have run).

    `feeds` pairs each route with its own `Params` because per-feed defaults
    (radius, limit) differ.
    """
    yield format_event(
        "ready",
        {
            "feeds": [r.key for r, _ in feeds],
            "interval_s": interval_s,
            "cycles": cycles or None,
            "server_time": now_utc().isoformat(),
        },
    )
    done = 0
    while True:
        for route, params in feeds:
            if await request.is_disconnected():
                return
            try:
                env = await route.handler(get_services(), params)
                data = env.model_dump(mode="json")
            except Exception as exc:  # a broken handler must not kill the whole stream
                log.exception("stream handler for %s failed", route.key)
                data = crashed(route.feed, exc).model_dump(mode="json")
            yield format_event(route.key, data)
        try:
            yield format_event("health", health_payload(get_services()))
        except Exception:
            log.exception("stream health payload failed")
        done += 1
        if cycles and done >= cycles:
            return
        if await request.is_disconnected():
            return
        async for frame in _wait(request, interval_s):
            yield frame
        if await request.is_disconnected():
            return
