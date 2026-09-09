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
    "search.js",
    "data-sync.js",
    "app.js",
]
CSS_FILES = [
    "tokens.css",
    "chrome.css",
    "layers-panel.css",
    "search.css",
    "detail-panel.css",
    "alerts-banner.css",
]

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


def test_bus_layer_is_registered_as_a_feed() -> None:
    """mta_bus is a real, key-gated, live endpoint (ROUTE_BY_KEY) -- it must appear in
    FEEDS like every other map layer, not be fetched through a bespoke path."""
    keys = re.findall(r'^\s{4}key: "([a-z0-9_]+)",$', APP_JS, flags=re.MULTILINE)
    assert "mta_bus" in keys
    assert "mta_bus" in ROUTE_BY_KEY
    assert "busLayer" in APP_JS
    assert "busDetail" in APP_JS
    assert "bus: busDetail," in APP_JS


def test_bus_icon_exists() -> None:
    assert "bus:" in (STATIC_DIR / "js" / "icons.js").read_text()
    assert "mta_bus:" in (STATIC_DIR / "js" / "status-panel.js").read_text()


def test_subway_shapes_are_fetched_and_drawn_as_a_backdrop() -> None:
    """mta_subway_shapes is fetched once through the generic fetchFeed/applyEnvelope
    machinery (not a bespoke fetch call) and rendered with a PathLayer, but is not one
    of the toggleable FEEDS entries -- it's a static 24h-TTL background layer."""
    assert "deck.PathLayer" in APP_JS
    assert "fetchFeed(SUBWAY_SHAPES_KEY)" in APP_JS
    keys = re.findall(r'^\s{4}key: "([a-z0-9_]+)",$', APP_JS, flags=re.MULTILINE)
    assert "mta_subway_shapes" not in keys
    assert "mta_subway_shapes" in ROUTE_BY_KEY


def test_deck_layers_cover_the_brief() -> None:
    assert "deck.ScatterplotLayer" in APP_JS  # subway, 311, Citi Bike, cameras, buses
    assert "deck.HeatmapLayer" in APP_JS  # camera density
    assert "deck.PathLayer" in APP_JS  # subway route shapes backdrop
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


def test_alert_route_chips_drive_a_shared_highlight_state_on_the_map() -> None:
    """Clicking a route chip in the alerts banner (alerts-banner.js) must set the same
    `highlightedRoute` global that subwayShapesLayer() (map-layers.js) reads when
    building the PathLayer, and the layer must re-render on that change alone (an
    updateTriggers entry keyed on it), not just on the next data refresh."""
    assert "let highlightedRoute = null;" in APP_JS
    assert "highlightedRoute = highlightedRoute === route ? null : route;" in APP_JS
    assert "getColor: highlightedRoute" in APP_JS
    assert "getWidth: highlightedRoute" in APP_JS
    # banner -> map only: selectRoute() must trigger a map re-render itself.
    assert 'if (typeof renderLayers === "function") renderLayers();' in APP_JS


def test_legend_covers_bus_and_subway_shapes_layers() -> None:
    """The static Legend block must have an entry for every layer actually drawn on the
    map, including mta_bus (routes) and the always-on subway-shapes backdrop -- both
    added after the legend was last written."""
    legend_match = re.search(r'<ul class="legend">(.*?)</ul>', INDEX, flags=re.DOTALL)
    assert legend_match, "could not find the Legend <ul> in index.html"
    legend_html = legend_match.group(1)
    assert "bus" in legend_html
    assert "subway route" in legend_html


def test_borough_filter_control_exists_in_the_sidebar() -> None:
    """A segmented row of borough chips (5 boroughs + All) must be present in #panel's
    markup, and every borough named in state.js's BOROUGHS must have a corresponding
    chip in index.html."""
    filter_match = re.search(
        r'<div id="borough-filter" class="borough-filter"[^>]*>(.*?)</div>', INDEX, flags=re.DOTALL
    )
    assert filter_match, "could not find #borough-filter in index.html"
    chips_html = filter_match.group(1)
    assert 'data-borough="all"' in chips_html
    boroughs_match = re.search(r"const BOROUGHS = \[(.*?)\];", APP_JS)
    assert boroughs_match, "could not find the BOROUGHS list in state.js"
    boroughs = re.findall(r'"([^"]+)"', boroughs_match.group(1))
    assert boroughs == ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]
    for borough in boroughs:
        assert f'data-borough="{borough}"' in chips_html


def test_borough_filter_narrows_exactly_the_three_layers_with_a_borough_field() -> None:
    """cameraLayer (Camera.area), layer311 (ServiceRequest.borough), and
    inspectionsLayer (RestaurantInspection.boro) must filter their data through the
    shared selectedBorough global; every other layer builder must not reference it."""
    assert "let selectedBorough = " in APP_JS
    assert 'boroughFiltered(located(envelope.records), "area")' in APP_JS
    assert 'boroughFiltered(located(envelope.records), "borough")' in APP_JS
    assert 'boroughFiltered(located(envelope.records), "boro")' in APP_JS
    # comparisons must be case-insensitive: the three feeds don't agree on borough case
    assert "value.toUpperCase() === selectedBorough.toUpperCase()" in APP_JS


