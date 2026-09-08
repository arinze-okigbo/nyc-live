"""Browser half of the Phase 4 gate: first paint under 2 s on a warm cache.

RUN THIS ON A MACHINE WITH NETWORK ACCESS. It needs two things this sandbox does not
have:

1. the `playwright` Python package (report it to the orchestrator as a dev dependency;
   `pyproject.toml` is not ours to edit). Chromium is already installed at
   `/opt/pw-browsers` — do NOT run `playwright install`.
2. `unpkg.com` (maplibre-gl, deck.gl) and `tiles.openfreemap.org` (basemap tiles), both
   of which the sandbox egress proxy answers with 403. Without them the page cannot
   paint a map at all, so the number would be meaningless and is never guessed.

Both conditions are checked at runtime and the tests skip with a reason naming what is
missing. `test_page_degrades_when_the_map_libraries_are_blocked` needs only playwright:
it blocks the CDN on purpose and asserts our own code still renders honest status pills.

    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers uv run pytest tests/dash/test_first_paint.py -s

The measured number is printed as `first-contentful-paint (warm cache): NNN ms`.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

pytestmark = pytest.mark.slow

FIRST_PAINT_BUDGET_MS = 2000.0
CDN_PROBES = (
    "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js",
    "https://unpkg.com/deck.gl@9.0.0/dist.min.js",
    "https://tiles.openfreemap.org/styles/liberty",
)
BLOCKED_PATTERNS = ("**unpkg.com/**", "**tiles.openfreemap.org/**")


def playwright_api() -> object:
    return pytest.importorskip(
        "playwright.sync_api",
        reason=(
            "playwright is not installed; it is a dev dependency the orchestrator must add "
            "to pyproject.toml. Chromium is already at /opt/pw-browsers."
        ),
    )


def require_cdn() -> None:
    for url in CDN_PROBES:
        host = httpx.URL(url).host
        try:
            response = httpx.head(url, timeout=8.0, follow_redirects=True)
        except Exception as exc:
            pytest.skip(f"{host} is unreachable from this machine ({exc}); cannot measure paint")
        if response.status_code >= 400:
            pytest.skip(
                f"{host} returned HTTP {response.status_code} for {url}; the map libraries and "
                "basemap tiles are blocked here, so first paint cannot be measured"
            )


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    """Run the app under uvicorn on an ephemeral port for the duration of the test."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("uvicorn did not start")
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def first_contentful_paint_ms(page: object) -> float | None:
    return page.evaluate(  # type: ignore[attr-defined]
        "() => {const e = performance.getEntriesByName('first-contentful-paint')[0];"
        " return e ? e.startTime : null;}"
    )


def test_first_paint_under_two_seconds_on_a_warm_cache(app: FastAPI) -> None:
    api = playwright_api()
    require_cdn()
    with serve(app) as base, api.sync_playwright() as pw:  # type: ignore[attr-defined]
        browser = pw.chromium.launch()
        try:
            context = browser.new_context()  # one context => the HTTP cache is reused
            page = context.new_page()
            page.goto(base, wait_until="load")  # cold load: fills the cache
            page.goto(base, wait_until="load")  # warm load: the measured one
            page.wait_for_function(
                "() => performance.getEntriesByName('first-contentful-paint').length > 0",
                timeout=15_000,
            )
            fcp = first_contentful_paint_ms(page)
            print(f"first-contentful-paint (warm cache): {fcp:.0f} ms")
            assert fcp is not None, "the browser reported no first-contentful-paint entry"
            assert fcp < FIRST_PAINT_BUDGET_MS, f"first paint {fcp:.0f} ms exceeds the 2 s gate"
            # the map and the live data must actually arrive, not just the shell
            page.wait_for_selector("#map canvas", timeout=15_000)
            page.wait_for_selector('#pill-citibike[data-status="fresh"]', timeout=15_000)
        finally:
            browser.close()


def test_updates_arrive_over_sse_without_a_page_refresh(app: FastAPI) -> None:
    api = playwright_api()
    require_cdn()
    with serve(app) as base, api.sync_playwright() as pw:  # type: ignore[attr-defined]
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(base, wait_until="load")
            page.wait_for_selector('#connection-badge[data-mode="live"]', timeout=15_000)
            before = page.text_content("#updated")
            page.wait_for_function(
                "(previous) => document.getElementById('updated').textContent !== previous",
                arg=before,
                timeout=30_000,
            )
        finally:
            browser.close()


def test_page_degrades_when_the_map_libraries_are_blocked(app: FastAPI) -> None:
    """No CDN needed: with unpkg/tiles blocked the pills and the banner must still render."""
    api = playwright_api()
    with serve(app) as base, api.sync_playwright() as pw:  # type: ignore[attr-defined]
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            for pattern in BLOCKED_PATTERNS:
                page.route(pattern, lambda route: route.abort())
            page.goto(base, wait_until="load")
            page.wait_for_selector("#banner:not([hidden])", timeout=15_000)
            assert "unpkg.com" in (page.text_content("#banner") or "")
            page.wait_for_selector('#pill-citibike[data-status="fresh"]', timeout=15_000)
            # density has no samples in the test store: the pill must say error, honestly
            page.wait_for_selector('#pill-density[data-status="error"]', timeout=15_000)
            assert "nyc-vision" in (page.text_content("#detail-density") or "")
            # updates still arrive over SSE with no map on the page
            page.wait_for_selector('#connection-badge[data-mode="live"]', timeout=15_000)
        finally:
            browser.close()


def test_polling_fallback_when_the_sse_stream_is_unavailable(app: FastAPI) -> None:
    """No CDN needed: with /api/stream blocked the page must fall back to polling."""
    api = playwright_api()
    with serve(app) as base, api.sync_playwright() as pw:  # type: ignore[attr-defined]
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            for pattern in (*BLOCKED_PATTERNS, "**/api/stream**"):
                page.route(pattern, lambda route: route.abort())
            page.goto(base, wait_until="load")
            page.wait_for_selector('#connection-badge[data-mode="polling"]', timeout=15_000)
            page.wait_for_selector('#pill-citibike[data-status="fresh"]', timeout=15_000)
        finally:
            browser.close()
