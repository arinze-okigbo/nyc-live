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


def test_alerts_count_reflects_the_envelope_total_not_the_page_size() -> None:
    """The "N active" badge must be driven by envelope.total_before_filter -- the real
    upstream count -- never by records.length, which is only ever the fetched page
    size (bounded by ALERTS_ENDPOINT's limit) and silently undercounts once the feed
    is truncated. A truncated response must also render an honest "+N more" notice
    rather than just presenting the partial list as if it were everything."""
    alerts_js = (STATIC_DIR / "js" / "alerts-banner.js").read_text()
    assert "function totalActiveCount(envelope)" in alerts_js
    assert "envelope.total_before_filter" in alerts_js
    assert "records.length} active" not in alerts_js
    assert "function truncationNoticeHtml(envelope, shownCount, totalCount)" in alerts_js
    assert "envelope.truncated" in alerts_js
    assert "more active alert" in alerts_js
    # limit=50 was the bug (silently hid alerts past the 50th); it must be gone.
    assert "limit=50" not in alerts_js


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
    # no affordance at all once there is neither a real forecast nor an active alert --
    # never opens on nothing. weatherPopoverHasContent() covers both (see the alerts
    # test below), superseding the forecast-only gate this popover started with.
    assert "function weatherPopoverHasContent()" in APP_JS
    assert "return weatherForecastPeriods.length > 0 || weatherAlertEntries.length > 0;" in APP_JS
    assert "if (!weatherPopoverHasContent()) return;" in APP_JS
    assert 'badge.classList.toggle("has-forecast", hasContent);' in APP_JS
    assert ".badge.has-forecast" in STYLE


def test_weather_alerts_surface_urgency_on_the_badge_and_in_the_popover() -> None:
    """`WeatherReport.alerts` (contracts.py, real NWS alerts.weather.gov data) is safety
    information, unlike the forecast, so it must (1) change the badge's own appearance
    via a `data-alert-severity` attribute keyed to this app's existing --error/--stale
    status color tokens rather than a fabricated new color, with the worst severities
    (Extreme/Severe) pulsing since those are the ones worth interrupting a glance for,
    (2) aggregate across every station in the envelope, not just the one driving the
    badge's temperature text (a JFK-only alert must not be invisible behind Central
    Park's clear skies), and (3) list in the popover with event/severity/headline/area,
    in their own section distinct from the routine forecast list."""
    # severity -> badge attribute, ranked worst-first, aggregated across all records.
    assert "const ALERT_SEVERITY_RANK = " in APP_JS
    assert "function collectWeatherAlerts(records)" in APP_JS
    assert "function applyWeatherAlertSeverity(records)" in APP_JS
    assert "badge.dataset.alertSeverity = entries[0].alert.severity.toLowerCase();" in APP_JS
    assert "applyWeatherAlertSeverity(envelope.records);" in APP_JS
    # an empty alerts list (the normal case) must not add a placeholder anywhere.
    assert "delete badge.dataset.alertSeverity;" in APP_JS
    # alerts are collected from the whole envelope, not report (== records[0]) alone.
    assert "renderWeatherAlerts(envelope.records);" in APP_JS
    # popover content: its own section, above the forecast, with event/severity/
    # headline/area, distinct markup from the plain forecast period list.
    assert "function weatherAlertHtml(entry)" in APP_JS
    assert 'class="weather-alert-list"' in APP_JS
    assert "alert.area_desc" in APP_JS
    assert "const alertsHtml = weatherAlertEntries.length" in APP_JS
    assert "weatherPopoverEl.innerHTML = alertsHtml + forecastHtml;" in APP_JS
    # CSS: severity keys off this app's existing --error/--stale tokens (tokens.css),
    # not a new color, and only the worst severities pulse.
    assert '.badge[data-alert-severity="extreme"]' in STYLE
    assert '.badge[data-alert-severity="severe"]' in STYLE
    assert "color: var(--error);" in STYLE
    assert "animation: weather-alert-pulse" in STYLE
    assert '.badge[data-alert-severity="moderate"]' in STYLE
    assert '.badge[data-alert-severity="minor"]' in STYLE
    assert "color: var(--stale);" in STYLE
    assert ".weather-alert-list" in STYLE


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
    assert "function searchCameraResults(query, origin)" in search_js
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


