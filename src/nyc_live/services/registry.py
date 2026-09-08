"""Build the shared `FeedRegistry` (+ frame source, + store) that nyc-mcp and nyc-dash consume.

One `httpx.AsyncClient` is shared by every adapter and by the camera frame source.
Key-gated adapters (`MTA_BUS`, `NY511_CAMERAS`) are registered too; their
`CachedFeed` reports `status="error"` with `kind=not_configured` until the env
var is set, so consumers can show "not configured" instead of "missing".

Store policy for readers
------------------------
nyc-mcp / nyc-dash are readers. `open_store_for_reader` opens
`settings.duckdb_path` read-only when the file already exists. When it does not
exist yet (fresh checkout, nyc-vision never ran) it is opened writable ONCE so
the frozen schema is created and `query_warehouse` has tables to describe; a
writable store also lets the registry record `feed_fetches` telemetry. When the
file cannot be opened at all (another process holds the write lock) the reader
runs with `store=None` and every store-backed tool returns an honest error
envelope instead of crashing the server.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from nyc_live.cache import DEFAULT_MAX_STALE, CachedFeed, FeedRegistry
from nyc_live.config import Settings, get_settings
from nyc_live.contracts import FeedAdapter, FrameSource
from nyc_live.feeds import load_adapters, load_frame_source
from nyc_live.http import make_client
from nyc_live.store import Store

log = logging.getLogger(__name__)


def build_registry(
    adapters: list[FeedAdapter[Any]],
    *,
    store: Store | None = None,
    max_stale: timedelta = DEFAULT_MAX_STALE,
) -> FeedRegistry:
    """Wrap every adapter in a `CachedFeed`. Telemetry goes to `store` only if it is writable."""
    telemetry = store if store is not None and not store.read_only else None
    return FeedRegistry(CachedFeed(a, store=telemetry, max_stale=max_stale) for a in adapters)


def open_store_for_reader(settings: Settings) -> Store | None:
    """Open the DuckDB file for a reader process; see the module docstring for the policy."""
    path = settings.duckdb_path
    try:
        if path.exists():
            return Store(path, read_only=True)
        log.info("duckdb file %s does not exist yet; creating it with the frozen schema", path)
        return Store(path, read_only=False)
    except Exception as exc:
        log.warning("could not open duckdb store at %s: %s; store-backed tools degrade", path, exc)
        return None


@dataclass
class Services:
    """Everything a consumer needs, built once per process."""

    settings: Settings
    client: httpx.AsyncClient
    registry: FeedRegistry
    frames: FrameSource
    store: Store | None = None
    _owns_client: bool = field(default=False, repr=False)
    _owns_store: bool = field(default=False, repr=False)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
        if self._owns_store and self.store is not None:
            self.store.close()


def build_services(
    settings: Settings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    store: Store | None = None,
    open_store: bool = True,
    strict: bool = False,
) -> Services:
    """Construct the registry + frame source over one shared client.

    `client=None` creates a client that `Services.aclose()` will close. `store=None` with
    `open_store=True` opens `settings.duckdb_path` per `open_store_for_reader`.
    """
    s = settings or get_settings()
    owns_client = client is None
    c = client or make_client(s)
    owns_store = store is None and open_store
    st = store if store is not None else (open_store_for_reader(s) if open_store else None)
    adapters = load_adapters(c, s, strict=strict)
    registry = build_registry(adapters, store=st)
    frames = load_frame_source(c, s)
    return Services(
        settings=s,
        client=c,
        registry=registry,
        frames=frames,
        store=st,
        _owns_client=owns_client,
        _owns_store=owns_store,
    )


@asynccontextmanager
async def open_services(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    open_store: bool = True,
    strict: bool = False,
) -> AsyncIterator[Services]:
    """Async context manager form of `build_services`; closes the client (and owned store)."""
    services = build_services(settings, store=store, open_store=open_store, strict=strict)
    try:
        yield services
    finally:
        await services.aclose()