def test_borough_filter_click_updates_state_and_rerenders_without_new_fetches() -> None:
    """Clicking a chip must update selectedBorough, refresh the affected sidebar pills
    from already-fetched envelopes (no new fetch), and call renderLayers() -- the same
    shared-global-plus-explicit-rerender pattern highlightedRoute/selectRoute already
    established for the route-highlight feature."""
    assert "function setSelectedBorough(borough)" in APP_JS
    assert "selectedBorough = borough;" in APP_JS
    assert 'typeof setPill === "function") setPill(key, entry.envelope);' in APP_JS
    assert "renderLayers();" in APP_JS
    assert "function initBoroughFilter()" in APP_JS


def test_borough_filtered_sidebar_counts_reflect_the_active_filter() -> None:
    """The three affected FEEDS entries' count() must route through boroughCountLabel
    so the sidebar number matches what's actually drawn once a borough is selected,
    instead of staying a stale citywide total."""
    assert 'boroughCountLabel(env.records, "area", "cameras")' in APP_JS
    assert 'boroughCountLabel(env.records, "borough", "requests")' in APP_JS
    assert 'boroughCountLabel(env.records, "boro", "inspections")' in APP_JS


def test_weather_badge_surfaces_the_forecast_via_a_click_popover() -> None:
    """`WeatherReport.forecast` (contracts.py) is fetched by the backend but must not be
    discarded: the badge exposes it through a click-toggle popover built from
    status-panel.js, not a fabricated placeholder and not routed through
    detail-panel.js (out of scope / owned elsewhere)."""
    assert "report.forecast" in APP_JS
    assert "function renderWeatherForecast(periods)" in APP_JS
    assert "function ensureWeatherPopover()" in APP_JS
    assert "weather-forecast-popover" in APP_JS
    assert "weather-forecast-popover" in STYLE
    # no affordance at all once a real forecast is absent -- never opens on nothing.
    assert "if (!weatherForecastPeriods.length) return;" in APP_JS
    assert 'badge.classList.toggle("has-forecast", weatherForecastPeriods.length > 0);' in APP_JS
    assert ".badge.has-forecast" in STYLE


def test_search_box_markup_exists_above_the_borough_section() -> None:
    """The search input + results list must live in #panel, above the Borough filter
    (search is the first thing a user reaching for a specific place should see)."""
    panel_match = re.search(r'<aside id="panel">(.*?)</aside>', INDEX, flags=re.DOTALL)
    assert panel_match, "could not find #panel in index.html"
    panel_html = panel_match.group(1)
    assert "<input" in panel_html and 'id="search-input"' in panel_html
    assert 'id="search-results"' in panel_html
    search_pos = panel_html.find('id="search-input"')
    borough_pos = panel_html.find('id="borough-filter"')
    assert search_pos != -1 and borough_pos != -1
    assert search_pos < borough_pos, "search box must come before the Borough section"


def test_search_js_searches_subway_and_citibike_over_already_fetched_state() -> None:
    """Search must read the same `state` map every layer builder reads (no re-fetch, no
    new endpoint) and must cover both subway_arrivals and citibike, per the brief."""
    search_js = (STATIC_DIR / "js" / "search.js").read_text()
    assert 'searchRecordsFor("subway_arrivals")' in search_js
    assert 'searchRecordsFor("citibike")' in search_js
    assert "state.get(key)" in search_js
    # An errored feed must never contribute fabricated/stale-looking search results.
    assert 'entry.envelope.status === "error"' in search_js


def test_search_result_selection_flies_to_the_record_and_opens_its_detail_panel() -> None:
    """Selecting a result must pan the map with flyTo (the same pattern
    createRecenterControl() in map-layers.js already uses) and open the detail panel
    through the shared DETAIL_BUILDERS table (detail-panel.js), not a bespoke UI."""
    search_js = (STATIC_DIR / "js" / "search.js").read_text()
    assert "map.flyTo({ center: [result.lon, result.lat], zoom: SEARCH_FLYTO_ZOOM });" in search_js
    assert "DETAIL_BUILDERS[result.kind]" in search_js


def test_search_debounces_input_instead_of_filtering_on_every_keystroke() -> None:
    search_js = (STATIC_DIR / "js" / "search.js").read_text()
    assert "SEARCH_DEBOUNCE_MS" in search_js
    assert "setTimeout(" in search_js and "clearTimeout(searchDebounceTimer)" in search_js