def test_search_ranks_point_based_results_by_distance_from_the_map_center() -> None:
    """Subway/Citi Bike/camera results must be ranked nearest-to-the-current-map-center
    first (not the previous arbitrary/alphabetical order), computed fresh from
    map.getCenter() each time a search runs; bus routes have no single point and must
    keep their own ordering untouched."""
    search_js = (STATIC_DIR / "js" / "search.js").read_text()
    assert "function haversineDistanceMeters(lat1, lon1, lat2, lon2)" in search_js
    assert "function currentSearchOrigin()" in search_js
    assert "map.getCenter()" in search_js
    assert "function sortByDistance(records, origin)" in search_js
    assert ".sort((a, b) => a.distanceM - b.distanceM)" in search_js
    # the three point-based kinds route through the shared distance sort...
    assert "sortByDistance(Array.from(bestByStop.values()), origin)" in search_js
    assert search_js.count("sortByDistance(matches, origin)") == 2  # citibike + cameras
    # ...bus routes explicitly do not (no lat/lon on a route, alphabetical unchanged).
    bus_fn = search_js[search_js.index("function searchBusRouteResults(query) {") :]
    bus_fn = bus_fn[: bus_fn.index("\n}\n")]
    assert "sortByDistance" not in bus_fn
    assert ".sort((a, b) => a[0].localeCompare(b[0]))" in bus_fn


def test_search_kind_badge_css_exists_for_cameras_and_bus_routes() -> None:
    """The 4th/5th result-kind badges (cameras, bus_route) must have their own color
    rule, following the exact per-kind selector pattern subway/citibike already use."""
    assert '.search-result-kind[data-kind="cameras"]' in STYLE
    assert '.search-result-kind[data-kind="bus_route"]' in STYLE


