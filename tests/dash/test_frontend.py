"""Static frontend checks that need no browser: pinned CDN versions, no keys, honest layers."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from nyc_dash import STATIC_DIR
from nyc_dash.api import ROUTE_BY_KEY

INDEX = (STATIC_DIR / "index.html").read_text()

# Dependency order matters for the JS files (map-layers.js's FEEDS array references
# layer builders at construction time) -- this list mirrors the <script> tags in
# index.html exactly. Tests below check content by concatenating them, since most
# assertions just need "this string exists somewhere in the app", not which file.
JS_FILES = [
    "utils.js",
    "icons.js",
    "state.js",
    "map-layers.js",
    "alerts-banner.js",
    "status-panel.js",
    "detail-panel.js",
    "data-sync.js",
    "app.js",
]
CSS_FILES = ["tokens.css", "chrome.css", "layers-panel.css", "detail-panel.css", "alerts-banner.css"]

APP_JS = "\n".join((STATIC_DIR / "js" / name).read_text() for name in JS_FILES)
STYLE = "\n".join((STATIC_DIR / "css" / name).read_text() for name in CSS_FILES)

MAPLIBRE_JS = re.compile(r"https://unpkg\.com/maplibre-gl@(\d+\.\d+\.\d+)/dist/maplibre-gl\.js")
MAPLIBRE_CSS = re.compile(r"https://unpkg\.com/maplibre-gl@(\d+\.\d+\.\d+)/dist/maplibre-gl\.css")
DECKGL_JS = re.compile(r"https://unpkg\.com/deck\.gl@(\d+\.\d+\.\d+)/dist\.min\.js")


def test_cdn_versions_are_pinned_exactly() -> None:
    js = MAPLIBRE_JS.search(INDEX)
    css = MAPLIBRE_CSS.search(INDEX)
    deckgl = DECKGL_JS.search(INDEX)
    assert js and css and deckgl, "index.html must load maplibre-gl and deck.gl from pinned URLs"
    assert js.group(1) == css.group(1)
    assert "@latest" not in INDEX
    assert "@next" not in INDEX


def test_basemap_style_is_keyless() -> None:
    assert "https://tiles.openfreemap.org/styles/liberty" in APP_JS
    for secret in ("access_token", "apikey", "api_key", "?key=", "&key="):
        assert secret not in INDEX.lower(), secret
        assert secret not in APP_JS.lower(), secret


def test_single_page_assets_only() -> None:
    """No build step: index.html loads exactly these plain scripts/stylesheets, in
    dependency order, and nothing else is served from static/ that isn't one of them."""
    for name in JS_FILES:
        assert f'<script defer src="/js/{name}"></script>' in INDEX
    for name in CSS_FILES:
        assert f'<link rel="stylesheet" href="/css/{name}" />' in INDEX
    # dependency order: app.js (boot) must be the last local script; map-layers.js
    # (which constructs FEEDS from layer-builder functions) must follow utils/state.
    local_scripts = re.findall(r'<script defer src="(/js/[a-z-]+\.js)"></script>', INDEX)
    assert local_scripts == [f"/js/{name}" for name in JS_FILES]

    served_js = sorted(p.name for p in (STATIC_DIR / "js").iterdir())
    served_css = sorted(p.name for p in (STATIC_DIR / "css").iterdir())
    assert served_js == sorted(JS_FILES)
    assert served_css == sorted(CSS_FILES)
    top_level = sorted(p.name for p in STATIC_DIR.iterdir() if not p.name.startswith("."))
    assert top_level == ["css", "index.html", "js"]


def test_every_layer_in_app_js_maps_to_a_real_endpoint() -> None:
    keys = re.findall(r'^\s{4}key: "([a-z0-9_]+)",$', APP_JS, flags=re.MULTILINE)
    assert keys, "could not find the FEEDS table in map-layers.js"
    assert set(keys) <= set(ROUTE_BY_KEY)
    # the brief's layers, plus the weather badge
    assert {"subway_arrivals", "density", "nyc_311", "citibike"} <= set(keys)
    assert 'const WEATHER_KEY = "weather";' in APP_JS


def test_deck_layers_cover_the_brief() -> None:
    assert "deck.ScatterplotLayer" in APP_JS  # subway, 311, Citi Bike, cameras
    assert "deck.HeatmapLayer" in APP_JS  # camera density
    assert "deck.MapboxOverlay" in APP_JS
    assert "new maplibregl.Map" in APP_JS


def test_error_status_hides_the_layer_and_stale_shows_last_good() -> None:
    assert 'if (entry.envelope.status === "error") continue;' in APP_JS
    assert "last good ${hhmmss(envelope.fetched_at)}" in APP_JS
    assert "envelope.error.message" in APP_JS


def test_frontend_pushes_with_sse_and_falls_back_to_polling() -> None:
    assert "new EventSource(url)" in APP_JS
    assert "startPolling" in APP_JS
    assert "setInterval(refreshAll, REFRESH_S * 1000)" in APP_JS
    assert '"EventSource" in window' in APP_JS


def test_missing_map_libraries_do_not_stop_the_status_pills() -> None:
    assert 'typeof maplibregl === "undefined" || typeof deck === "undefined"' in APP_JS
    assert "could not be loaded from unpkg.com" in APP_JS


def test_status_pill_styling_exists_for_each_status() -> None:
    for status in ("fresh", "stale", "error"):
        assert f'.pill[data-status="{status}"]' in STYLE


def test_assets_are_served_with_the_right_content_types(client: TestClient) -> None:
    assert client.get("/index.html").headers["content-type"].startswith("text/html")
    assert (
        client.get("/js/app.js")
        .headers["content-type"]
        .startswith(("text/javascript", "application/javascript"))
    )
    assert client.get("/css/tokens.css").headers["content-type"].startswith("text/css")
