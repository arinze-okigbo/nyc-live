/*
 * nyc-live dashboard.
 *
 * Data flow: one GET per feed on load (fast first data), then Server-Sent Events
 * on /api/stream for updates without a page refresh. If EventSource is missing or
 * the stream errors, we close it and fall back to polling the same /api/<feed>
 * endpoints on the same cadence; the connection pill says which mode is active.
 *
 * Degradation rules (the whole point of this page):
 *   - every layer's pill is driven by envelope.status and nothing else;
 *   - "stale" keeps the last good records on the map and shows when they were good;
 *   - "error" hides that layer and shows envelope.error.message;
 *   - a layer with no data is absent, never a placeholder shape or a fake value;
 *   - one feed failing changes nothing about any other layer.
 *
 * MapLibre GL and deck.gl come from pinned CDN URLs (see index.html). If they fail
 * to load, the banner says so and the feed pills keep working without a map.
 */

const REFRESH_S = 15;
const NYC = { longitude: -73.9855, latitude: 40.7484, zoom: 11.2, pitch: 0, bearing: 0 };
const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

const ROUTE_COLORS = {
  "1": [238, 53, 46], "2": [238, 53, 46], "3": [238, 53, 46],
  "4": [0, 147, 60], "5": [0, 147, 60], "6": [0, 147, 60],
  "7": [185, 51, 173],
  A: [0, 57, 166], C: [0, 57, 166], E: [0, 57, 166],
  B: [255, 99, 25], D: [255, 99, 25], F: [255, 99, 25], M: [255, 99, 25],
  G: [108, 190, 69],
  J: [153, 102, 51], Z: [153, 102, 51],
  L: [167, 169, 172],
  N: [252, 204, 10], Q: [252, 204, 10], R: [252, 204, 10], W: [252, 204, 10],
  S: [128, 129, 131],
};

const FEEDS = [
  {
    key: "subway_arrivals",
    label: "Subway (next stop)",
    query: "limit=800&horizon_s=1200",
    defaultVisible: true,
    build: subwayLayer,
    count: (env) => `${new Set(env.records.map((r) => r.trip_id)).size} trains`,
  },
  {
    key: "density",
    label: "Camera density",
    query: "limit=500&window_s=900",
    defaultVisible: true,
    build: densityLayer,
    count: (env) => `${env.records.length} cameras`,
  },
  {
    key: "nyc_311",
    label: "311 requests",
    query: "limit=1000",
    defaultVisible: true,
    build: layer311,
    count: (env) => `${env.records.length} requests`,
  },
  {
    key: "citibike",
    label: "Citi Bike",
    query: "limit=2500",
    defaultVisible: true,
    build: bikeLayer,
    count: (env) => `${env.records.length} stations`,
  },
  {
    key: "dot_cameras",
    label: "DOT cameras",
    query: "limit=2000",
    defaultVisible: false,
    build: cameraLayer,
    count: (env) => `${env.records.length} cameras`,
  },
];

const WEATHER_KEY = "weather";
const STREAM_KEYS = FEEDS.map((f) => f.key).concat([WEATHER_KEY]);

const state = new Map(); // key -> {envelope, visible}
let overlay = null;
let map = null;
let source = null; // EventSource
let pollTimer = null;

// --------------------------------------------------------------------------- utils

function el(id) {
  return document.getElementById(id);
}

function hhmmss(iso) {
  if (!iso) return "unknown time";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}

function banner(message) {
  const node = el("banner");
  node.textContent = message;
  node.hidden = false;
}

function setConnection(mode, text) {
  const node = el("connection-badge");
  node.dataset.mode = mode;
  node.textContent = text;
}

// --------------------------------------------------------------------------- panel

function buildPanel() {
  const list = el("layers");
  for (const feed of FEEDS) {
    const li = document.createElement("li");
    li.id = `layer-${feed.key}`;
    li.innerHTML = `
      <div class="layer-head">
        <input type="checkbox" id="toggle-${feed.key}" ${feed.defaultVisible ? "checked" : ""} />
        <label for="toggle-${feed.key}">${feed.label}</label>
        <span class="pill" id="pill-${feed.key}" data-status="loading">loading…</span>
      </div>
      <div class="detail" id="detail-${feed.key}"></div>`;
    list.appendChild(li);
    state.set(feed.key, { envelope: null, visible: feed.defaultVisible });
    el(`toggle-${feed.key}`).addEventListener("change", (ev) => {
      state.get(feed.key).visible = ev.target.checked;
      renderLayers();
    });
  }
}

