/*
 * Boot: map init and page wiring. This is the last script loaded -- every function it
 * calls (buildPanel, refreshAll, connectStream, closeDetailPanel, handleMapClick,
 * tooltip) is defined in an earlier-loaded file.
 *
 * MapLibre GL and deck.gl come from pinned CDN URLs (see index.html). If they fail
 * to load, the banner says so and the feed pills keep working without a map.
 */

// Deep-linkable map view: URL hash format `#zoom/lat/lon` (2-decimal zoom, 5-decimal
// lat/lon), e.g. "#12.40/40.73570/-73.99110" -- the same convention Leaflet's `hash`
// plugin and openstreetmap.org's own permalinks use, so a copied URL is compact and
// human-readable, not a base64 blob. Written with `history.replaceState`, never
// `pushState`, so panning/zooming doesn't spam the browser's back-button history.
const HASH_UPDATE_DEBOUNCE_MS = 200;
let hashUpdateTimer = null;

// Parses `location.hash` into a validated { zoom, lat, lon }, or null if it's absent
// or malformed -- callers fall back to the default NYC view rather than crash on a
// hand-edited or corrupted URL.
function parseHashView() {
  const match = /^#(-?\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)$/.exec(location.hash);
  if (!match) return null;
  const zoom = Number(match[1]);
  const lat = Number(match[2]);
  const lon = Number(match[3]);
  if (!Number.isFinite(zoom) || !(zoom >= 0 && zoom <= 22)) return null;
  if (!Number.isFinite(lat) || !(lat >= -90 && lat <= 90)) return null;
  if (!Number.isFinite(lon) || !(lon >= -180 && lon <= 180)) return null;
  return { zoom, lat, lon };
}

// Writes the map's current center/zoom into the URL hash. Called only from
// scheduleHashUpdate's debounce, never directly off a raw "move" event.
function writeHashView() {
  if (!map) return;
  const center = map.getCenter();
  const zoom = map.getZoom();
  const hash = `#${zoom.toFixed(2)}/${center.lat.toFixed(5)}/${center.lng.toFixed(5)}`;
  history.replaceState(null, "", hash);
}

// MapLibre's "moveend" already fires once per gesture (drag release, scroll-zoom
// settle, or a flyTo animation's end) rather than once per animation frame like
// "move" does, but a short debounce is still added as a safety net against bursts of
// closely-spaced moveend events (e.g. momentum scrolling settling in steps).
function scheduleHashUpdate() {
  clearTimeout(hashUpdateTimer);
  hashUpdateTimer = setTimeout(writeHashView, HASH_UPDATE_DEBOUNCE_MS);
}

function initMap() {
  if (typeof maplibregl === "undefined" || typeof deck === "undefined") {
    banner(
      "Map libraries (maplibre-gl, deck.gl) could not be loaded from unpkg.com, so the map " +
        "is not shown. Feed status and errors below are live."
    );
    return false;
  }
  const initialView = parseHashView();
  map = new maplibregl.Map({
    container: "map",
    style: BASEMAP_STYLE,
    center: initialView ? [initialView.lon, initialView.lat] : [NYC.longitude, NYC.latitude],
    zoom: initialView ? initialView.zoom : NYC.zoom,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  map.addControl(createRecenterControl(), "top-left");
  map.on("moveend", scheduleHashUpdate);
  map.on("error", (ev) => {
    if (ev && ev.error && /style|tile/i.test(String(ev.error.message || ""))) {
      banner(`Basemap tiles unavailable (${BASEMAP_STYLE}); data layers still update.`);
    }
  });
  overlay = new deck.MapboxOverlay({
    interleaved: false,
    layers: [],
    getTooltip: tooltip,
    onClick: handleMapClick,
  });
  map.addControl(overlay);

  // Reveal the map once its first full set of tiles has actually painted, instead of
  // showing a blank/half-loaded frame while a city-wide vector basemap streams in. The
  // timeout is a safety net only, in case 'idle' never fires cleanly (e.g. one stuck tile).
  const reveal = () => el("map").classList.add("ready");
  map.once("idle", reveal);
  setTimeout(reveal, 4000);
  return true;
}

function start() {
  buildPanel();
  initAlertsBanner();
  initMap();
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeDetailPanel();
  });
  setConnection("connecting", "connecting…");
  refreshAll().then(connectStream);
  // One-shot: static GTFS route shapes (24h TTL) don't belong in the polling/SSE
  // cycle above (see SUBWAY_SHAPES_KEY in map-layers.js) -- fetch them once via the
  // same fetchFeed/applyEnvelope machinery and let them sit as a map backdrop.
  fetchFeed(SUBWAY_SHAPES_KEY);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
