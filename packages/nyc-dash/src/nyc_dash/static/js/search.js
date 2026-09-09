/*
 * Sidebar search: find an already-fetched subway station, Citi Bike dock, or DOT camera
 * by name and jump straight to it, instead of hunting for one marker among thousands on
 * the map; or find a bus route by number and highlight its live vehicles on the map.
 * Entirely client-side over data the app already polls -- no new backend endpoint.
 *
 * Depends on: utils.js (el, escapeHtml), state.js (state, map, highlightedBusRoute),
 * map-layers.js (busRouteLabel, renderLayers), detail-panel.js (DETAIL_BUILDERS).
 * Self-initializes on DOMContentLoaded like app.js does, since this file must not
 * require app.js to know about it.
 *
 * Three of the four searchable kinds (subway, Citi Bike, DOT cameras) resolve to one
 * named record with coordinates: selecting one flies the map there and opens that
 * record's real detail panel via the shared DETAIL_BUILDERS table, exactly the same
 * handoff a map click already uses. Bus routes are different in kind -- a route is many
 * live vehicles, not one point -- so a bus-route result instead sets `highlightedBusRoute`
 * (state.js) and lets busLayer() (map-layers.js) bring that route's vehicles forward on
 * the map already on screen, the same "drive a map layer through a shared global" idiom
 * alerts-banner.js's route chips already established for highlightedRoute.
 */

// A keystroke-by-keystroke re-filter over ~800 subway arrivals + ~2500 Citi Bike
// stations is cheap, but a short debounce keeps fast typing from queuing up redundant
// filter/render passes.
const SEARCH_DEBOUNCE_MS = 120;
// Enough to see every close match without the dropdown outgrowing the sidebar.
const SEARCH_MAX_RESULTS_PER_KIND = 6;
// Close enough to actually distinguish one station/dock from its neighbors, matching
// the zoom level createRecenterControl() (map-layers.js) uses for its own flyTo, just
// tighter since this centers on one point rather than the whole city.
const SEARCH_FLYTO_ZOOM = 16;

let searchDebounceTimer = null;
let currentSearchResults = [];

function searchNormalize(text) {
  return text.trim().toLowerCase();
}

// Reads the same `state` map every layer builder reads (map-layers.js), so search never
// has its own copy of the data or its own idea of what "fresh" means: an errored feed
// contributes zero results here for the same reason it draws nothing on the map.
function searchRecordsFor(key) {
  const entry = state.get(key);
  if (!entry || !entry.envelope || entry.envelope.status === "error") return [];
  return entry.envelope.records || [];
}

// subway_arrivals is one row per (trip, stop) pair, so a busy station shows up once per
// arriving train. A search result should be one per station: keep only the
// soonest-arriving train for each stop_id, so "station name" maps to exactly one pin to
// fly to and exactly one detail panel to open (that train's, same as clicking it would).
function searchSubwayResults(query) {
  const bestByStop = new Map(); // stop_id -> soonest-eta record
  for (const record of searchRecordsFor("subway_arrivals")) {
    if (!record.stop_name || record.lat == null || record.lon == null) continue;
    if (!record.stop_name.toLowerCase().includes(query)) continue;
    const existing = bestByStop.get(record.stop_id);
    if (!existing || record.eta_s < existing.eta_s) bestByStop.set(record.stop_id, record);
  }
  return Array.from(bestByStop.values())
    .sort((a, b) => a.stop_name.localeCompare(b.stop_name))
    .slice(0, SEARCH_MAX_RESULTS_PER_KIND)
    .map((record) => ({
      kind: "subway",
      label: record.stop_name,
      sublabel: `${record.route_id} train · ${Math.round(record.eta_s / 60)} min`,
      lat: record.lat,
      lon: record.lon,
      record,
    }));
}

function searchCitibikeResults(query) {
  return searchRecordsFor("citibike")
    .filter((record) => record.name && record.name.toLowerCase().includes(query))
    .sort((a, b) => a.name.localeCompare(b.name))
    .slice(0, SEARCH_MAX_RESULTS_PER_KIND)
    .map((record) => ({
      kind: "citibike",
      label: record.name,
      sublabel: `${record.bikes_available} bikes · ${record.docks_available} docks`,
      lat: record.lat,
      lon: record.lon,
      record,
    }));
}

// `kind: "cameras"` matches DETAIL_BUILDERS' actual key for this record type
// (detail-panel.js: `cameras: cameraDetail`), not the dot_cameras state/feed key --
// same as how searchSubwayResults' "subway" kind matches DETAIL_BUILDERS.subway rather
// than the subway_arrivals state key. selectSearchResult looks results up by `kind`.
function searchCameraResults(query) {
  return searchRecordsFor("dot_cameras")
    .filter((record) => record.name && record.name.toLowerCase().includes(query))
    .sort((a, b) => a.name.localeCompare(b.name))
    .slice(0, SEARCH_MAX_RESULTS_PER_KIND)
    .map((record) => ({
      kind: "cameras",
      label: record.name,
      sublabel: [record.area, record.is_online ? "online" : "offline"]
        .filter(Boolean)
        .join(" · "),
      lat: record.lat,
      lon: record.lon,
      record,
    }));
}

