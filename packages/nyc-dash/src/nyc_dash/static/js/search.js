/*
 * Sidebar search: find an already-fetched subway station, Citi Bike dock, or DOT camera
 * by name and jump straight to it, instead of hunting for one marker among thousands on
 * the map; or find a bus route by number and highlight its live vehicles on the map.
 * Entirely client-side over data the app already polls -- no new backend endpoint.
 *
 * Depends on: utils.js (el, escapeHtml), state.js (state, map, highlightedBusRoute, NYC),
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
 *
 * Keyboard: ArrowUp/ArrowDown move a highlight through the open results (aria-
 * activedescendant on #search-input, per index.html's role="combobox" -- see
 * syncHighlight below), Enter selects the highlighted result (or the first one, if
 * none has been highlighted yet), Escape clears -- the results themselves are not real
 * Tab stops (tabindex="-1"), so without this a keyboard-only user could see results but
 * never select one.
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
// Index into currentSearchResults the user has reached with ArrowUp/ArrowDown, or -1
// for "nothing highlighted yet" -- see moveHighlight/syncHighlight below. Focus itself
// never leaves #search-input (a real <li> can't take focus: they're tabindex="-1" on
// purpose, see renderSearchResults), so this is tracked as plain state, the same
// "shared mutable value, no import machinery" idiom state.js's own globals use.
let highlightedResultIndex = -1;

function searchNormalize(text) {
  return text.trim().toLowerCase();
}

// Haversine distance in meters. City-scale distances are small enough that an
// equirectangular approximation would be indistinguishable in practice, but Haversine
// is barely more code and gives the real great-circle distance with no approximation
// error to reason about near the edges of the metro area (Staten Island to the Bronx is
// still "the same map"), so there's no reason to reach for the cheaper shortcut here.
// No external geo library -- this file has zero dependencies and a five-line formula
// isn't worth gaining one.
const EARTH_RADIUS_M = 6371000;

function toRadians(degrees) {
  return (degrees * Math.PI) / 180;
}

function haversineDistanceMeters(lat1, lon1, lat2, lon2) {
  const dLat = toRadians(lat2 - lat1);
  const dLon = toRadians(lon2 - lon1);
  const sinHalfLat = Math.sin(dLat / 2);
  const sinHalfLon = Math.sin(dLon / 2);
  const a =
    sinHalfLat * sinHalfLat +
    Math.cos(toRadians(lat1)) * Math.cos(toRadians(lat2)) * sinHalfLon * sinHalfLon;
  return 2 * EARTH_RADIUS_M * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// Where "nearest" is measured from: the map's current center, read fresh every time a
// search actually runs. That's handleSearchInput's existing debounce callback, not a
// new "resort on map pan" listener -- panning while a result list is already open won't
// live-resort it until the next keystroke, which is the same debounce-driven cadence
// this file already uses for re-filtering, not a new one. Falls back to the app's
// default NYC view (state.js's `NYC`, the same constant app.js itself falls back to for
// the initial map view) on the off chance this runs before the map finishes constructing.
function currentSearchOrigin() {
  if (map && typeof map.getCenter === "function") {
    const center = map.getCenter();
    return { lat: center.lat, lon: center.lng };
  }
  return { lat: NYC.latitude, lon: NYC.longitude };
}

// Shared by all three point-based kinds (subway, Citi Bike, cameras): sorts
// already-filtered records nearest-first from `origin` *before* truncating to
// SEARCH_MAX_RESULTS_PER_KIND, so a distant alphabetical match can never bump a closer
// one out of the visible list. Bus routes (searchBusRouteResults) have no single
// lat/lon -- a route is many live vehicles, not one point -- so they're intentionally
// left out of this and keep their existing alphabetical ordering.
function sortByDistance(records, origin) {
  return records
    .map((record) => ({
      record,
      distanceM: haversineDistanceMeters(origin.lat, origin.lon, record.lat, record.lon),
    }))
    .sort((a, b) => a.distanceM - b.distanceM)
    .map((entry) => entry.record);
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
function searchSubwayResults(query, origin) {
  const bestByStop = new Map(); // stop_id -> soonest-eta record
  for (const record of searchRecordsFor("subway_arrivals")) {
    if (!record.stop_name || record.lat == null || record.lon == null) continue;
    if (!record.stop_name.toLowerCase().includes(query)) continue;
    const existing = bestByStop.get(record.stop_id);
    if (!existing || record.eta_s < existing.eta_s) bestByStop.set(record.stop_id, record);
  }
  return sortByDistance(Array.from(bestByStop.values()), origin)
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

function searchCitibikeResults(query, origin) {
  const matches = searchRecordsFor("citibike").filter(
    (record) => record.name && record.name.toLowerCase().includes(query)
  );
  return sortByDistance(matches, origin)
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
function searchCameraResults(query, origin) {
  const matches = searchRecordsFor("dot_cameras").filter(
    (record) => record.name && record.name.toLowerCase().includes(query)
  );
  return sortByDistance(matches, origin)
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

// Results stay grouped by kind (subway block, then Citi Bike, then cameras, then bus
// routes) exactly as before -- only the ordering *within* each kind changed, from
// alphabetical to nearest-first. A single distance-ranked merge across kinds (letting a
// very close camera outrank a farther subway station) was the other option, but the
// list already renders a per-row kind badge and groups by kind for a reason: each kind
// is a different sort of thing to jump to (a station vs. a dock vs. a camera vs. a
// route), and a user scanning for "the subway station near me named 14 St" would find a
// kind-interleaved list harder to scan than four short, internally-sorted groups. Kept
// the existing grouping and only made each group itself distance-aware.
function runSearch(rawQuery) {
  const query = searchNormalize(rawQuery);
  if (!query) return [];
  const origin = currentSearchOrigin();
  return [
    ...searchSubwayResults(query, origin),
    ...searchCitibikeResults(query, origin),
    ...searchCameraResults(query, origin),
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

// Reflects `highlightedResultIndex` onto the DOM: this is the ARIA 1.2 "listbox
// autocomplete" pattern (index.html's #search-input carries role="combobox" +
// aria-controls="search-results" for exactly this) -- focus stays on the text input the
// whole time, and `aria-activedescendant` is how a screen reader is told which <li> is
// "current" instead. The matching item also gets a visible `.is-active` class (paired
// with the existing :hover/:focus-visible treatment in search.css) so sighted keyboard
// users get the same feedback. Called after every render and after every arrow-key move.
function syncHighlight() {
  const input = el("search-input");
  const items = document.querySelectorAll("#search-results .search-result[data-index]");
  items.forEach((item) => {
    const isActive = Number(item.dataset.index) === highlightedResultIndex;
    item.classList.toggle("is-active", isActive);
    item.setAttribute("aria-selected", isActive ? "true" : "false");
  });
  if (!input) return;
  if (highlightedResultIndex === -1) {
    input.removeAttribute("aria-activedescendant");
    return;
  }
  const activeItem = el(`search-result-${highlightedResultIndex}`);
  if (activeItem) {
    input.setAttribute("aria-activedescendant", activeItem.id);
    activeItem.scrollIntoView({ block: "nearest" });
  }
}

function setSearchExpanded(expanded) {
  const input = el("search-input");
  if (input) input.setAttribute("aria-expanded", expanded ? "true" : "false");
}

// Clamps rather than wraps: at either end of the list, repeating the same arrow key
// just stays put instead of jumping to the opposite end, which is easier to reason
// about while scanning a handful of grouped results than a carousel would be.
function moveHighlight(delta) {
  if (!currentSearchResults.length) return;
  const max = currentSearchResults.length - 1;
  highlightedResultIndex =
    highlightedResultIndex === -1
      ? (delta > 0 ? 0 : max)
      : Math.max(0, Math.min(max, highlightedResultIndex + delta));
  syncHighlight();
}

function renderSearchResults(results, query) {
  const list = el("search-results");
  if (!query) {
    list.hidden = true;
    list.innerHTML = "";
  } else if (!results.length) {
    list.hidden = false;
    list.innerHTML = `<li class="search-result-empty">No matches for "${escapeHtml(query)}"</li>`;
  } else {
    list.hidden = false;
    list.innerHTML = results
      .map(
        (result, index) => `
      <li class="search-result" id="search-result-${index}" role="option" data-index="${index}" tabindex="-1" aria-selected="false">
        <span class="search-result-kind" data-kind="${result.kind}">${searchKindLabel(result.kind)}</span>
        <span class="search-result-text">
          <span class="search-result-label">${escapeHtml(result.label)}</span>
          <span class="search-result-sublabel">${escapeHtml(result.sublabel)}</span>
        </span>
      </li>`
      )
      .join("");
  }
  setSearchExpanded(!list.hidden);
  syncHighlight();
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
  // clearSearch() below tears down the clicked <li> immediately, so it can't survive as
  // the detail panel's focus-restoration target (detail-panel.js's panelTriggerElement)
  // for as long as the panel stays open. Supply the search input itself as a fallback --
  // the one enduring, still-focusable control this whole interaction started from -- via
  // the same shared-global handoff highlightedBusRoute already establishes for search ->
  // map communication.
  panelFocusFallback = el("search-input");
  const builder = DETAIL_BUILDERS[result.kind];
  if (builder) builder(result.record);
  clearSearch();
}

function clearSearch() {
  const input = el("search-input");
  if (input) input.value = "";
  currentSearchResults = [];
  highlightedResultIndex = -1;
  renderSearchResults([], "");
}

function handleSearchInput(ev) {
  const rawQuery = ev.target.value;
  if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    currentSearchResults = runSearch(rawQuery);
    // A fresh set of results has no highlight yet -- always start from "nothing
    // selected" rather than carrying an index over from the previous query, which
    // could silently point at an unrelated row (or past the end of a shorter list).
    highlightedResultIndex = -1;
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
  // ArrowUp/ArrowDown/Enter: the keyboard half of the results list. Before this, the
  // dropdown's <li role="option"> items were reachable by mouse only -- they carry
  // tabindex="-1" on purpose (see renderSearchResults), so Tab skips straight over
  // them into whatever's rendered next in #panel, and there was no other way to pick
  // a result without a pointer. This keeps focus on the input itself (the standard
  // ARIA combobox-with-listbox-autocomplete pattern -- see syncHighlight) rather than
  // moving real DOM focus onto an <li>.
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      clearSearch();
      return;
    }
    if (ev.key === "ArrowDown") {
      if (!currentSearchResults.length) return;
      ev.preventDefault(); // don't let the caret jump to the end of the input's text
      moveHighlight(1);
      return;
    }
    if (ev.key === "ArrowUp") {
      if (!currentSearchResults.length) return;
      ev.preventDefault();
      moveHighlight(-1);
      return;
    }
    if (ev.key === "Enter") {
      if (!currentSearchResults.length) return;
      ev.preventDefault(); // no surrounding <form> to submit, but stay explicit
      const index = highlightedResultIndex === -1 ? 0 : highlightedResultIndex;
      selectSearchResult(currentSearchResults[index]);
    }
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
