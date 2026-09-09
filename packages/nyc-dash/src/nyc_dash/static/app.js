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

// Validated categorical/sequential steps from the dataviz palette (references/palette.md):
// status-critical for incident-style markers, and the blue sequential ramp (light->dark)
// for continuous magnitude (bike availability). Subway keeps official MTA route colors and
// the density heatmap keeps deck.gl's warm ramp -- both are already correct, not ad hoc.
const STATUS_CRITICAL = [208, 59, 59]; // #d03b3b
const SEQUENTIAL_BLUE_LIGHT = [183, 211, 246]; // step 150, #b7d3f6 -- near-empty
const SEQUENTIAL_BLUE_DARK = [16, 66, 129]; // step 650, #104281 -- near-full
const MUTED_INK = [137, 135, 129]; // #898781 -- "no data", never a point on the scale

// Restaurant grades read as status, not an arbitrary new hue: these are the exact
// --fresh/--stale/--error CSS variables from style.css, converted to RGB for deck.gl.
// Anything that isn't A/B/C (ungraded, pending, null) uses MUTED_INK, same as "no data"
// elsewhere on this map.
const GRADE_A = [46, 204, 113]; // --fresh #2ecc71
const GRADE_B = [244, 162, 89]; // --stale #f4a259
const GRADE_C = [239, 71, 111]; // --error #ef476f

function gradeColor(grade) {
  if (grade === "A") return GRADE_A;
  if (grade === "B") return GRADE_B;
  if (grade === "C") return GRADE_C;
  return MUTED_INK;
}