def test_search_js_loads_after_its_dependencies_and_before_data_sync() -> None:
    """search.js reads `state`/`map` (state.js), DETAIL_BUILDERS (detail-panel.js), and
    el/escapeHtml (utils.js) at call time, so it must load after all three; it has no
    hard ordering requirement against data-sync.js/app.js beyond that."""
    local_scripts = re.findall(r'<script defer src="(/js/[a-z-]+\.js)"></script>', INDEX)
    assert local_scripts.index("/js/search.js") > local_scripts.index("/js/detail-panel.js")
    assert local_scripts.index("/js/search.js") > local_scripts.index("/js/state.js")
    assert local_scripts.index("/js/search.js") > local_scripts.index("/js/utils.js")


def test_search_covers_dot_cameras_over_already_fetched_state() -> None:
    """Search must also cover dot_cameras, the same way it already covers
    subway_arrivals/citibike -- no re-fetch, no new endpoint, records read straight out
    of the shared `state` map."""
    search_js = (STATIC_DIR / "js" / "search.js").read_text()
    assert 'searchRecordsFor("dot_cameras")' in search_js
    assert 'kind: "cameras"' in search_js  # matches DETAIL_BUILDERS' real key, not the feed key


def test_camera_search_result_selection_flies_to_it_and_opens_its_detail_panel() -> None:
    """A selected camera result must go through the exact same flyTo + DETAIL_BUILDERS
    handoff selectSearchResult already uses for subway/citibike, not a bespoke path."""
    search_js = (STATIC_DIR / "js" / "search.js").read_text()
    assert "function searchCameraResults(query)" in search_js
    assert "DETAIL_BUILDERS[result.kind]" in search_js
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert "cameras: cameraDetail," in detail_js


def test_bus_route_search_is_a_distinct_kind_that_highlights_instead_of_flying_to_a_point() -> None:
    """A bus route has no single record to fly to (many live vehicles), so it must be a
    distinct result kind whose selection sets highlightedBusRoute rather than calling
    map.flyTo, and must reuse busRouteLabel (map-layers.js) rather than reimplementing
    agency-prefix parsing."""
    search_js = (STATIC_DIR / "js" / "search.js").read_text()
    assert 'searchRecordsFor("mta_bus")' in search_js
    assert "busRouteLabel(record.route_id)" in search_js
    assert 'kind: "bus_route"' in search_js
    assert "function selectBusRouteResult(result)" in search_js
    assert (
        "highlightedBusRoute = highlightedBusRoute === result.route ? null : result.route;"
        in search_js
    )
    # the bus_route branch must return before reaching the flyTo/DETAIL_BUILDERS path
    # every other kind shares.
    bus_branch = search_js[search_js.index("function selectSearchResult(result) {") :]
    assert 'if (result.kind === "bus_route")' in bus_branch
    assert bus_branch.index('if (result.kind === "bus_route")') < bus_branch.index("map.flyTo(")


def test_bus_route_highlight_drives_bus_layer_via_a_shared_state_global() -> None:
    """highlightedBusRoute (state.js) must be its own variable, not a reuse of
    highlightedRoute (which means something different: a highlighted subway line), and
    busLayer() (map-layers.js) must read it to dim/emphasize markers and re-render on
    that change alone via updateTriggers, the same idiom subwayShapesLayer's route
    highlight already established for highlightedRoute."""
    assert "let highlightedBusRoute = null;" in APP_JS
    assert "function busAlpha(routeId)" in APP_JS
    assert "function busRadius(routeId)" in APP_JS
    assert "getRadius: (d) => busRadius(d.route_id)," in APP_JS
    assert "getFillColor: (d) => [" in APP_JS
    assert "busAlpha(d.route_id)" in APP_JS
    assert "getRadius: highlightedBusRoute," in APP_JS


def test_selecting_a_bus_route_auto_enables_the_bus_layer() -> None:
    """mta_bus defaults to hidden (dense data, opt-in like dot_cameras); searching for a
    route implies wanting to see it, so selection must flip that layer's visibility and
    its sidebar checkbox on rather than leaving the user to discover a second manual
    step, mirroring buildPanel()'s own checkbox-wiring pattern (status-panel.js)."""
    search_js = (STATIC_DIR / "js" / "search.js").read_text()
    assert "function enableBusLayer()" in search_js
    assert 'state.get("mta_bus")' in search_js
    assert 'el("toggle-mta_bus")' in search_js
    assert "entry.visible = true;" in search_js


def test_search_kind_badge_css_exists_for_cameras_and_bus_routes() -> None:
    """The 4th/5th result-kind badges (cameras, bus_route) must have their own color
    rule, following the exact per-kind selector pattern subway/citibike already use."""
    assert '.search-result-kind[data-kind="cameras"]' in STYLE
    assert '.search-result-kind[data-kind="bus_route"]' in STYLE


def test_assets_are_served_with_the_right_content_types(client: TestClient) -> None:
    assert client.get("/index.html").headers["content-type"].startswith("text/html")
    assert (
        client.get("/js/app.js")
        .headers["content-type"]
        .startswith(("text/javascript", "application/javascript"))
    )
    assert client.get("/css/tokens.css").headers["content-type"].startswith("text/css")