function setPill(key, envelope) {
  const pill = el(`pill-${key}`);
  const detail = el(`detail-${key}`);
  if (!pill) return;
  const feed = FEEDS.find((f) => f.key === key);
  pill.dataset.status = envelope.status;
  detail.dataset.status = envelope.status;
  if (envelope.status === "error") {
    pill.textContent = "error";
    detail.textContent = envelope.error ? envelope.error.message : "feed unavailable";
    return;
  }
  const counted = feed && feed.count ? feed.count(envelope) : `${envelope.records.length} records`;
  if (envelope.status === "stale") {
    pill.textContent = "stale";
    detail.textContent =
      `last good ${hhmmss(envelope.fetched_at)} · ${counted}` +
      (envelope.error ? ` · ${envelope.error.message}` : "");
    return;
  }
  pill.textContent = "fresh";
  detail.textContent = `${counted} · ${hhmmss(envelope.fetched_at)}`;
}

function applyHealth(payload) {
  const counts = { fresh: 0, stale: 0, error: 0, never_fetched: 0 };
  for (const feed of payload.feeds || []) {
    counts[feed.status] = (counts[feed.status] || 0) + 1;
  }
  const badge = el("health-badge");
  const bad = counts.error;
  badge.dataset.status = bad ? (counts.fresh ? "stale" : "error") : "fresh";
  badge.textContent = `feeds: ${counts.fresh} fresh · ${counts.stale} stale · ${bad} error`;
  badge.title = (payload.feeds || [])
    .map((f) => `${f.feed}: ${f.status}${f.last_error ? ` (${f.last_error.message})` : ""}`)
    .join("\n");
}

function applyWeather(envelope) {
  const badge = el("weather-badge");
  badge.dataset.status = envelope.status;
  if (envelope.status === "error") {
    badge.textContent = "weather unavailable";
    badge.title = envelope.error ? envelope.error.message : "";
    return;
  }
  const report = envelope.records[0];
  if (!report) {
    badge.textContent = "weather: no station reported";
    badge.title = "";
    return;
  }
  const obs = report.observation || {};
  const temp = obs.temperature_c == null ? "—" : `${Math.round(obs.temperature_c)}°C`;
  const text = obs.text ? ` ${obs.text}` : "";
  const suffix =
    envelope.status === "stale" ? ` · last good ${hhmmss(envelope.fetched_at)}` : "";
  badge.textContent = `${temp}${text} · ${report.station_name}${suffix}`;
  badge.title =
    `observed ${hhmmss(obs.observed_at)}` +
    (envelope.error ? `\n${envelope.error.message}` : "");
}

// --------------------------------------------------------------------------- layers

function located(records) {
  return records.filter((r) => r.lat != null && r.lon != null);
}

function subwayLayer(envelope) {
  // One dot per train, at the stop it is next due at (coordinates come from the
  // static stops feed). Nothing is interpolated between stations.
  const best = new Map();
  for (const rec of located(envelope.records)) {
    const current = best.get(rec.trip_id);
    if (!current || rec.eta_s < current.eta_s) best.set(rec.trip_id, rec);
  }
  const data = Array.from(best.values());
  if (!data.length) return null;
  return new deck.ScatterplotLayer({
    id: "subway",
    data,
    pickable: true,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: 70,
    radiusMinPixels: 3,
    radiusMaxPixels: 12,
    getFillColor: (d) => ROUTE_COLORS[d.route_id] || [244, 211, 94],
    getLineColor: [10, 12, 16],
    lineWidthMinPixels: 1,
    stroked: true,
    updateTriggers: { getFillColor: envelope.fetched_at },
  });
}

function densityLayer(envelope) {
  const data = located(envelope.records);
  if (!data.length) return null;
  return new deck.HeatmapLayer({
    id: "density",
    data,
    getPosition: (d) => [d.lon, d.lat],
    getWeight: (d) => (d.person_mean || 0) + (d.vehicle_mean || 0),
    radiusPixels: 55,
    intensity: 1,
    threshold: 0.05,
    aggregation: "SUM",
  });
}

function layer311(envelope) {
  const data = located(envelope.records);
  if (!data.length) return null;
  return new deck.ScatterplotLayer({
    id: "nyc311",
    data,
    pickable: true,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: 40,
    radiusMinPixels: 2,
    radiusMaxPixels: 8,
    getFillColor: [239, 71, 111, 190],
  });
}

function bikeLayer(envelope) {
  const data = located(envelope.records);
  if (!data.length) return null;
  return new deck.ScatterplotLayer({
    id: "citibike",
    data,
    pickable: true,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: (d) => 30 + 3 * Math.sqrt(d.capacity || d.bikes_available + d.docks_available || 1),
    radiusMinPixels: 2,
    radiusMaxPixels: 14,
    getFillColor: (d) => {
      const total = d.capacity || d.bikes_available + d.docks_available;
      if (!total) return [141, 153, 174, 200];
      const ratio = Math.max(0, Math.min(1, d.bikes_available / total));
      return [17 + 60 * (1 - ratio), 138 * ratio + 60, 178 * ratio + 40, 210];
    },
  });
}

