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


def test_search_results_are_keyboard_reachable_not_just_clickable() -> None:
    """The results <li>s are tabindex="-1" on purpose (they're not real Tab stops), so
    without dedicated key handling a keyboard-only user could see results but never
    select one. ArrowUp/ArrowDown must move a highlight (reflected as
    aria-activedescendant on the ARIA combobox input, per the standard "listbox
    autocomplete" pattern) and Enter must select the highlighted result -- or the first
    one, if none has been highlighted yet -- the same handoff a mouse click already
    uses (selectSearchResult), not a bespoke keyboard-only path."""
    search_js = (STATIC_DIR / "js" / "search.js").read_text()
    assert "function syncHighlight()" in search_js
    assert "function moveHighlight(delta)" in search_js
    assert "let highlightedResultIndex = -1;" in search_js
    assert 'input.setAttribute("aria-activedescendant", activeItem.id);' in search_js
    # arrow keys and Enter are wired on the same input keydown listener Escape uses.
    assert 'if (ev.key === "ArrowDown") {' in search_js
    assert 'if (ev.key === "ArrowUp") {' in search_js
    assert 'if (ev.key === "Enter") {' in search_js
    assert "moveHighlight(1);" in search_js
    assert "moveHighlight(-1);" in search_js
    assert "selectSearchResult(currentSearchResults[index]);" in search_js
    # index.html wires the ARIA combobox semantics aria-activedescendant depends on.
    assert 'role="combobox"' in INDEX
    assert 'aria-controls="search-results"' in INDEX
    style = (STATIC_DIR / "css" / "search.css").read_text()
    assert ".search-result.is-active" in style


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


