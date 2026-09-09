/*
 * Service Alerts: a collapsible sidebar section listing active MTA subway service
 * alerts. This feed (mta_subway_alerts) has no lat/lon -- it's route-level, not
 * point-located -- so it is not a map layer and is not part of FEEDS/STREAM_KEYS in
 * map-layers.js. It is not pushed over /api/stream either; this module fetches and
 * polls it independently.
 *
 * Depends on: utils.js (el, escapeHtml, hhmmss), icons.js (icon), map-layers.js
 * (ROUTE_COLORS, so a route chip here matches that route's color on the map, and
 * renderLayers(), called after a chip toggles the highlight below), state.js
 * (highlightedRoute, the shared value this module writes and map-layers.js's
 * subwayShapesLayer() reads). Must load after all of them. Exposes one entry point,
 * initAlertsBanner(), which the boot sequence in app.js calls; nothing here runs at
 * load time on its own.
 *
 * Route chips double as the one control point for `highlightedRoute` (state.js):
 * clicking a chip sets it to that route so the map's subway-shapes backdrop can bring
 * that route's path to full opacity while dimming the rest (a rider reading "the 2
 * train skips Jackson Av" can then see the 2 train's actual route); clicking the same
 * chip again, or a different one, updates or clears it. The connection is one-way --
 * this banner drives the map, the map never drives the banner.
 */

// Alerts change far less often than train positions, so this polls on its own slower
// cadence instead of piggybacking on REFRESH_S (the map-layer poll/stream interval) --
// frequent enough to feel live without hammering the endpoint.
const ALERTS_POLL_MS = 60000;
const ALERTS_ENDPOINT = "/api/mta_subway_alerts?limit=50";

// Matches the fallback color subwayLayer() (map-layers.js) uses for a route_id it
// doesn't recognize, so a chip for an unmapped route (e.g. the Staten Island Railway,
// "SI") still reads consistently with how that train would render on the map.
const DEFAULT_ROUTE_COLOR = [244, 211, 94];

function routeChipColor(route) {
  return ROUTE_COLORS[route] || DEFAULT_ROUTE_COLOR;
}

// Cheap relative-luminance check so light chips (N/Q/R/W yellow, L grey) get dark
// text and dark chips (A/C/E blue) get light text, instead of hardcoding it per route.
function readableTextColor(rgb) {
  const [r, g, b] = rgb;
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return luminance > 0.6 ? "#10141a" : "#f5f7fa";
}

// "?" (alertItemHtml's fallback for a routeless alert record) doesn't correspond to any
// real route_id on the map, so it renders as a plain, non-interactive chip -- only real
// routes are click targets for the map highlight below.
function routeChipHtml(route) {
  const rgb = routeChipColor(route);
  const [r, g, b] = rgb;
  const textColor = readableTextColor(rgb);
  const style = `background:rgb(${r},${g},${b});color:${textColor}`;
  if (route === "?") {
    return `<span class="route-chip" style="${style}">${escapeHtml(route)}</span>`;
  }
  const activeClass = route === highlightedRoute ? " route-chip--active" : "";
  return `<span class="route-chip${activeClass}" data-route="${escapeHtml(
    route
  )}" role="button" tabindex="0" title="Highlight the ${escapeHtml(
    route
  )} train's route on the map" style="${style}">${escapeHtml(route)}</span>`;
}

// The one write site for the shared `highlightedRoute` (state.js): clicking the
// already-highlighted chip clears it (second click = revert, per the brief), clicking
// any other chip replaces it. Re-renders the alert list so the clicked chip's active
// styling updates immediately, then asks map-layers.js to rebuild the map layers --
// subwayShapesLayer()'s data hasn't changed, only the highlight, so without this call
// the map would stay stale until the next unrelated refresh.
function selectRoute(route) {
  highlightedRoute = highlightedRoute === route ? null : route;
  if (lastAlertsEnvelope) renderAlerts(lastAlertsEnvelope);
  if (typeof renderLayers === "function") renderLayers();
}

function handleRouteChipActivate(event) {
  const chip = event.target.closest(".route-chip[data-route]");
  if (!chip) return;
  selectRoute(chip.dataset.route);
}

function handleRouteChipKeydown(event) {
  if (event.key !== "Enter" && event.key !== " ") return;
  const chip = event.target.closest(".route-chip[data-route]");
  if (!chip) return;
  event.preventDefault();
  selectRoute(chip.dataset.route);
}

function alertItemHtml(alertRecord) {
  const routes = alertRecord.routes && alertRecord.routes.length ? alertRecord.routes : ["?"];
  const chips = routes.map(routeChipHtml).join("");
  return `<li class="alert-item">
    <div class="alert-routes">${chips}</div>
    <div class="alert-header">${escapeHtml(alertRecord.header)}</div>
    <div class="alert-meta">since ${hhmmss(alertRecord.active_from)}</div>
  </li>`;
}

