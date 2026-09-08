"""Shared HTTP plumbing: one client factory, one rate limiter, one retry helper.

Every adapter uses these so User-Agent, timeouts, and politeness are uniform.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import timedelta

import httpx

from nyc_live.config import Settings, get_settings
from nyc_live.contracts import ErrorKind, FeedName, FeedUnavailable


def make_client(settings: Settings | None = None, **kwargs: object) -> httpx.AsyncClient:
    s = settings or get_settings()
    headers = {"User-Agent": s.user_agent, "Accept": "*/*"}
    extra_headers = kwargs.pop("headers", None)
    if isinstance(extra_headers, Mapping):
        headers.update({str(k): str(v) for k, v in extra_headers.items()})
    return httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(s.http_timeout_s),
        follow_redirects=True,
        **kwargs,  # type: ignore[arg-type]
    )


class RateLimiter:
    """Per-key minimum interval. `wait(key)` sleeps until the key may fire again.

    Used for the 2 s per-camera cadence and for per-feed floors. Async-safe.
    """

    def __init__(self, min_interval: timedelta) -> None:
        self.min_interval_s = min_interval.total_seconds()
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def wait(self, key: str) -> float:
        """Block until allowed; returns seconds actually slept."""
        async with self._lock(key):
            now = time.monotonic()
            last = self._last.get(key)
            slept = 0.0
            if last is not None:
                remaining = self.min_interval_s - (now - last)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    slept = remaining
            self._last[key] = time.monotonic()
            return slept

    def forget(self, key: str) -> None:
        self._last.pop(key, None)
        self._locks.pop(key, None)


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    feed: FeedName,
    retries: int | None = None,
    params: Mapping[str, str | int | float] | None = None,
    headers: Mapping[str, str] | None = None,
    backoff_s: float = 0.5,
) -> httpx.Response:
    """GET with bounded retries on timeouts / 5xx. 4xx is fatal and loud.

    Returns a 2xx response or raises FeedUnavailable with the right ErrorKind.
    """
    attempts = (get_settings().http_retries if retries is None else retries) + 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            last_exc = exc
        except httpx.HTTPError as exc:
            last_exc = exc
        else:
            if resp.status_code == 429:
                retry_after = _retry_after_s(resp)
                raise FeedUnavailable(
                    feed,
                    f"rate limited by upstream (429) for {url}",
                    kind=ErrorKind.RATE_LIMITED,
                    url=url,
                    upstream_status=429,
                    retry_after_s=retry_after,
                )
            if resp.status_code == 404:
                raise FeedUnavailable(
                    feed,
                    f"404 Not Found for {url}; the endpoint or slug is wrong",
                    kind=ErrorKind.NOT_FOUND,
                    url=url,
                    upstream_status=404,
                )
            if 400 <= resp.status_code < 500:
                raise FeedUnavailable(
                    feed,
                    f"HTTP {resp.status_code} from {url}: {resp.text[:200]}",
                    url=url,
                    upstream_status=resp.status_code,
                )
            if resp.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            else:
                return resp
        if attempt < attempts - 1:
            await asyncio.sleep(backoff_s * (2**attempt))
    kind = (
        ErrorKind.UPSTREAM_TIMEOUT
        if isinstance(last_exc, httpx.TimeoutException)
        else ErrorKind.UPSTREAM_HTTP
    )
    status = last_exc.response.status_code if isinstance(last_exc, httpx.HTTPStatusError) else None
    raise FeedUnavailable(
        feed,
        f"{type(last_exc).__name__} after {attempts} attempt(s) for {url}: {last_exc}",
        kind=kind,
        url=url,
        upstream_status=status,
    )


def _retry_after_s(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