def test_record_gone_state_is_announced_to_screen_readers() -> None:
    """The record-gone message is a significant, one-time state change happening to
    content inside a role="dialog" panel a screen-reader user can't re-glance at, so
    (unlike a routine ETA-tick refresh) it must be announced: emptyStateHtml() takes an
    opt-in `live` option -- defaulting to false so every other call site (a static empty
    state, or content already inside its own aria-live wrapper like
    #camera-density-history/#subway-stop-list) is unaffected -- and showRecordGoneState
    is the one caller that turns it on."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert "function emptyStateHtml(icon, message, { live = false } = {})" in detail_js
    assert 'const liveAttrs = live ? \' role="status" aria-live="polite"\' : "";' in detail_js
    assert '<div class="detail-empty-state"${liveAttrs}>' in detail_js
    assert "{ live: true }" in detail_js
    # only showRecordGoneState opts in -- no other emptyStateHtml call passes it.
    assert detail_js.count("{ live: true }") == 1
    gone_state_fn = detail_js[detail_js.index("function showRecordGoneState(feedKey) {") :]
    gone_state_fn = gone_state_fn[: gone_state_fn.index("\n}\n")]
    assert "{ live: true }" in gone_state_fn
    # the existing aria-live precedent this follows (loading states), left untouched.
    assert 'aria-busy="true" aria-live="polite"' in detail_js


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


def test_global_shortcuts_never_fire_while_the_user_is_typing() -> None:
    """The single most common way a keyboard layer breaks a page: a single-character
    shortcut firing mid-word in a text field. Every character shortcut must be gated on
    a typing check that covers <input>/<textarea>/<select> and contenteditable, tested
    against both the event target and the live activeElement, and any Ctrl/Cmd/Alt
    combination (which belongs to the browser) must be handed straight back. Shift must
    NOT be in that bail-out list -- `?` is Shift+/ on most layouts."""
    app_js = (STATIC_DIR / "js" / "app.js").read_text()
    assert "function isTypingTarget(node)" in app_js
    assert 'return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";' in app_js
    assert "if (node.isContentEditable) return true;" in app_js
    assert "return isTypingTarget(ev.target) || isTypingTarget(document.activeElement);" in app_js
    assert "function hasCommandModifier(ev)" in app_js
    assert "return ev.ctrlKey || ev.metaKey || ev.altKey;" in app_js
    assert "ev.shiftKey" not in app_js.split("function hasCommandModifier(ev)")[1].split("}")[0]
    # order matters: the modifier and typing gates run before any key is dispatched.
    dispatch = app_js[app_js.index("function handleGlobalKeydown(ev) {") :]
    dispatch = dispatch[: dispatch.index("\n}\n")]
    assert dispatch.index("if (hasCommandModifier(ev)) return;") < dispatch.index(
        "const action = SHORTCUT_ACTIONS[key];"
    )
    assert dispatch.index("if (isTypingContext(ev)) return;") < dispatch.index(
        "const layerIndex = digitLayerIndex(key);"
    )
    # a keystroke another handler already acted on (search.js's Arrow/Enter, the focus
    # traps) is never acted on twice, and IME composition is left alone.
    assert "if (ev.defaultPrevented) return;" in dispatch
    assert "if (ev.isComposing || ev.keyCode === 229) return;" in dispatch


def test_global_shortcut_map_covers_search_layers_recenter_and_borough() -> None:
    """The shortcut table itself: `/` focuses search, `?` toggles help, `r` recenters,
    `b` cycles the borough filter, and digits address the sidebar's layers. Layer
    toggling must go through the existing checkbox (buildPanel's change listener in
    status-panel.js stays the single write site for `visible`), and recentering must
    reuse state.js's NYC constant rather than another file's control DOM."""
    app_js = (STATIC_DIR / "js" / "app.js").read_text()
    assert "const SHORTCUT_ACTIONS = {" in app_js
    for entry in (
        '"/": focusSearchInput,',
        '"?": toggleShortcuts,',
        "r: recenterMap,",
        "b: cycleBoroughFilter,",
    ):
        assert entry in app_js, entry
    # layers: digits 1..9 map onto FEEDS order; the toggle drives the real checkbox.
    assert "function toggleLayerByIndex(index)" in app_js
    assert "const checkbox = el(`toggle-${feed.key}`);" in app_js
    assert "checkbox.click();" in app_js
    assert "function digitLayerIndex(key)" in app_js
    # recenter uses the same target as map-layers.js's own recenter control.
    assert "map.flyTo({ center: [NYC.longitude, NYC.latitude], zoom: NYC.zoom });" in app_js
    # borough cycle walks state.js's canonical list, "all" first, and wraps.
    assert 'const BOROUGH_CYCLE = ["all", ...BOROUGHS];' in app_js
    assert "BOROUGH_CYCLE[(current + 1) % BOROUGH_CYCLE.length]" in app_js
    assert "setSelectedBorough(next);" in app_js
    # actions whose only other feedback is visual are announced politely.
    assert 'id="shortcut-status"' in INDEX
    assert 'role="status"' in INDEX and 'aria-live="polite"' in INDEX
    assert "function announceShortcut(message)" in app_js


def test_shortcut_help_overlay_is_discoverable_dismissible_and_focus_trapped() -> None:
    """`?` is only useful if something points at it, and a modal that strands focus is
    worse than no modal: the overlay needs an always-visible affordance in the page
    chrome, dialog semantics, Escape AND click-outside dismissal, a Tab trap, and focus
    restored to whatever opened it -- the same contract detail-panel.js already meets."""
    app_js = (STATIC_DIR / "js" / "app.js").read_text()
    # discoverable: a real button in the topbar, wired to the same toggle the key uses.
    assert 'id="shortcuts-button"' in INDEX
    assert 'aria-haspopup="dialog"' in INDEX
    assert 'aria-controls="shortcuts-overlay"' in INDEX
    assert 'button.addEventListener("click", () => toggleShortcuts());' in app_js
    # dialog semantics on static markup, body filled from FEEDS at open time so the
    # per-layer rows can never drift from the layers that actually exist.
    assert 'id="shortcuts-overlay"' in INDEX
    assert 'role="dialog"' in INDEX
    assert 'aria-modal="true"' in INDEX
    assert 'aria-labelledby="shortcuts-title"' in INDEX
    assert "function renderShortcutsBody()" in app_js
    assert "FEEDS.slice(0, SHORTCUT_LAYER_DIGIT_MAX).map((feed, index)" in app_js
    # dismissal: Escape (topmost layer only), the close button, and a backdrop click.
    assert "function handleEscapeKey(ev)" in app_js
    assert "if (isShortcutsOpen()) {" in app_js
    assert "if (ev.target === overlay) closeShortcuts();" in app_js
    assert 'id="shortcuts-close"' in INDEX
    # focus: trapped while open, restored to the trigger on close, never forced onto a
    # detached node.
    assert "function trapShortcutsFocus(ev)" in app_js
    assert 'if (ev.key !== "Tab") return;' in app_js
    assert "let shortcutsTrigger = null;" in app_js
    assert "shortcutsTrigger = document.activeElement;" in app_js
    assert "trigger.focus({ preventScroll: true });" in app_js
    assert "trigger.isConnected" in app_js
    # the overlay documents the shortcuts that already existed elsewhere but were
    # invisible (detail-panel.js's Escape/Tab trap, search.js's arrows/Enter).
    assert "const SHORTCUT_ROWS = [" in app_js
    for key in ('keys: ["Esc"]', 'keys: ["↑", "↓"]', 'keys: ["Enter"]', 'keys: ["Tab"]'):
        assert key in app_js, key
    # [hidden] must beat the overlay's own display:flex or it would never hide.
    assert ".shortcuts-overlay[hidden] { display: none; }" in INDEX


def test_inspection_panel_shows_this_visits_violations_not_a_fabricated_history() -> None:
    """The dohmh_inspections feed now emits one record per restaurant, so the panel's old
    "Inspection history" section (which re-scanned state for other rows sharing the same
    camis) could only ever produce an empty list. It is replaced by RestaurantInspection's
    `violations` -- every violation cited on the ONE visit shown -- and the section must
    say exactly that, never re-claiming to be inspection history."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    # the dead code, and the helpers only it used, are gone rather than left unreachable.
    for dead in (
        "otherInspectionsForCamis",
        "function truncate(",
        "INSPECTION_HISTORY_LIMIT",
        "INSPECTION_VIOLATION_TRUNCATE_LENGTH",
        "inspectionHistoryHtml",
    ):
        assert dead not in detail_js, dead
    # ...including its only reason to read a whole feed out of state.js.
    assert 'state.get("dohmh_inspections")' not in detail_js
    # the replacement renders from the record's own violations list.
    assert "function inspectionViolationsHtml(record)" in detail_js
    assert "Array.isArray(record.violations) ? record.violations : []" in detail_js
    assert "inspectionViolationsHtml(r)" in detail_js
    # honest framing: this visit's violations, and an explicit note that earlier visits
    # are not in the feed -- neither the heading nor the note may promise history.
    assert "Violations cited on this inspection" in detail_js
    assert "Inspection history" not in detail_js
    assert "most recent inspection only, not its earlier visits" in detail_js
    # upstream order is preserved (adapter emits graded-first, Critical-first, code asc).
    assert ".map(violationRowHtml)" in detail_js