// Most-recently-started first. Records with no active_from (shouldn't happen per the
// contract, but never trust upstream shape blindly) sort last rather than crash.
function sortedByMostRecent(records) {
  return [...records].sort((a, b) => {
    const started_a = a.active_from ? Date.parse(a.active_from) : -Infinity;
    const started_b = b.active_from ? Date.parse(b.active_from) : -Infinity;
    return started_b - started_a;
  });
}

function setAlertsCount(text, status) {
  const badge = el("alerts-count");
  if (!badge) return;
  badge.textContent = text;
  badge.dataset.status = status;
}

// "stale" still shows real, last-good data plus when it was good -- same rule every
// map layer follows (see setPill in status-panel.js) -- never a fabricated refresh.
function staleSuffix(envelope) {
  return envelope.status === "stale" ? ` (stale, last good ${hhmmss(envelope.fetched_at)})` : "";
}

function renderAlertsError(envelope) {
  setAlertsCount("error", "error");
  const list = el("alerts-list");
  list.innerHTML = "";
  list.hidden = true;
  const empty = el("alerts-empty");
  empty.hidden = false;
  empty.dataset.status = "error";
  const message = envelope.error ? envelope.error.message : "alerts feed unavailable";
  empty.textContent = `Service alerts unavailable: ${message}`;
}

function renderAlertsEmpty(envelope) {
  setAlertsCount(`0 active${staleSuffix(envelope)}`, envelope.status);
  const list = el("alerts-list");
  list.innerHTML = "";
  list.hidden = true;
  const empty = el("alerts-empty");
  empty.hidden = false;
  empty.dataset.status = envelope.status;
  empty.textContent = "No active alerts.";
}

function renderAlertsList(envelope) {
  const records = sortedByMostRecent(envelope.records);
  setAlertsCount(`${records.length} active${staleSuffix(envelope)}`, envelope.status);
  const list = el("alerts-list");
  list.innerHTML = records.map(alertItemHtml).join("");
  list.hidden = false;
  const empty = el("alerts-empty");
  empty.hidden = true;
}

// Kept so selectRoute() can re-render the list (to update a chip's active styling)
// without waiting for the next poll or refetching anything.
let lastAlertsEnvelope = null;

function renderAlerts(envelope) {
  lastAlertsEnvelope = envelope;
  if (envelope.status === "error") {
    renderAlertsError(envelope);
    return;
  }
  if (!envelope.records.length) {
    renderAlertsEmpty(envelope);
    return;
  }
  renderAlertsList(envelope);
}

async function fetchAlerts() {
  try {
    const response = await fetch(ALERTS_ENDPOINT, { headers: { accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    renderAlerts(payload);
  } catch (err) {
    // The API itself is unreachable, or returned something unparseable. Say so
    // honestly; never invent an alert or fall back to a previous render silently.
    renderAlerts({
      status: "error",
      fetched_at: null,
      records: [],
      error: { kind: "internal", message: `dashboard API unreachable: ${err.message}` },
    });
  }
}

// Builds the section once and inserts it as the first child of #panel, immediately
// before the existing "Layers" heading -- the same "rearrange #panel's existing DOM,
// don't assume markup that isn't there" approach collapseLegend() uses in
// status-panel.js. Guards against double-insertion if called more than once.
function buildAlertsSection() {
  if (el("alerts-details")) return;
  const panel = document.getElementById("panel");
  if (!panel) return;
  const layersHeading = Array.from(panel.querySelectorAll("h2")).find(
    (h) => h.textContent.trim() === "Layers"
  );

  const details = document.createElement("details");
  details.id = "alerts-details";
  details.className = "alerts-details";
  details.open = true;
  details.innerHTML = `
    <summary>
      ${icon("alert")} Service Alerts
      <span class="alerts-count" id="alerts-count" data-status="loading">loading…</span>
    </summary>
    <p class="alerts-empty" id="alerts-empty" data-status="loading" hidden></p>
    <ul class="alerts-list" id="alerts-list" hidden></ul>`;

  if (layersHeading) {
    panel.insertBefore(details, layersHeading);
  } else {
    panel.prepend(details);
  }

  // Event delegation on the (persistent) <ul>, not per-chip listeners: renderAlertsList
  // replaces the chips themselves via innerHTML on every poll/selectRoute() call, but
  // this list element is created once, right here, so one delegated listener covers
  // every chip that will ever be rendered into it.
  const list = el("alerts-list");
  list.addEventListener("click", handleRouteChipActivate);
  list.addEventListener("keydown", handleRouteChipKeydown);
}

// Single entry point for the boot sequence in app.js. Builds the DOM once, fetches
// immediately so the section has real content on first paint, then polls on its own
// cadence. Deliberately not self-invoking at module load -- app.js decides when in the
// boot order this runs.
function initAlertsBanner() {
  buildAlertsSection();
  fetchAlerts();
  setInterval(fetchAlerts, ALERTS_POLL_MS);
}