def test_detail_panel_has_a_focus_trap_and_restores_focus_on_close() -> None:
    """The click-detail panel behaves like a modal overlay (map-layers.js's
    handleMapClick and search.js's selectSearchResult both open it over the map/sidebar),
    so it needs: (1) ARIA modal semantics, (2) a Tab/Shift+Tab focus trap so keyboard
    users can't tab out into the content it's covering, and (3) focus restored to
    whatever triggered it when it closes, instead of dropping to document.body."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    # ARIA modal semantics, set on open.
    assert 'panel.setAttribute("role", "dialog");' in detail_js
    assert 'panel.setAttribute("aria-modal", "true");' in detail_js
    assert 'panel.setAttribute("aria-labelledby", "detail-panel-title");' in detail_js
    assert 'id="detail-panel-title"' in detail_js
    # Tab trap: a keydown listener that wraps focus at the panel's first/last
    # focusable element, attached directly to the panel so it only ever fires while
    # focus is already inside it.
    assert "function trapPanelFocus(ev)" in detail_js
    assert 'if (ev.key !== "Tab") return;' in detail_js
    assert "function panelFocusableElements(panel)" in detail_js
    assert 'panel.addEventListener("keydown", trapPanelFocus);' in detail_js
    # Focus restoration: the trigger is captured on open and restored on a real close,
    # but not when one panel is immediately replaced by another (there is nothing to
    # restore to there -- focus is about to move into the new panel instead).
    assert "let panelTriggerElement = null;" in detail_js
    assert "panelTriggerElement = document.activeElement;" in detail_js
    assert "closeDetailPanel({ restoreFocus: false })" in detail_js
    assert (
        "trigger.focus({ preventScroll: true })" in detail_js
        or "target.focus({ preventScroll: true })" in detail_js
    )
    # A one-shot fallback restoration target: search.js's result <li>s don't survive
    # past selection (clearSearch() tears them down immediately), so search.js hands
    # off a fallback (the search input) through the same shared-global idiom
    # highlightedBusRoute already establishes for search -> map communication.
    assert "let panelFocusFallback = null;" in detail_js
    search_js = (STATIC_DIR / "js" / "search.js").read_text()
    assert "panelFocusFallback = el(" in search_js


def test_map_view_is_deep_linkable_via_the_url_hash() -> None:
    """Panning/zooming the map must update the URL hash (format `#zoom/lat/lon`, e.g.
    "#12.40/40.73570/-73.99110") via history.replaceState -- never pushState, which
    would spam the back-button history on every pan/zoom -- debounced past MapLibre's
    "moveend" (already once-per-gesture, not once-per-frame like "move"). On load, a
    present and valid hash must initialize the map there instead of the default NYC
    view; an absent or malformed hash must fall back to NYC rather than crash."""
    assert "function parseHashView()" in APP_JS
    assert "function writeHashView()" in APP_JS
    assert "function scheduleHashUpdate()" in APP_JS
    assert 'map.on("moveend", scheduleHashUpdate);' in APP_JS
    assert 'history.replaceState(null, "", hash);' in APP_JS
    assert "history.pushState(" not in APP_JS
    assert "clearTimeout(hashUpdateTimer);" in APP_JS
    assert "setTimeout(writeHashView, HASH_UPDATE_DEBOUNCE_MS);" in APP_JS
    # sane range validation of the parsed hash, not blind trust of an arbitrary URL
    assert "zoom >= 0 && zoom <= 22" in APP_JS
    assert "lat >= -90 && lat <= 90" in APP_JS
    assert "lon >= -180 && lon <= 180" in APP_JS
    # falls back to the default NYC view (not a crash) when the hash is absent/malformed
    assert (
        "center: initialView ? [initialView.lon, initialView.lat] : [NYC.longitude, NYC.latitude],"
        in APP_JS
    )
    assert "zoom: initialView ? initialView.zoom : NYC.zoom," in APP_JS


def test_map_has_a_discoverable_copy_link_control() -> None:
    """The deep-link hash (test_map_view_is_deep_linkable_via_the_url_hash) is otherwise
    only discoverable by noticing the URL bar changed -- a copy-link control must exist
    alongside the recenter control (same IControl onAdd/onRemove contract, same
    maplibregl-ctrl-group container), copy `location.href` via
    navigator.clipboard.writeText on click, show visible feedback on success, fail
    gracefully (no unhandled rejection) when the Clipboard API is unavailable or
    rejects, and be wired into the same top-left control stack in app.js."""
    assert "function createCopyLinkControl()" in APP_JS
    assert 'container.className = "maplibregl-ctrl maplibregl-ctrl-group";' in APP_JS
    assert "navigator.clipboard.writeText(location.href).then(" in APP_JS
    # graceful fallback: no crash / unhandled rejection when the API is missing or
    # the write itself is rejected -- both paths route through the same feedback fn.
    assert '!navigator.clipboard || typeof navigator.clipboard.writeText !== "function"' in APP_JS
    assert 'showFeedback(ERROR_GLYPH, "is-error")' in APP_JS
    assert 'showFeedback(SUCCESS_GLYPH, "is-copied")' in APP_JS
    # wired into the same top-left stack as the existing recenter control.
    assert 'map.addControl(createRecenterControl(), "top-left");' in APP_JS
    assert 'map.addControl(createCopyLinkControl(), "top-left");' in APP_JS
    # feedback states are styled, not silent.
    assert ".copy-link-btn.is-copied" in STYLE
    assert ".copy-link-btn.is-error" in STYLE


def test_assets_are_served_with_the_right_content_types(client: TestClient) -> None:
    assert client.get("/index.html").headers["content-type"].startswith("text/html")
    assert (
        client.get("/js/app.js")
        .headers["content-type"]
        .startswith(("text/javascript", "application/javascript"))
    )
    assert client.get("/css/tokens.css").headers["content-type"].startswith("text/css")


def test_open_detail_panels_refresh_from_the_feed_they_belong_to() -> None:
    """A previously-open detail panel must not go stale between polls: data-sync.js's
    applyEnvelope must call detail-panel.js's refreshOpenDetailPanel once per feed update,
    generically for every feed (not just subway), and that function must be a no-op
    unless the currently-open panel actually belongs to the feed that just refreshed."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    data_sync_js = (STATIC_DIR / "js" / "data-sync.js").read_text()
    assert "function refreshOpenDetailPanel(feedKey, envelope)" in detail_js
    assert "refreshOpenDetailPanel(key, envelope);" in data_sync_js
    assert "if (!openPanelFeedKey || openPanelFeedKey !== feedKey) return;" in detail_js