def test_inspection_clean_visit_reads_as_a_result_not_as_missing_data() -> None:
    """`violations: []` is a real outcome -- 25 of 500 live records are a visit where
    DOHMH cited nothing -- so it gets an affirmative empty state, not the generic
    "nothing to show" treatment. It must NOT opt into emptyStateHtml's `{ live: true }`
    ARIA path: that is reserved for the one-time record-gone transition, and this string
    is static content re-rendered unchanged on every poll."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert 'emptyStateHtml("\u2705", "No violations were cited on this inspection.")' in detail_js
    violations_fn = detail_js[detail_js.index("function inspectionViolationsHtml(record) {") :]
    violations_fn = violations_fn[: violations_fn.index("\n}\n")]
    assert "{ live: true }" not in violations_fn
    # showRecordGoneState remains the sole opt-in (guarded by the existing count == 1).
    assert detail_js.count("{ live: true }") == 1


def test_inspection_violation_rows_surface_criticality_and_escape_upstream_text() -> None:
    """A critical violation is not the same as a routine one, so the row and the heading
    both carry it. critical_flag is upstream free text ("Critical" / "Not Critical" /
    "Not Applicable" in the live feed), so only the exact critical value is special-cased
    and everything else is shown verbatim -- escaped, like every other upstream string."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert "function isCriticalViolation(violation)" in detail_js
    assert 'const CRITICAL_FLAG = "critical";' in detail_js
    assert (
        'String(violation.critical_flag || "").trim().toLowerCase() === CRITICAL_FLAG' in detail_js
    )
    # heading counts the critical subset instead of leaving the reader to tally it.
    assert "function violationCountLabel(violations)" in detail_js
    assert "violations.filter(isCriticalViolation).length" in detail_js
    assert "${total} ${noun}, ${critical} critical" in detail_js
    # every upstream-controlled string in a row goes through escapeHtml.
    row_fn = detail_js[detail_js.index("function violationRowHtml(violation) {") :]
    row_fn = row_fn[: row_fn.index("\n}\n")]
    assert "escapeHtml(violation.code)" in row_fn
    assert "escapeHtml(violation.critical_flag)" in row_fn
    assert "escapeHtml(" in row_fn and "violation.description" in row_fn
    assert "${violation.description}" not in row_fn and "${violation.code}" not in row_fn
    # DOHMH's `action` (the only place "Establishment Closed by DOHMH" surfaces) replaces
    # the old "Latest violation" field, which now just repeats violations[0] verbatim.
    assert '["Result", escapeHtml(r.action || "\u2014")]' in detail_js
    assert "Latest violation" not in detail_js