function cameraLayer(envelope) {
  const data = located(envelope.records);
  if (!data.length) return null;
  return new deck.ScatterplotLayer({
    id: "cameras",
    data,
    pickable: true,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: 25,
    radiusMinPixels: 2,
    radiusMaxPixels: 6,
    getFillColor: (d) => (d.is_online ? [141, 153, 174, 200] : [90, 96, 110, 140]),
  });
}

function renderLayers() {
  if (!overlay) return;
  const layers = [];
  for (const feed of FEEDS) {
    const entry = state.get(feed.key);
    // status === "error" means there is no usable data: the layer is not drawn.
    if (!entry || !entry.visible || !entry.envelope) continue;
    if (entry.envelope.status === "error") continue;
    const layer = feed.build(entry.envelope);
    if (layer) layers.push(layer);
  }
  overlay.setProps({ layers });
}

function tooltip({ object, layer }) {
  if (!object) return null;
  if (layer.id === "subway") {
    return {
      html: `<b>${object.route_id}</b> to ${object.stop_name || object.stop_id}<br/>in ${Math.round(
        object.eta_s / 60
      )} min`,
    };
  }
  if (layer.id === "density") {
    return {
      html: `<b>${object.name || object.camera_id}</b><br/>people ${object.person_mean.toFixed(
        1
      )} · vehicles ${object.vehicle_mean.toFixed(1)}<br/>${object.sample_count} frames`,
    };
  }
  if (layer.id === "nyc311") {
    return {
      html: `<b>${object.complaint_type}</b><br/>${object.descriptor || ""}<br/>${
        object.agency
      } · ${object.status || ""}`,
    };
  }
  if (layer.id === "citibike") {
    return {
      html: `<b>${object.name}</b><br/>${object.bikes_available} bikes · ${object.docks_available} docks`,
    };
  }
  if (layer.id === "cameras") {
    return { html: `<b>${object.name}</b><br/>${object.is_online ? "online" : "offline"}` };
  }
  return null;
}

// --------------------------------------------------------------------------- data

function applyEnvelope(key, envelope) {
  if (key === WEATHER_KEY) {
    applyWeather(envelope);
    el("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
    return;
  }
  const entry = state.get(key);
  if (!entry) return;
  entry.envelope = envelope;
  setPill(key, envelope);
  renderLayers();
  el("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
}

function feedUrl(key) {
  const feed = FEEDS.find((f) => f.key === key);
  return feed && feed.query ? `/api/${key}?${feed.query}` : `/api/${key}?limit=10`;
}

async function fetchFeed(key) {
  try {
    const response = await fetch(feedUrl(key), { headers: { accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    applyEnvelope(key, payload);
  } catch (err) {
    // The API itself is unreachable. Say so on that layer; do not invent records.
    applyEnvelope(key, {
      feed: key,
      status: "error",
      fetched_at: null,
      stale_after: null,
      records: [],
      error: { kind: "internal", message: `dashboard API unreachable: ${err.message}` },
    });
  }
}

async function refreshAll() {
  await Promise.all(STREAM_KEYS.map(fetchFeed));
  fetchHealth();
}

async function fetchHealth() {
  try {
    const response = await fetch("/api/health");
    applyHealth(await response.json());
  } catch (err) {
    const badge = el("health-badge");
    badge.dataset.status = "error";
    badge.textContent = "feeds: health unavailable";
    badge.title = err.message;
  }
}

function startPolling(reason) {
  if (pollTimer) return;
  setConnection("polling", `polling every ${REFRESH_S}s`);
  el("connection-badge").title = reason;
  pollTimer = setInterval(refreshAll, REFRESH_S * 1000);
}

function connectStream() {
  if (!("EventSource" in window)) {
    startPolling("EventSource is not supported by this browser");
    return;
  }
  const url = `/api/stream?feeds=${STREAM_KEYS.join(",")}&interval_s=${REFRESH_S}`;
  source = new EventSource(url);
  source.addEventListener("ready", () => setConnection("live", "live (SSE)"));
  source.addEventListener("health", (ev) => applyHealth(JSON.parse(ev.data)));
  for (const key of STREAM_KEYS) {
    source.addEventListener(key, (ev) => applyEnvelope(key, JSON.parse(ev.data)));
  }
  source.onerror = () => {
    source.close();
    source = null;
    startPolling("the SSE stream closed; falling back to polling");
  };
}

// --------------------------------------------------------------------------- boot

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
  map.on("error", (ev) => {
    if (ev && ev.error && /style|tile/i.test(String(ev.error.message || ""))) {
      banner(`Basemap tiles unavailable (${BASEMAP_STYLE}); data layers still update.`);
    }
  });
  overlay = new deck.MapboxOverlay({ interleaved: false, layers: [], getTooltip: tooltip });
  map.addControl(overlay);
  return true;
}

function start() {
  buildPanel();
  initMap();
  setConnection("connecting", "connecting…");
  refreshAll().then(connectStream);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
