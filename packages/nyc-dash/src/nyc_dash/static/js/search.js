/*
 * Sidebar search: find an already-fetched subway station or Citi Bike dock by name and
 * jump straight to it, instead of hunting for one marker among thousands on the map.
 * Entirely client-side over data the app already polls -- no new backend endpoint.
 *
 * Depends on: utils.js (el, escapeHtml), state.js (state, map), detail-panel.js
 * (DETAIL_BUILDERS). Self-initializes on DOMContentLoaded like app.js does, since this
 * file must not require app.js to know about it.
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

function runSearch(rawQuery) {
  const query = searchNormalize(rawQuery);
  if (!query) return [];
  return [...searchSubwayResults(query), ...searchCitibikeResults(query)];
}

function searchKindLabel(kind) {
  return kind === "subway" ? "Subway" : "Citi Bike";
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

// Same handoff handleMapClick (detail-panel.js) already uses for a clicked marker: look
// the record's kind up in the shared DETAIL_BUILDERS table rather than building a
// bespoke subway/citibike detail view just for search results.
function selectSearchResult(result) {
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