def test_inspection_violation_list_keeps_its_own_scroll_across_a_refresh() -> None:
    """The violation list -- not the panel body -- is the scroll container for this
    builder (.detail-stop-list caps at 220px; the feed's largest visit overflows it by
    ~1,280px), so refreshOpenDetailPanel's body-level scroll preservation cannot help
    here. The builder must carry the list's own scrollTop across its innerHTML
    replacement, or every poll yanks a mid-list reader back to the top."""
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert 'const VIOLATION_LIST_SELECTOR = ".inspection-violation-list";' in detail_js
    assert "function violationListScrollTop(body)" in detail_js
    builder = detail_js[detail_js.index("function inspectionDetail(record) {") :]
    builder = builder[: builder.index("\n}\n")]
    # read before the wipe, restore after it.
    assert builder.index("const listScrollTop = violationListScrollTop(body);") < builder.index(
        "body.innerHTML ="
    )
    assert "if (list) list.scrollTop = listScrollTop;" in builder
    # the CSS class the selector depends on is actually emitted by the list markup.
    assert "inspection-violation-list" in detail_js


# -- Component-quality invariants for the three dense components ---------------
#
# layers-panel.css (sidebar), detail-panel.css (click-detail card) and
# alerts-banner.css (service alerts) style what is visually one family of dense,
# 300px-wide information components. These lock in the rules that pass established,
# because each one was violated by drift that accumulated feature-by-feature.

CSS_DIR = STATIC_DIR / "css"
LAYERS_CSS = (CSS_DIR / "layers-panel.css").read_text()
DETAIL_CSS = (CSS_DIR / "detail-panel.css").read_text()
ALERTS_CSS = (CSS_DIR / "alerts-banner.css").read_text()
COMPONENT_CSS = "\n".join((LAYERS_CSS, DETAIL_CSS, ALERTS_CSS))
# These stylesheets carry long rationale comments that quote the values they replaced
# (a `min-height: 20px`, a hand-picked hex), so any "this value is gone" assertion has
# to look at declarations only.
DECLARATIONS_ONLY = re.sub(r"/\*.*?\*/", "", COMPONENT_CSS, flags=re.DOTALL)


def _media_block(css: str, query: str) -> str:
    """Every `@media <query> { ... }` block's body, brace-matched and concatenated --
    layers-panel.css has two separate `max-width: 480px` blocks."""
    bodies = []
    cursor = 0
    while True:
        start = css.find(query, cursor)
        if start == -1:
            break
        open_brace = css.index("{", start)
        depth = 0
        for i in range(open_brace, len(css)):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(css[open_brace + 1 : i])
                    cursor = i
                    break
        else:
            raise AssertionError(f"unterminated {query}")
    assert bodies, f"no {query} block"
    return "\n".join(bodies)


