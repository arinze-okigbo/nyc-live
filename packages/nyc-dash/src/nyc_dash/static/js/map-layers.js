/*
 * Map markers: colors, per-feed deck.gl layer builders, hover tooltips, and the
 * feed -> layer registry (FEEDS) that drives both the map and the sidebar panel.
 *
 * Depends on: utils.js (escapeHtml), state.js (overlay, state). Loads after both.
 */

// Validated categorical/sequential steps from the dataviz palette (references/palette.md):
// status-critical for incident-style markers, and the blue sequential ramp (light->dark)
// for continuous magnitude (bike availability). Subway keeps official MTA route colors and
// the density heatmap keeps deck.gl's warm ramp -- both are already correct, not ad hoc.
const STATUS_CRITICAL = [208, 59, 59]; // #d03b3b
const SEQUENTIAL_BLUE_LIGHT = [183, 211, 246]; // step 150, #b7d3f6 -- near-empty
const SEQUENTIAL_BLUE_DARK = [16, 66, 129]; // step 650, #104281 -- near-full
const MUTED_INK = [137, 135, 129]; // #898781 -- "no data", never a point on the scale

// Restaurant grades read as status, not an arbitrary new hue: these are the exact
// --fresh/--stale/--error CSS variables from tokens.css, converted to RGB for deck.gl.
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