function lerpColor(from, to, t) {
  const c = Math.max(0, Math.min(1, t));
  return [
    Math.round(from[0] + (to[0] - from[0]) * c),
    Math.round(from[1] + (to[1] - from[1]) * c),
    Math.round(from[2] + (to[2] - from[2]) * c),
  ];
}

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
  {
    key: "dohmh_inspections",
    label: "Restaurant inspections",
    query: "limit=1000",
    defaultVisible: false, // dense data; opt-in like DOT cameras
    build: inspectionsLayer,
    count: (env) => `${env.records.length} inspections`,
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

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

// Upstream text (restaurant names, 311 descriptors, camera names, GTFS stop names) is
// never trusted as markup: every detail panel and tooltip runs interpolated values
// through this before landing in innerHTML.
function escapeHtml(value) {
  if (value == null) return "";
  return String(value).replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
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

// Plain-language summary for the ErrorKinds whose raw message tends to carry upstream
// jargon (dataset ids, SQL-shaped filters, raw HTTP bodies) -- the raw message moves to
// the element's title instead (still "surfaced in the UI", just not the primary text).
// NOT_CONFIGURED and INTERNAL are deliberately absent: their messages in this codebase
// are already written as plain English (e.g. density's "run `just vision run`" hint),
// so summarizing them would throw away real information for no readability gain.
const ERROR_KIND_SUMMARY = {
  upstream_http: "upstream rejected the request",
  upstream_timeout: "upstream is slow to respond",
  upstream_parse: "upstream data looks wrong right now",
  rate_limited: "rate-limited by the upstream",
  not_found: "upstream endpoint not found",
};

function setPill(key, envelope) {
  const pill = el(`pill-${key}`);
  const detail = el(`detail-${key}`);
  if (!pill) return;
  const feed = FEEDS.find((f) => f.key === key);
  pill.dataset.status = envelope.status;
  detail.dataset.status = envelope.status;
  if (envelope.status === "error") {
    pill.textContent = "error";
    const err = envelope.error;
    const summary = err && ERROR_KIND_SUMMARY[err.kind];
    detail.textContent = summary || (err ? err.message : "feed unavailable");
    detail.title = summary && err ? err.message : "";
    return;
  }
  const counted = feed && feed.count ? feed.count(envelope) : `${envelope.records.length} records`;
  if (envelope.status === "stale") {
    pill.textContent = "stale";
    const err = envelope.error;
    const summary = err && ERROR_KIND_SUMMARY[err.kind];
    const why = err ? ` · ${summary || err.message}` : "";
    detail.textContent = `last good ${hhmmss(envelope.fetched_at)} · ${counted}${why}`;
    detail.title = summary && err ? err.message : "";
    return;
  }
  detail.title = "";
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
    getFillColor: [...STATUS_CRITICAL, 200],
    getLineColor: [255, 255, 255, 120],
    lineWidthMinPixels: 1,
    stroked: true,
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
    getRadius: (d) => 26 + 3 * Math.sqrt(d.capacity || d.bikes_available + d.docks_available || 1),
    radiusMinPixels: 2,
    radiusMaxPixels: 14,
    getFillColor: (d) => {
      const total = d.capacity || d.bikes_available + d.docks_available;
      if (!total) return [...MUTED_INK, 160];
      const ratio = d.bikes_available / total;
      return [...lerpColor(SEQUENTIAL_BLUE_LIGHT, SEQUENTIAL_BLUE_DARK, ratio), 190];
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

function inspectionsLayer(envelope) {
  const data = located(envelope.records);
  if (!data.length) return null;
  return new deck.ScatterplotLayer({
    id: "dohmh",
    data,
    pickable: true,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: 30,
    radiusMinPixels: 2,
    radiusMaxPixels: 8,
    getFillColor: (d) => [...gradeColor(d.grade), 200],
    getLineColor: [255, 255, 255, 100],
    lineWidthMinPixels: 1,
    stroked: true,
    updateTriggers: { getFillColor: envelope.fetched_at },
  });
}

function renderLayersNow() {
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

let renderScheduled = false;

// On first load, up to 5 feed fetches resolve within milliseconds of each other; without
// coalescing, each would trigger its own full layer rebuild (rebuilding data for every
// other already-loaded layer too) and re-upload to the GPU, competing with the basemap
// for the first paint. One rAF per burst keeps startup to a single layer rebuild.
function renderLayers() {
  if (renderScheduled) return;
  renderScheduled = true;
  requestAnimationFrame(() => {
    renderScheduled = false;
    renderLayersNow();
  });
}

function tooltip({ object, layer }) {
  if (!object) return null;
  if (layer.id === "subway") {
    return {
      html: `<b>${escapeHtml(object.route_id)}</b> to ${escapeHtml(
        object.stop_name || object.stop_id
      )}<br/>in ${Math.round(object.eta_s / 60)} min · click for the full stop list`,
    };
  }
  if (layer.id === "density") {
    return {
      html: `<b>${escapeHtml(object.name || object.camera_id)}</b><br/>people ${object.person_mean.toFixed(
        1
      )} · vehicles ${object.vehicle_mean.toFixed(1)}<br/>${object.sample_count} frames`,
    };
  }
  if (layer.id === "nyc311") {
    return {
      html: `<b>${escapeHtml(object.complaint_type)}</b><br/>${escapeHtml(
        object.descriptor || ""
      )}<br/>${escapeHtml(object.agency)} · ${escapeHtml(object.status || "")} · click for details`,
    };
  }
  if (layer.id === "citibike") {
    return {
      html: `<b>${escapeHtml(object.name)}</b><br/>${object.bikes_available} bikes · ${
        object.docks_available
      } docks · click for details`,
    };
  }
  if (layer.id === "cameras") {
    return {
      html: `<b>${escapeHtml(object.name)}</b><br/>${
        object.is_online ? "online" : "offline"
      } · click for live view`,
    };
  }
  if (layer.id === "dohmh") {
    return {
      html: `<b>${escapeHtml(object.dba || "unnamed")}</b><br/>grade ${escapeHtml(
        object.grade || "ungraded"
      )} · click for details`,
    };
  }
  return null;
}

// --------------------------------------------------------------------------- click detail panel
//
// One persistent panel, reused by every clickable layer (subway, 311, Citi Bike, DOT
// cameras, restaurant inspections) instead of four bespoke UIs. `openDetailPanel` owns
// the panel's DOM; each layer's `*Detail` function only supplies a title and a function
// that fills in the body. That body-builder may return a cleanup function (clearing a
// `setInterval`, cancelling a fetch) which runs when the panel is closed or replaced.

let panelCleanup = null;

function closeDetailPanel() {
  if (panelCleanup) {
    panelCleanup();
    panelCleanup = null;
  }
  const panel = el("detail-panel");
  panel.hidden = true;
  panel.innerHTML = "";
}

function openDetailPanel(title, buildBody) {
  closeDetailPanel(); // also clears any previous camera refresh / in-flight fetch
  const panel = el("detail-panel");
  panel.hidden = false;
  panel.innerHTML = `
    <div class="detail-panel-head">
      <span class="detail-panel-title">${title}</span>
      <button type="button" class="detail-panel-close" id="detail-panel-close" aria-label="Close">×</button>
    </div>
    <div class="detail-panel-body" id="detail-panel-body"></div>`;
  el("detail-panel-close").addEventListener("click", closeDetailPanel);
  panelCleanup = buildBody(el("detail-panel-body")) || null;
}

function fieldsHtml(pairs) {
  return `<dl class="detail-fields">${pairs
    .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${value}</dd>`)
    .join("")}</dl>`;
}

function cameraDetail(camera) {
  openDetailPanel(escapeHtml(camera.name), (body) => {
    body.innerHTML =
      fieldsHtml([
        ["Status", camera.is_online ? "online" : "offline"],
        ["Roadway", escapeHtml(camera.roadway || "—")],
        ["Direction", escapeHtml(camera.direction || "—")],
        ["Area", escapeHtml(camera.area || "—")],
      ]) +
      `<p class="detail-subhead">Live view</p>
       <div class="camera-live">
         <img id="camera-live-img" alt="Live view of ${escapeHtml(camera.name)}" hidden />
         <p class="camera-live-error" id="camera-live-error" hidden>
           Live image is unavailable right now.
         </p>
       </div>`;
    const img = body.querySelector("#camera-live-img");
    const errNode = body.querySelector("#camera-live-error");
    img.onerror = () => {
      img.hidden = true;
      errNode.hidden = false;
    };
    const refresh = () => {
      img.hidden = false;
      errNode.hidden = true;
      img.src = `${camera.image_url}?_ts=${Date.now()}`;
    };
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  });
}

function inspectionDetail(record) {
  openDetailPanel(escapeHtml(record.dba || "Unnamed restaurant"), (body) => {
    body.innerHTML = fieldsHtml([
      ["Cuisine", escapeHtml(record.cuisine || "—")],
      ["Grade", escapeHtml(record.grade || "ungraded")],
      ["Score", record.score != null ? record.score : "—"],
      ["Inspected", record.inspection_date ? hhmmss(record.inspection_date) : "—"],
      ["Latest violation", escapeHtml(record.violation_description || "none recorded")],
    ]);
  });
}

function service311Detail(record) {
  openDetailPanel(escapeHtml(record.complaint_type || "311 request"), (body) => {
    body.innerHTML = fieldsHtml([
      ["Descriptor", escapeHtml(record.descriptor || "—")],
      ["Agency", escapeHtml(record.agency || "—")],
      ["Status", escapeHtml(record.status || "—")],
      ["Borough", escapeHtml(record.borough || "—")],
      ["Address", escapeHtml(record.incident_address || "—")],
      ["Created", record.created_at ? hhmmss(record.created_at) : "—"],
    ]);
  });
}

function bikeDetail(record) {
  openDetailPanel(escapeHtml(record.name), (body) => {
    const bikes =
      record.ebikes_available != null
        ? `${record.bikes_available} (${record.ebikes_available} e-bikes)`
        : `${record.bikes_available}`;
    body.innerHTML = fieldsHtml([
      ["Bikes", bikes],
      ["Docks", record.docks_available],
      ["Capacity", record.capacity != null ? record.capacity : "—"],
      ["Renting", record.is_renting ? "yes" : "no"],
      ["Returning", record.is_returning ? "yes" : "no"],
      ["Last reported", record.last_reported ? hhmmss(record.last_reported) : "—"],
    ]);
  });
}

// Raw trip data (with the full stop_times list) and the static stop names are each
// fetched at most once per page load and cached here, no matter how many trains get
// clicked -- exactly the two endpoints the task already fetches elsewhere in spirit
// (mta_subway, mta_subway_stops), just lazily, since nothing else on this page needed
// them yet.
let subwayTripsCache = null;
let subwayStopsCache = null;

function loadSubwayTrips() {
  if (!subwayTripsCache) {
    subwayTripsCache = fetch("/api/mta_subway?limit=800", { headers: { accept: "application/json" } })
      .then((r) => r.json())
      .then((env) => {
        const byTripId = new Map();
        for (const trip of env.records || []) byTripId.set(trip.trip_id, trip);
        return byTripId;
      })
      .catch((err) => {
        subwayTripsCache = null; // let the next click retry instead of caching a failure
        throw err;
      });
  }
  return subwayTripsCache;
}

function loadSubwayStops() {
  if (!subwayStopsCache) {
    subwayStopsCache = fetch("/api/mta_subway_stops?limit=1500", {
      headers: { accept: "application/json" },
    })
      .then((r) => r.json())
      .then((env) => {
        const byStopId = new Map();
        for (const stop of env.records || []) byStopId.set(stop.stop_id, stop);
        return byStopId;
      })
      .catch((err) => {
        subwayStopsCache = null;
        throw err;
      });
  }
  return subwayStopsCache;
}

function subwayDetail(record) {
  openDetailPanel(`${escapeHtml(record.route_id)} train`, (body) => {
    body.innerHTML =
      fieldsHtml([
        ["Trip", escapeHtml(record.trip_id)],
        ["Direction", escapeHtml(record.direction || "—")],
        [
          "Next stop",
          `${escapeHtml(record.stop_name || record.stop_id)} in ${Math.round(record.eta_s / 60)} min`,
        ],
      ]) +
      `<p class="detail-subhead">Full stop list</p>
       <div id="subway-stop-list" class="detail-loading">loading full stop list…</div>`;
    const listNode = body.querySelector("#subway-stop-list");
    let cancelled = false;
    Promise.all([loadSubwayTrips(), loadSubwayStops()])
      .then(([trips, stops]) => {
        if (cancelled) return;
        const trip = trips.get(record.trip_id);
        if (!trip || !trip.stop_times.length) {
          listNode.textContent = trip
            ? "No stop times reported for this trip."
            : "This train's raw trip data is no longer available (it may have completed its run).";
          return;
        }
        listNode.innerHTML = `<ul class="detail-stop-list">${trip.stop_times
          .map((st) => {
            const stop = stops.get(st.stop_id);
            const name = stop ? stop.name : st.stop_id;
            const when = st.arrival ? hhmmss(st.arrival) : "—";
            return `<li><span>${escapeHtml(name)}</span><span>${when}</span></li>`;
          })
          .join("")}</ul>`;
      })
      .catch((err) => {
        if (!cancelled) listNode.textContent = `Could not load the full stop list: ${err.message}`;
      });
    return () => {
      cancelled = true;
    };
  });
}

const DETAIL_BUILDERS = {
  subway: subwayDetail,
  nyc311: service311Detail,
  citibike: bikeDetail,
  cameras: cameraDetail,
  dohmh: inspectionDetail,
};

function handleMapClick(info) {
  if (!info || !info.object || !info.layer) return;
  const builder = DETAIL_BUILDERS[info.layer.id];
  if (builder) builder(info.object);
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