def test_sidebar_section_headings_share_one_overline_size() -> None:
    """The <h2>s, the Legend <summary> and the Service Alerts <summary> are the same
    rank in the same column. Service Alerts used to render a step larger (12px) than the
    <h2>s it sits between, which read as drift, not hierarchy."""
    assert "#panel h2,\n#legend-details summary {" in LAYERS_CSS
    heading_rule = LAYERS_CSS[LAYERS_CSS.index("#panel h2,\n#legend-details summary {") :]
    heading_rule = heading_rule[: heading_rule.index("}")]
    assert "font-size: 11px;" in heading_rule
    alerts_summary = ALERTS_CSS[ALERTS_CSS.index(".alerts-details summary {") :]
    alerts_summary = alerts_summary[: alerts_summary.index("}")]
    assert "font-size: 11px;" in alerts_summary


def test_every_interactive_control_has_the_same_focus_ring() -> None:
    """Focus styling used to be present on the borough/route chips and absent on the
    detail panel's close button, both <details> summaries and the layer checkboxes --
    which meant those four fell back to Chrome's default blue ring, the one off-palette
    colour on the page. detail-panel.js focuses the close button on every open, so that
    ring was visible constantly."""
    controls = (
        ".borough-chip",
        '#layers input[type="checkbox"]',
        "#legend-details summary",
        ".alerts-details summary",
        ".route-chip[data-route]",
        ".detail-panel-close",
    )
    for selector in controls:
        rule = f"{selector}:focus-visible"
        assert rule in COMPONENT_CSS, selector
        declaration = COMPONENT_CSS[COMPONENT_CSS.index(rule) :]
        declaration = declaration[: declaration.index("}")]
        assert "outline: 2px solid var(--text);" in declaration, selector


def test_loading_skeleton_matches_the_row_it_stands_in_for() -> None:
    """The stop-list skeleton was `padding: 5px 0` around a 10px chip where the real row
    is `padding: 3px 0` around a ~17px line box, so the list jumped when the data landed.
    The min-height is derived from the real row's own metrics so they cannot drift."""
    real_row = DETAIL_CSS[DETAIL_CSS.index(".detail-stop-list li {") :]
    real_row = real_row[: real_row.index("}")]
    skeleton_row = DETAIL_CSS[DETAIL_CSS.index(".skeleton-row {") :]
    skeleton_row = skeleton_row[: skeleton_row.index("}")]
    assert "padding: 3px 0;" in real_row
    assert "padding: 3px 0;" in skeleton_row
    assert "min-height: calc(1.45em + 6px);" in skeleton_row


def test_density_history_has_a_visible_loading_state() -> None:
    """The camera panel's density section used to have a screen-reader-only loading
    state and no visual one at all -- zero height, so the panel reflowed when
    /api/camera_density_history answered, while the subway panel one click away got a
    skeleton. Driven off the aria-busy detail-panel.js already sets; no markup change."""
    assert '#camera-density-history[aria-busy="true"]' in DETAIL_CSS
    rule = DETAIL_CSS[DETAIL_CSS.index('#camera-density-history[aria-busy="true"] {') :]
    rule = rule[: rule.index("}")]
    assert "animation: detail-shimmer" in rule
    assert "min-height:" in rule
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert 'id="camera-density-history" class="detail-loading" aria-busy="true"' in detail_js
    reduced = _media_block(
        DETAIL_CSS, "@media (prefers-reduced-motion: reduce) {\n  .skeleton-chip,"
    )
    assert "animation: none" in reduced


def test_empty_state_icon_slot_is_size_normalised() -> None:
    """emptyStateHtml() is called with both emoji ("⚠️", "📷", "🚇", "✅") and 14px
    icon() SVGs. Unnormalised, the same "could not load" error looked urgent in the
    subway panel (large colour emoji) and incidental in the camera panel (small grey
    stroke triangle)."""
    slot = DETAIL_CSS[DETAIL_CSS.index(".detail-empty-icon {") :]
    slot = slot[: slot.index("}")]
    assert "width: 16px;" in slot
    svg = DETAIL_CSS[DETAIL_CSS.index(".detail-empty-icon .icon {") :]
    svg = svg[: svg.index("}")]
    assert "width: 16px;" in svg and "height: 16px;" in svg
    assert "color: inherit;" in svg


