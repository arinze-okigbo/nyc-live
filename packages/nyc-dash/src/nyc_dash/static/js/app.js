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
  map.addControl(createCopyLinkControl(), "top-left");
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

// ---------------------------------------------------------------------------
// Global keyboard layer
// ---------------------------------------------------------------------------
//
// Everything a mouse can do to the *page chrome* (focus search, toggle a layer, recenter,
// filter by borough) now has a key, plus a `?` overlay that documents both the new keys
// and the ones that already existed but were invisible (Escape closes the detail panel,
// Arrow/Enter drive the search results, Tab is trapped inside the open panel).
//
// Three rules this layer is built around, because they are the three ways a keyboard
// layer usually goes wrong:
//
// 1. It never fires while the user is typing. `isTypingContext` short-circuits every
//    single-character shortcut whenever focus is in an <input>/<textarea>/<select> or
//    anything contenteditable -- typing "brooklyn 1 av" into #search-input must not
//    toggle layers, cycle boroughs or recenter the map. Escape is the one deliberate
//    exception (it is not a character key, and "Escape means dismiss" is expected
//    everywhere, including inside a text field).
// 2. It never takes a key the browser or the OS owns: any Ctrl/Cmd/Alt combination is
//    handed straight back. Shift is *not* in that list -- `?` is Shift+/ on most
//    layouts, so bailing on Shift would make the help overlay unreachable.
// 3. It composes with the handlers that were already here rather than racing them.
//    This listener sits on `document` and runs after search.js's own input-level
//    keydown (which handles Arrow/Enter/Escape for the results list) has bubbled, and
//    it returns immediately on an already-`defaultPrevented` event, so the two never
//    double-fire on the same keystroke.
const SHORTCUT_LAYER_DIGIT_MIN = 1;
const SHORTCUT_LAYER_DIGIT_MAX = 9;

// The cycle order for `b`: the "All" pseudo-borough first, then state.js's canonical
// BOROUGHS in sidebar-chip order, so the key walks the same chips left to right.
const BOROUGH_CYCLE = ["all", ...BOROUGHS];

