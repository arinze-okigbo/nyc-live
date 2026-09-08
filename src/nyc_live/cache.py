"""Snapshot cache + feed registry: single-flight refresh, TTL, stale-with-error.

This is the layer that turns "adapter raised" into an honest Envelope. Policy:

* fresh   : snapshot younger than TTL. Served without touching upstream.
* stale   : TTL expired, refresh failed, an older snapshot exists. Served WITH
            the refresh error attached so consumers can show "last good: 12:04".
* error   : no snapshot ever succeeded (or max_stale exceeded). records == [].

Never returns fabricated or partially fabricated data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from nyc_live.contracts import (
    DEFAULT_TTL,
    Envelope,
    ErrorKind,
    FeedAdapter,
    FeedError,
    FeedHealth,
    FeedName,
    FeedStatus,
    FeedUnavailable,
    Snapshot,
    now_utc,
)
from nyc_live.store import Store

log = logging.getLogger(__name__)

DEFAULT_MAX_STALE = timedelta(minutes=30)


class CachedFeed[RecordT: BaseModel]:
    def __init__(
        self,
        adapter: FeedAdapter[RecordT],
        *,
        store: Store | None = None,
        max_stale: timedelta = DEFAULT_MAX_STALE,
    ) -> None:
        self.adapter = adapter
        self.name: FeedName = adapter.name
        self.ttl: timedelta = adapter.ttl or DEFAULT_TTL[adapter.name]
        self.max_stale = max_stale
        self.store = store
        self._snapshot: Snapshot[RecordT] | None = None
        self._last_error: FeedError | None = None
        self._consecutive_failures = 0
        self._refresh_lock = asyncio.Lock()

    # -- reads -------------------------------------------------------------

    @property
    def snapshot(self) -> Snapshot[RecordT] | None:
        return self._snapshot

    def _is_fresh(self) -> bool:
        return self._snapshot is not None and now_utc() < self._snapshot.stale_after

    async def get(self, *, force: bool = False) -> Envelope[RecordT]:
        if not force and self._is_fresh():
            return self._envelope("fresh")
        async with self._refresh_lock:
            # another caller may have refreshed while we waited
            if not force and self._is_fresh():
                return self._envelope("fresh")
            await self._refresh()
        return self._envelope(self._status_after_refresh())

    async def _refresh(self) -> None:
        if not self.adapter.is_configured():
            err = FeedUnavailable(
                self.name, "feed not configured (missing key)", kind=ErrorKind.NOT_CONFIGURED
            ).to_model()
            self._last_error = err
            return
        started = time.perf_counter()
        try:
            snap = await self.adapter.fetch()
        except FeedUnavailable as exc:
            self._consecutive_failures += 1
            self._last_error = exc.to_model()
            log.warning("feed %s refresh failed: %s", self.name.value, exc)
            self._record(ok=False, started=started, error=self._last_error)
            return
        except Exception as exc:
            self._consecutive_failures += 1
            self._last_error = FeedUnavailable(
                self.name, f"{type(exc).__name__}: {exc}", kind=ErrorKind.INTERNAL
            ).to_model()
            log.exception("feed %s refresh crashed", self.name.value)
            self._record(ok=False, started=started, error=self._last_error)
            return
        self._snapshot = snap
        self._last_error = None
        self._consecutive_failures = 0
        self._record(ok=True, started=started, count=len(snap.records))

    def _status_after_refresh(self) -> FeedStatus:
        if self._last_error is None and self._snapshot is not None:
            return "fresh"
        if self._snapshot is not None and now_utc() - self._snapshot.fetched_at <= (
            self.ttl + self.max_stale
        ):
            return "stale"
        return "error"

    def _envelope(self, status: FeedStatus) -> Envelope[RecordT]:
        snap = self._snapshot if status != "error" else None
        return Envelope[RecordT](
            feed=self.name,
            status=status,
            fetched_at=snap.fetched_at if snap else None,
            stale_after=snap.stale_after if snap else None,
            records=list(snap.records) if snap else [],
            error=self._last_error if status != "fresh" else None,
            total_before_filter=len(snap.records) if snap else None,
        )

    def health(self) -> FeedHealth:
        status: Any
        if self._snapshot is None and self._last_error is None:
            status = "never_fetched"
        elif self._snapshot is None:
            status = "error"
        else:
            status = "fresh" if self._is_fresh() else self._status_after_refresh()
        return FeedHealth(
            feed=self.name,
            configured=self.adapter.is_configured(),
            status=status,
            ttl_s=self.ttl.total_seconds(),
            last_ok_at=self._snapshot.fetched_at if self._snapshot else None,
            last_error=self._last_error,
            consecutive_failures=self._consecutive_failures,
            record_count=len(self._snapshot.records) if self._snapshot else None,
        )

    def _record(
        self,
        *,
        ok: bool,
        started: float,
        count: int | None = None,
        error: FeedError | None = None,
    ) -> None:
        if self.store is None:
            return
        try:
            self.store.record_feed_fetch(
                self.name,
                ok=ok,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                record_count=count,
                error=error,
            )
        except Exception:
            log.exception("failed to record feed_fetches row for %s", self.name.value)


class FeedRegistry:
    """All cached feeds in one place. Shared by nyc-mcp and nyc-dash."""

    def __init__(self, feeds: Iterable[CachedFeed[Any]] = ()) -> None:
        self._feeds: dict[FeedName, CachedFeed[Any]] = {}
        for f in feeds:
            self.add(f)

    def add(self, feed: CachedFeed[Any]) -> None:
        if feed.name in self._feeds:
            raise ValueError(f"duplicate feed registered: {feed.name.value}")
        self._feeds[feed.name] = feed

    def __getitem__(self, name: FeedName) -> CachedFeed[Any]:
        return self._feeds[name]

    def __contains__(self, name: object) -> bool:
        return name in self._feeds

    def names(self) -> list[FeedName]:
        return list(self._feeds)

    def health(self) -> list[FeedHealth]:
        return [f.health() for f in self._feeds.values()]

    async def refresh_all(self) -> dict[FeedName, Envelope[Any]]:
        results = await asyncio.gather(*(f.get(force=True) for f in self._feeds.values()))
        return dict(zip(self._feeds, results, strict=True))