def test_camera_frame_reserves_its_space_without_cropping() -> None:
    """nyctmc.org serves 352x240. Reserving the box stops the density section below from
    being shoved down when the JPEG decodes; `contain` (not `cover`) means a camera that
    ever serves another size is letterboxed rather than silently cropped -- no pixel of
    a live feed may be hidden to make a box fit."""
    rule = DETAIL_CSS[DETAIL_CSS.index(".camera-live img {") :]
    rule = rule[: rule.index("}")]
    assert "aspect-ratio: 22 / 15;" in rule
    assert "object-fit: contain;" in rule


def test_phone_width_touch_targets_meet_the_44px_baseline() -> None:
    """An earlier accessibility pass set 44px for the layer rows and the map controls.
    Three controls were missed: the Service Alerts summary (its comment claimed a grown
    tap target while setting `min-height: 20px`), the Legend summary, and the alert route
    chips -- which are real click targets (they drive the map highlight) at ~14x17px."""
    alerts_mobile = _media_block(ALERTS_CSS, "@media (max-width: 480px)")
    assert "min-height: 44px;" in alerts_mobile
    assert "min-height: 20px" not in DECLARATIONS_ONLY
    assert "min-width: 28px;" in alerts_mobile and "height: 28px;" in alerts_mobile

    layers_mobile = _media_block(LAYERS_CSS, "@media (max-width: 480px)")
    assert ".layer-head { min-height: 44px" in layers_mobile  # preserved from that pass
    assert "#legend-details summary { min-height: 44px;" in layers_mobile

    detail_mobile = _media_block(DETAIL_CSS, "@media (max-width: 480px)")
    # Grown via a transparent ::after (24px button + 10px on each side = 44px) rather
    # than by resizing the button, whose head is `align-items: flex-start` around titles
    # that wrap to two and three lines.
    assert ".detail-panel-close::after" in detail_mobile
    assert "inset: -10px;" in detail_mobile


def test_component_css_derives_its_tints_from_tokens_not_hand_picked_hex() -> None:
    """tokens.css's own header says every other stylesheet references its custom
    properties and never a raw hex. These three had a hand-lightened error pink and
    three hardcoded rgba() restatements of --muted/--fresh that would not track a
    palette change. The documented per-layer marker colours stay: they deliberately
    mirror map-layers.js's colourblind-validated palette, not the token set."""
    assert "#ffb3c4" not in DECLARATIONS_ONLY
    assert "rgba(141, 153, 174" not in DECLARATIONS_ONLY
    assert "rgba(46, 204, 113" not in DECLARATIONS_ONLY
    assert COMPONENT_CSS.count("color-mix(in srgb, var(--") >= 10
    # the intentional exception, still present and still commented
    assert ".icon.layer-marker-subway_arrivals { color: #f4d35e; }" in LAYERS_CSS


def test_critical_violations_are_never_signalled_by_colour_alone() -> None:
    """violationRowHtml (detail-panel.js) emits `data-critical` plus an inline ⚠️ and the
    flag word. The CSS adds a left rule and a tint on top of those; it must not become
    the only channel."""
    assert '.inspection-violation-row[data-critical="true"]' in DETAIL_CSS
    rule = DETAIL_CSS[DETAIL_CSS.index('.inspection-violation-row[data-critical="true"] {') :]
    rule = rule[: rule.index("}")]
    assert "border-left-color: var(--error);" in rule
    detail_js = (STATIC_DIR / "js" / "detail-panel.js").read_text()
    assert '<span aria-hidden="true">⚠️</span>' in detail_js
    assert "inspection-violation-flag" in detail_js