function isTypingTarget(node) {
  if (!node || node.nodeType !== 1) return false;
  if (node.isContentEditable) return true;
  const tag = node.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

// Both `ev.target` and `document.activeElement` are checked: the first is what actually
// received the keystroke, the second catches the (rare, but real) case of a synthetic or
// retargeted event dispatched at the document while a field genuinely holds focus.
function isTypingContext(ev) {
  return isTypingTarget(ev.target) || isTypingTarget(document.activeElement);
}

function hasCommandModifier(ev) {
  return ev.ctrlKey || ev.metaKey || ev.altKey;
}

// A polite, one-line announcement for actions whose only other feedback is a visual
// change somewhere the user may not be looking (a sidebar checkbox, the map's center).
function announceShortcut(message) {
  const node = el("shortcut-status");
  if (node) node.textContent = message;
}

function focusSearchInput() {
  const input = el("search-input");
  if (!input) return false;
  input.focus({ preventScroll: true });
  // Select rather than append: `/` on an already-filled box should start a new query.
  if (typeof input.select === "function") input.select();
  return true;
}

// Drives the sidebar checkbox rather than writing `state.get(key).visible` directly, so
// buildPanel()'s existing change listener (status-panel.js) stays the single write site
// for layer visibility and the checkbox, the map and the shortcut can never disagree.
function toggleLayerByIndex(index) {
  if (typeof FEEDS === "undefined" || !FEEDS[index]) return false;
  const feed = FEEDS[index];
  const checkbox = el(`toggle-${feed.key}`);
  if (!checkbox) return false;
  checkbox.click();
  announceShortcut(`${feed.label} layer ${checkbox.checked ? "shown" : "hidden"}`);
  return true;
}

// Same flyTo target as the map's own recenter control (map-layers.js's
// createRecenterControl), read from the same NYC constant rather than reaching into
// that control's DOM button, which belongs to another file.
function recenterMap() {
  if (!map) return false;
  map.flyTo({ center: [NYC.longitude, NYC.latitude], zoom: NYC.zoom });
  announceShortcut("Map recentered on New York City");
  return true;
}

function cycleBoroughFilter() {
  if (typeof setSelectedBorough !== "function") return false;
  const current = BOROUGH_CYCLE.indexOf(selectedBorough);
  const next = BOROUGH_CYCLE[(current + 1) % BOROUGH_CYCLE.length];
  setSelectedBorough(next);
  announceShortcut(next === "all" ? "Borough filter cleared" : `Borough filter: ${next}`);
  return true;
}

// The non-layer half of the help overlay's table. The last four rows document behavior
// that already shipped in other files (detail-panel.js's Escape/Tab trap, search.js's
// Arrow/Enter handling) and was previously undiscoverable -- listing it here is the
// whole point of the overlay, so those rows must stay in sync with those files.
const SHORTCUT_ROWS = [
  { keys: ["/"], description: "Jump to the search box (and select what is already in it)" },
  { keys: ["?"], description: "Open or close this shortcut list" },
  { keys: ["R"], description: "Recenter the map on New York City" },
  { keys: ["B"], description: "Cycle the borough filter: All, then each borough in turn" },
  { keys: ["Esc"], description: "Close this overlay, close an open detail panel, or clear the search box" },
  { keys: ["↑", "↓"], description: "Move through search results while the search box is focused" },
  { keys: ["Enter"], description: "Open the highlighted search result" },
  { keys: ["Tab"], description: "Move between controls; cycles within an open detail panel or this overlay" },
];

function shortcutRowHtml(keys, description) {
  const keysHtml = keys
    .map((key) => `<kbd>${escapeHtml(key)}</kbd>`)
    .join('<span class="shortcut-key-sep">/</span>');
  return `<li><span class="shortcut-keys">${keysHtml}</span><span class="shortcut-desc">${escapeHtml(
    description
  )}</span></li>`;
}

// Built at open time, not baked into index.html, so the layer rows name the layers that
// actually exist (FEEDS, map-layers.js) in the order the sidebar lists them. If FEEDS
// ever grows past nine entries the extra layers simply get no digit rather than a
// fabricated one.
function renderShortcutsBody() {
  const body = el("shortcuts-body");
  if (!body) return;
  const layerRows =
    typeof FEEDS === "undefined"
      ? []
      : FEEDS.slice(0, SHORTCUT_LAYER_DIGIT_MAX).map((feed, index) =>
          shortcutRowHtml([String(index + SHORTCUT_LAYER_DIGIT_MIN)], `Show or hide ${feed.label}`)
        );
  const layersSection = layerRows.length
    ? `<h3 class="shortcuts-section-title">Layers</h3>
       <ul class="shortcuts-list">${layerRows.join("")}</ul>`
    : "";
  body.innerHTML = `
    <h3 class="shortcuts-section-title">Navigation</h3>
    <ul class="shortcuts-list">${SHORTCUT_ROWS.map((row) =>
      shortcutRowHtml(row.keys, row.description)
    ).join("")}</ul>
    ${layersSection}`;
}

// Whatever had focus when the overlay opened, restored on close so a keyboard user is
// put back where they were instead of at the top of the document. Same contract as
// detail-panel.js's panelTriggerElement.
let shortcutsTrigger = null;

// Deliberately a local copy of detail-panel.js's PANEL_FOCUSABLE_SELECTOR idea rather
// than a call into that file: this overlay must keep working regardless of what happens
// to detail-panel.js, and the whole trap is a dozen lines.
const SHORTCUTS_FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';

function isShortcutsOpen() {
  const overlay = el("shortcuts-overlay");
  return Boolean(overlay) && !overlay.hidden;
}

// Tab/Shift+Tab cycle only among the overlay's own controls while it is open. Attached
// to the overlay element itself, so it is a no-op whenever the overlay is hidden.
function trapShortcutsFocus(ev) {
  if (ev.key !== "Tab") return;
  const overlay = el("shortcuts-overlay");
  if (!overlay || overlay.hidden) return;
  const focusable = Array.from(overlay.querySelectorAll(SHORTCUTS_FOCUSABLE_SELECTOR)).filter(
    (node) => node.getClientRects().length > 0
  );
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const goingBackward = ev.shiftKey;
  const atEdge = goingBackward ? document.activeElement === first : document.activeElement === last;
  if (atEdge || !overlay.contains(document.activeElement)) {
    ev.preventDefault();
    (goingBackward ? last : first).focus();
  }
}

function setShortcutsButtonExpanded(expanded) {
  const button = el("shortcuts-button");
  if (button) button.setAttribute("aria-expanded", expanded ? "true" : "false");
}

function openShortcuts() {
  const overlay = el("shortcuts-overlay");
  if (!overlay || !overlay.hidden) return false;
  renderShortcutsBody();
  shortcutsTrigger = document.activeElement;
  overlay.hidden = false;
  setShortcutsButtonExpanded(true);
  const close = el("shortcuts-close");
  if (close) close.focus({ preventScroll: true });
  return true;
}

function closeShortcuts() {
  const overlay = el("shortcuts-overlay");
  if (!overlay || overlay.hidden) return false;
  overlay.hidden = true;
  setShortcutsButtonExpanded(false);
  const trigger = shortcutsTrigger;
  shortcutsTrigger = null;
  // Focusing a node that has since left the DOM throws in some browsers and restores
  // nothing useful in the rest, so an unusable trigger is a deliberate no-op.
  if (trigger && trigger.isConnected && typeof trigger.focus === "function") {
    trigger.focus({ preventScroll: true });
  }
  return true;
}

function toggleShortcuts() {
  return isShortcutsOpen() ? closeShortcuts() : openShortcuts();
}

// Escape is handled ahead of the typing guard on purpose: it is not a character key, so
// swallowing it inside a text field would be wrong. search.js clears the search box from
// its own input-level handler; this layer only decides between "dismiss the overlay" and
// "dismiss the detail panel", closing at most one thing per press (topmost first).
function handleEscapeKey(ev) {
  if (isShortcutsOpen()) {
    ev.preventDefault();
    closeShortcuts();
    return;
  }
  closeDetailPanel();
}

const SHORTCUT_ACTIONS = {
  "/": focusSearchInput,
  "?": toggleShortcuts,
  r: recenterMap,
  b: cycleBoroughFilter,
};

function digitLayerIndex(key) {
  if (key < String(SHORTCUT_LAYER_DIGIT_MIN) || key > String(SHORTCUT_LAYER_DIGIT_MAX)) return -1;
  return Number(key) - SHORTCUT_LAYER_DIGIT_MIN;
}

function handleGlobalKeydown(ev) {
  // Already acted on by a more specific handler (search.js's Arrow/Enter, the focus
  // traps): never act on the same keystroke twice.
  if (ev.defaultPrevented) return;
  // Mid-IME composition: `key` is meaningless here (229 is the legacy signal for it).
  if (ev.isComposing || ev.keyCode === 229) return;
  if (ev.key === "Escape") {
    handleEscapeKey(ev);
    return;
  }
  if (hasCommandModifier(ev)) return; // Cmd/Ctrl/Alt combinations belong to the browser
  if (isTypingContext(ev)) return; // rule 1: never hijack a key while the user types
  if (ev.key.length !== 1) return; // Tab, arrows, F-keys and friends are not ours
  const key = ev.key.toLowerCase();
  if (isShortcutsOpen()) {
    // While the overlay is up, everything behind it is inert; only `?` (close) applies.
    if (key === "?") {
      ev.preventDefault();
      closeShortcuts();
    }
    return;
  }
  const layerIndex = digitLayerIndex(key);
  if (layerIndex >= 0) {
    if (toggleLayerByIndex(layerIndex)) ev.preventDefault();
    return;
  }
  const action = SHORTCUT_ACTIONS[key];
  if (!action) return;
  // preventDefault only when the action really ran, so an unavailable target (no map
  // yet, no search box) leaves the browser's own behavior for that key intact.
  if (action()) ev.preventDefault();
}

function initKeyboardShortcuts() {
  document.addEventListener("keydown", handleGlobalKeydown);
  const overlay = el("shortcuts-overlay");
  if (overlay) {
    overlay.addEventListener("keydown", trapShortcutsFocus);
    // Click-outside dismiss: only a click on the backdrop itself, never one that
    // started inside the dialog.
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) closeShortcuts();
    });
  }
  const close = el("shortcuts-close");
  if (close) close.addEventListener("click", () => closeShortcuts());
  const button = el("shortcuts-button");
  if (button) button.addEventListener("click", () => toggleShortcuts());
}

function start() {
  buildPanel();
  initAlertsBanner();
  initMap();
  initKeyboardShortcuts();
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
