/*
 * Boot: map init and page wiring. This is the last script loaded -- every function it
 * calls (buildPanel, refreshAll, connectStream, closeDetailPanel, handleMapClick,
 * tooltip) is defined in an earlier-loaded file.
 *
 * MapLibre GL and deck.gl come from pinned CDN URLs (see index.html). If they fail
 * to load, the banner says so and the feed pills keep working without a map.
 */

function initMap() {
  if (typeof maplibregl === "undefined" || typeof deck === "undefined") {
    banner(
      "Map libraries (maplibre-gl, deck.gl) could not be loaded from unpkg.com, so the map " +
        "is not shown. Feed status and errors below are live."
    );
    return false;
  }
  map = new maplibregl.Map({
    container: "map",
    style: BASEMAP_STYLE,
    center: [NYC.longitude, NYC.latitude],
    zoom: NYC.zoom,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  map.addControl(createRecenterControl(), "top-left");
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
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
