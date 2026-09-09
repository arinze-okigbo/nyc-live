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

QUERY PARAMETERS
----------------
Request:

    /api/stream?feeds=mta_bus,density&interval_s=15
        &mta_bus.limit=3000&density.limit=500&density.window_s=900

* `feeds` — comma-separated feed keys; defaults to `api.DEFAULT_STREAM_KEYS`.
* `interval_s`, `cycles` — cadence and (test-only) cycle bound.
* `lat` / `lon` / `radius_m` / `limit` — flat, and apply to *every* streamed feed.
* `<feed_key>.<name>` — the same argument for one feed only, overriding the flat
  value above and the route's own default. `<name>` is any of the `GET
  /api/<feed>` query parameters (`lat`, `lon`, `radius_m`, `limit`, `stop_id`,
  `camera_id`, `complaint_type`, `horizon_s`, `window_s`), validated with the
  same bounds; see `FeedParamOverrides`.

The prefixed form exists because a stream carries many feeds at once and each one
needs its own page size and window: a single flat `limit=` cannot say "3000 buses
but 500 density rows", so a client that asked `GET /api/mta_bus?limit=3000` on
load could not ask for the same thing on the stream, and its layer silently shrank
to the route default on the first push. Flat `name=value` pairs (rather than a
JSON blob) keep the request greppable in an access log and buildable from the same
`URLSearchParams` the polling URLs are built from. An unknown `<name>`, an
out-of-range value, or a prefix naming a feed that is not in `feeds` is a 400 —
never a silently ignored argument, which is what made the original bug invisible.

Sending no parameters at all is unchanged: every feed streams with its route's
default `Params`, exactly as `GET /api/<feed>` with no query string does.

POLLING FALLBACK
----------------
`/api/stream` is an optimisation, not a requirement. Every event name is also a
`GET /api/<feed>` endpoint returning the identical envelope, so a client that
cannot hold an EventSource open (no SSE support, a buffering proxy, a stream
error) polls those endpoints on a timer instead. `static/js/data-sync.js` does
exactly that: it switches to `setInterval` polling when the `EventSource` errors,
shows "polling" in the connection pill, and retries the stream with exponential
backoff until it is live again.

Both transports must carry the *same* arguments or they disagree about what the
data is. `data-sync.js` builds `<feed_key>.<name>` here from the very
`URLSearchParams` it puts in the `GET /api/<feed>` URL, so polling and streaming
are the same request asked two ways.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import QueryParams
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

FEED_PARAM_SEP = "."
"""Separates a feed key from a parameter name in `<feed_key>.<name>=<value>`."""


class FeedParamOverrides(BaseModel):
    """One feed's `/api/stream` arguments, parsed from its `<feed_key>.<name>` params.

    Every field mirrors a `GET /api/<feed>` query parameter, with the same bounds, and
    defaults to None meaning "not given" -- the caller then falls back to the stream's
    flat parameter and finally to the route's own default, so an absent override can
    never be confused with an explicit one. `extra="forbid"` makes a misspelled name a
    400 rather than an argument that quietly does nothing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_m: float | None = Field(default=None, gt=0, le=50_000)
    limit: int | None = Field(default=None, ge=1, le=50_000)
    stop_id: str | None = None
    camera_id: str | None = None
    complaint_type: str | None = None
    horizon_s: int | None = Field(default=None, ge=0, le=86_400)
    window_s: int | None = Field(default=None, ge=1, le=86_400)


def _describe(key: str, exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        name = ".".join(str(loc) for loc in err["loc"]) or "?"
        parts.append(f"{key}{FEED_PARAM_SEP}{name}: {err['msg']}")
    return "; ".join(parts)


def parse_feed_overrides(
    query_params: QueryParams, keys: Iterable[str]
) -> dict[str, FeedParamOverrides]:
    """Group `<feed_key>.<name>=<value>` params by feed and validate each group.

    Returns one entry per requested key (an all-None `FeedParamOverrides` when that
    feed was given no arguments). Raises `ValueError` -- which the route turns into a
    400 -- for a prefix that is not one of `keys`, an unknown name, or a value outside
    the documented range.
    """
    grouped: dict[str, dict[str, str]] = {key: {} for key in keys}
    for raw_name, value in query_params.multi_items():
        prefix, sep, name = raw_name.partition(FEED_PARAM_SEP)
        if not sep:
            continue  # a flat parameter (feeds, interval_s, cycles, lat, ...)
        if prefix not in grouped:
            raise ValueError(
                f"{raw_name!r} sets a parameter for feed {prefix!r}, which is not in `feeds` "
                f"({', '.join(grouped) or 'none'})"
            )
        grouped[prefix][name] = value
    parsed: dict[str, FeedParamOverrides] = {}
    for key, fields in grouped.items():
        try:
            parsed[key] = FeedParamOverrides(**fields)
        except ValidationError as exc:
            raise ValueError(_describe(key, exc)) from exc
    return parsed


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