// Bus routes have no single named record to fly to (BusVehicle.route_id) -- a route is
// however many vehicles are currently out on it. This groups already-fetched mta_bus
// vehicles by short route label (busRouteLabel, map-layers.js -- the same helper that
// colors bus markers and titles the bus detail panel, not a second parser) and returns
// one result per matching route, kind "bus_route", carrying that label instead of a
// lat/lon/record. selectSearchResult branches on this kind to highlight the route on
// the map rather than flying to a point.
function searchBusRouteResults(query) {
  const vehicleCountByRoute = new Map(); // short route label -> live vehicle count
  for (const record of searchRecordsFor("mta_bus")) {
    if (!record.route_id) continue;
    const label = busRouteLabel(record.route_id);
    if (!label.toLowerCase().includes(query)) continue;
    vehicleCountByRoute.set(label, (vehicleCountByRoute.get(label) || 0) + 1);
  }
  return Array.from(vehicleCountByRoute.entries())
    .sort((a, b) => a[0].localeCompare(b[0]))
    .slice(0, SEARCH_MAX_RESULTS_PER_KIND)
    .map(([route, count]) => ({
      kind: "bus_route",
      label: `${route} bus route`,
      sublabel: `${count} bus${count === 1 ? "" : "es"} live now`,
      route,
    }));
}

function runSearch(rawQuery) {
  const query = searchNormalize(rawQuery);
  if (!query) return [];
  return [
    ...searchSubwayResults(query),
    ...searchCitibikeResults(query),
    ...searchCameraResults(query),
    ...searchBusRouteResults(query),
  ];
}

const SEARCH_KIND_LABELS = {
  subway: "Subway",
  citibike: "Citi Bike",
  cameras: "DOT Camera",
  bus_route: "Bus Route",
};

function searchKindLabel(kind) {
  return SEARCH_KIND_LABELS[kind] || kind;
}

function renderSearchResults(results, query) {
  const list = el("search-results");
  if (!query) {
    list.hidden = true;
    list.innerHTML = "";
    return;
  }
  if (!results.length) {
    list.hidden = false;
    list.innerHTML = `<li class="search-result-empty">No matches for "${escapeHtml(query)}"</li>`;
    return;
  }
  list.hidden = false;
  list.innerHTML = results
    .map(
      (result, index) => `
      <li class="search-result" role="option" data-index="${index}" tabindex="-1">
        <span class="search-result-kind" data-kind="${result.kind}">${searchKindLabel(result.kind)}</span>
        <span class="search-result-text">
          <span class="search-result-label">${escapeHtml(result.label)}</span>
          <span class="search-result-sublabel">${escapeHtml(result.sublabel)}</span>
        </span>
      </li>`
    )
    .join("");
}

// A bus-route search result implies the user wants to see that route on the map, and
// the mta_bus layer defaults to off (map-layers.js's FEEDS entry, same as dot_cameras
// and dohmh_inspections -- dense data, opt-in). Auto-enabling it here (rather than just
// documenting "check the Buses box first") is the same reasoning bikeLayer's own
// zoom-declutter already follows: don't make the user discover a second manual step to
// see the result of the thing they just asked for. Mirrors buildPanel()'s checkbox
// wiring (status-panel.js) so the sidebar checkbox itself reflects the change, not just
// the internal state.
function enableBusLayer() {
  const entry = state.get("mta_bus");
  if (!entry || entry.visible) return;
  entry.visible = true;
  const checkbox = el("toggle-mta_bus");
  if (checkbox) checkbox.checked = true;
}

// The one write site for the shared `highlightedBusRoute` (state.js), mirroring
// alerts-banner.js's selectRoute(): selecting the already-highlighted route clears it
// (second selection = revert), selecting a different one replaces it. No flyTo here --
// unlike the other three kinds, a bus route isn't one point, so the map stays put and
// busLayer() (map-layers.js) re-renders in place to bring that route's vehicles forward.
function selectBusRouteResult(result) {
  highlightedBusRoute = highlightedBusRoute === result.route ? null : result.route;
  enableBusLayer();
  if (typeof renderLayers === "function") renderLayers();
}

// Same handoff handleMapClick (detail-panel.js) already uses for a clicked marker: look
// the record's kind up in the shared DETAIL_BUILDERS table rather than building a
// bespoke subway/citibike/camera detail view just for search results.
function selectSearchResult(result) {
  if (result.kind === "bus_route") {
    selectBusRouteResult(result);
    clearSearch();
    return;
  }
  if (map) {
    map.flyTo({ center: [result.lon, result.lat], zoom: SEARCH_FLYTO_ZOOM });
  }
  const builder = DETAIL_BUILDERS[result.kind];
  if (builder) builder(result.record);
  clearSearch();
}

function clearSearch() {
  const input = el("search-input");
  if (input) input.value = "";
  currentSearchResults = [];
  renderSearchResults([], "");
}

function handleSearchInput(ev) {
  const rawQuery = ev.target.value;
  if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    currentSearchResults = runSearch(rawQuery);
    renderSearchResults(currentSearchResults, searchNormalize(rawQuery));
  }, SEARCH_DEBOUNCE_MS);
}

function handleSearchResultClick(ev) {
  const item = ev.target.closest(".search-result");
  if (!item) return;
  const result = currentSearchResults[Number(item.dataset.index)];
  if (result) selectSearchResult(result);
}

function initSearch() {
  const input = el("search-input");
  const results = el("search-results");
  if (!input || !results) return; // markup not present; nothing to wire up
  input.addEventListener("input", handleSearchInput);
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") clearSearch();
  });
  results.addEventListener("click", handleSearchResultClick);
  // Clicking anywhere outside the search box dismisses an open results list, the same
  // "click away closes it" convention the detail panel's own Escape handling implies.
  document.addEventListener("click", (ev) => {
    if (ev.target === input || results.contains(ev.target)) return;
    if (!results.hidden) renderSearchResults([], "");
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initSearch);
} else {
  initSearch();
}