def test_long_running_lists_do_not_scroll_chain_into_the_page() -> None:
    """The alerts list (194 active, observed live) and the stop/violation list are inner
    scroll containers. Without this, a flick that reached their end carried the sidebar
    -- or the map -- with it."""
    for block in (".alerts-list {", ".detail-stop-list {", ".detail-panel {"):
        css = ALERTS_CSS if "alerts" in block else DETAIL_CSS
        rule = css[css.index(block) :]
        rule = rule[: rule.index("}")]
        assert "overscroll-behavior: contain;" in rule, block


# -- icons.js glyph invariants -------------------------------------------------

ICONS_JS = (STATIC_DIR / "js" / "icons.js").read_text()
# Every `  key: `...`,` entry in the ICONS table. Parsed rather than string-searched
# because the two colour rules below must apply to the glyph bodies ONLY: the file also
# contains a legitimate literal white (iconGlyphDataUri's `color="#ffffff"`, which is
# what resolves the glyphs' own currentColor when they are rasterised into the atlas).
ICON_GLYPHS = dict(re.findall(r"^  ([a-z0-9_]+): `([^`]*)`,$", ICONS_JS, flags=re.MULTILINE))
# The three feeds that shipped with live data and no glyph (ny511_events,
# mta_elevator_outages, air_quality), plus NYC Ferry.
NEW_GLYPH_KEYS = ("incident", "elevator", "air_quality", "ferry")


def test_icons_table_parses_and_covers_the_glyphless_feeds() -> None:
    assert len(ICON_GLYPHS) >= 13, "the ICONS entry regex stopped matching the table"
    for key in NEW_GLYPH_KEYS:
        assert key in ICON_GLYPHS, key


def test_every_glyph_paints_only_with_currentcolor() -> None:
    """ICON_ATLAS_KEYS is Object.keys(ICONS), so each of these is packed into the runtime
    sprite atlas and rasterised as a WHITE mask, then tinted by the IconLayer's own
    getColor (mask: true) -- and recoloured in the sidebar by plain CSS `color`. A literal
    colour anywhere in a glyph body survives both and freezes that marker to a baked-in
    hex, so `currentColor` (or `none`) is the only paint value allowed."""
    for key, glyph in ICON_GLYPHS.items():
        assert "currentColor" in glyph, key
        paints = re.findall(r'(?:^|\s)(?:fill|stroke)="([^"]*)"', glyph)
        assert paints, key
        assert set(paints) <= {"currentColor", "none"}, (key, sorted(set(paints)))
        assert "#" not in glyph, key
        for literal in ("rgb(", "rgba(", "hsl(", "url(", "white", "black"):
            assert literal not in glyph, (key, literal)


def test_every_glyph_is_a_bare_16x16_fragment() -> None:
    """No wrapper <svg>: icon() and iconGlyphDataUri each supply their own (with different
    width/height -- 14px in the sidebar, ICON_ATLAS_CELL in the atlas), so a glyph that
    carried its own would nest and render at the wrong size in one of the two."""
    for key, glyph in ICON_GLYPHS.items():
        assert "<svg" not in glyph, key
        assert "viewBox" not in glyph, key
    assert 'viewBox="0 0 16 16" width="14" height="14"' in ICONS_JS  # icon()
    assert 'viewBox="0 0 16 16" ` +' in ICONS_JS  # iconGlyphDataUri
    assert "const ICON_ATLAS_KEYS = Object.keys(ICONS);" in ICONS_JS


def test_new_glyphs_keep_the_sets_stroke_weights() -> None:
    """A new glyph may be a filled silhouette (the 15px map size eats fine interior
    strokes), but where it does stroke, it must use a weight the set already uses --
    otherwise the sidebar column, where all 13 sit together at 14px, reads as two
    different icon sets."""
    established = {
        width
        for key, glyph in ICON_GLYPHS.items()
        if key not in NEW_GLYPH_KEYS
        for width in re.findall(r'stroke-width="([\d.]+)"', glyph)
    }
    assert established, "no stroke-width found in the pre-existing glyphs"
    for key in NEW_GLYPH_KEYS:
        for width in re.findall(r'stroke-width="([\d.]+)"', ICON_GLYPHS[key]):
            assert width in established, (key, width, sorted(established))