def test_detail_identity_fields_match_the_real_contract_field_names() -> None:
    """Re-finding the same real-world entity in a fresh envelope requires the exact
    identity field per record type (contracts.py): SubwayArrival.trip_id,
    ServiceRequest.unique_key, BikeStation.station_id, Camera.id,
    RestaurantInspection.camis, BusVehicle.vehicle_id -- keyed by data-sync.js's feed
    key (FEEDS[].key), the vocabulary refreshOpenDetailPanel is actually called with."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert "const DETAIL_IDENTITY = {" in detail_js
    identity_fields = {
        "subway_arrivals": "r.trip_id",
        "nyc_311": "r.unique_key",
        "citibike": "r.station_id",
        "dot_cameras": "r.id",
        "dohmh_inspections": "r.camis",
        "mta_bus": "r.vehicle_id",
    }
    for feed_key, field_expr in identity_fields.items():
        assert f"{feed_key}: (r) => {field_expr}," in detail_js


def test_every_detail_builder_passes_its_feed_key_and_record_as_identity() -> None:
    """Each *Detail function must hand openDetailPanel an `identity` (feedKey + record)
    matching DETAIL_IDENTITY's keys above, or refreshOpenDetailPanel has nothing to match
    against for that layer and the panel would stay frozen -- the exact bug being fixed."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    for feed_key in (
        "subway_arrivals",
        "nyc_311",
        "citibike",
        "dot_cameras",
        "dohmh_inspections",
        "mta_bus",
    ):
        assert f'{{ feedKey: "{feed_key}", record' in detail_js


def test_record_gone_state_is_explicit_not_a_silent_freeze() -> None:
    """If the tracked record drops out of a later envelope entirely (train completed its
    run, bus went out of service, etc.), the panel must show an explicit message instead
    of leaving the last-known render up forever -- and must stop re-checking once shown,
    rather than re-touching the DOM on every subsequent poll."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert "function showRecordGoneState(feedKey)" in detail_js
    assert "const RECORD_GONE_MESSAGES = {" in detail_js
    assert "if (!match) {" in detail_js
    assert "showRecordGoneState(feedKey);" in detail_js
    assert "let openPanelGone = false;" in detail_js
    assert "if (openPanelGone) return;" in detail_js
    assert "openPanelGone = true;" in detail_js


def test_camera_and_subway_panels_preserve_async_subsections_on_refresh() -> None:
    """cameraDetail's live image poll + one-shot density-history fetch, and subwayDetail's
    one-shot full-stop-list fetch, must NOT restart on every ~15s refresh -- only their
    summary fields (status/roadway/area; trip/direction/next-stop-eta) should rebuild.
    Enforced by each returning `{ cleanup, update }` (not a bare cleanup function) whose
    `update` touches only a dedicated summary container, leaving the rest of the body
    (and its running timers/fetches) untouched."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert "function cameraSummaryFieldsHtml(camera)" in detail_js
    assert '<div id="camera-summary-fields">' in detail_js
    assert 'body.querySelector("#camera-summary-fields")' in detail_js
    assert "function subwaySummaryFieldsHtml(record)" in detail_js
    assert '<div id="subway-summary-fields">' in detail_js
    assert 'body.querySelector("#subway-summary-fields")' in detail_js
    # both return the richer object shape, not a bare cleanup function, so
    # refreshOpenDetailPanel takes the lightweight `update` path instead of a full rebuild.
    assert detail_js.count("update: (freshCamera) => {") == 1
    assert detail_js.count("update: (freshRecord) => {") == 1


def test_dohmh_inspection_identity_ties_are_broken_by_most_recent_visit() -> None:
    """camis is not 1:1 with a dohmh_inspections record (one row per violation per visit),
    so refreshOpenDetailPanel's identity match must pick a deterministic candidate among
    same-camis rows rather than an arbitrary one -- newest inspection_date wins."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert "function findMatchingRecord(idFn, records, currentRecord)" in detail_js
    assert "return time > bestTime ? r : best;" in detail_js


def test_refresh_preserves_scroll_position_on_the_full_rebuild_path() -> None:
    """Builders with no async subsection to protect (inspectionDetail, service311Detail,
    bikeDetail, busDetail) refresh via a full body rebuild; that must not yank a
    mid-scroll reader (e.g. the inspection-history list) back to the top every poll."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert "const scrollTop = body.scrollTop;" in detail_js
    assert "body.scrollTop = scrollTop;" in detail_js
