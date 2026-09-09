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

// Hover feedback and update-animation tuning. HOVER_HIGHLIGHT reuses plain white (already
// on the palette as the stroke color below) blended in by deck.gl's autoHighlight, so
// hovering a marker brightens it instead of introducing a new hue. UPDATE_TRANSITION_MS
// is how long a marker takes to ease into a new value on refresh, applied only where the
// underlying quantity itself moves continuously (e.g. bike-fill ratio) -- never on data
// that is supposed to jump instantly (a train's route, a letter grade), where an eased
// blend would just show a muddy in-between color for no benefit.
const HOVER_HIGHLIGHT = [255, 255, 255, 100];
const UPDATE_TRANSITION_MS = 600;

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

// Case-insensitive match against the shared `selectedBorough` global (state.js) --
// "all" (the default) always matches. See BOROUGHS' comment in state.js for why this
// can't just be `===`: the three feeds that carry a borough field don't agree on case.
function inBorough(value) {
  if (selectedBorough === "all") return true;
  return typeof value === "string" && value.toUpperCase() === selectedBorough.toUpperCase();
}

// Applied by the three layer builders below whose record types actually carry a clean
// borough field (cameraLayer/area, layer311/borough, inspectionsLayer/boro) and by
// their sidebar counts (boroughCountLabel), so the markers drawn on the map and the
// count text next to their checkbox never disagree about what "selected" means. Every
// other layer (subway, density, Citi Bike, buses) ignores selectedBorough entirely --
// they don't have a comparable borough field, so forcing one on would be a fabrication.
function boroughFiltered(records, field) {
  return selectedBorough === "all" ? records : records.filter((r) => inBorough(r[field]));
}

// Sidebar count text (FEEDS[].count) for the three borough-filterable layers: once a
// borough is selected this shows "<drawn> of <total> <noun> · <borough>" instead of the
// unfiltered citywide total, so the number next to the checkbox always matches what's
// actually on the map -- never a stale count left over from before the filter changed.
function boroughCountLabel(records, field, noun) {
  const total = records.length;
  if (selectedBorough === "all") return `${total} ${noun}`;
  return `${boroughFiltered(records, field).length} of ${total} ${noun} · ${selectedBorough}`;
}

// Bus route ids from MTA Bus Time (BusVehicle.route_id) are agency-qualified, e.g.
// "MTA NYCT_Q30" -- strip the agency prefix for the short route code that's actually
// painted on the bus and shown to riders. Used for the marker color lookup below,
// the hover tooltip, and the detail panel title.
function busRouteLabel(routeId) {
  if (!routeId) return "?";
  const idx = routeId.indexOf("_");
  return idx === -1 ? routeId : routeId.slice(idx + 1);
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
    autoHighlight: true,
    highlightColor: HOVER_HIGHLIGHT,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: 70,
    radiusMinPixels: 3,
    radiusMaxPixels: 12,
    // Route color is a categorical jump (this train's next stop can put it on a
    // different line entirely), not a value that eases -- no transition here.
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
    // Not pickable: this is the one purely-informational layer, so it should read as
    // background context rather than compete with the clickable marker layers above it.
    // A touch of extra transparency is enough to make that hierarchy legible at a glance.
    opacity: 0.75,
    getPosition: (d) => [d.lon, d.lat],
    getWeight: (d) => (d.person_mean || 0) + (d.vehicle_mean || 0),
    radiusPixels: 55,
    intensity: 1,
    threshold: 0.05,
    aggregation: "SUM",
  });
}

function layer311(envelope) {
  const data = boroughFiltered(located(envelope.records), "borough");
  if (!data.length) return null;
  return new deck.ScatterplotLayer({
    id: "nyc311",
    data,
    pickable: true,
    autoHighlight: true,
    highlightColor: HOVER_HIGHLIGHT,
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

// Below this zoom, all 2500 Citi Bike stations packed into a city-wide view read as
// noise more than signal -- shrink dots further and fade out near-empty stations so the
// stations that actually have bikes to offer stand out. Both knobs relax back to the
// normal look by BIKE_DECLUTTER_ZOOM, so nothing looks thinned-out once zoomed in.
const BIKE_DECLUTTER_ZOOM = 13;

function bikeLegibility(zoom) {
  const t = Math.max(0, Math.min(1, (zoom - NYC.zoom) / (BIKE_DECLUTTER_ZOOM - NYC.zoom)));
  return {
    // 1.5px min at city-wide zoom, easing up to the normal 2px once zoomed past the
    // declutter threshold.
    radiusMinPixels: 1.5 + 0.5 * t,
    // Near-empty stations (little to no supply) are the least actionable dots on the
    // map; fading them at low zoom lets full/interesting stations read through the
    // clutter without hiding any station outright (still visible, just quieter).
    emptyStationAlpha: Math.round(70 + 90 * t),
  };
}

function bikeLayer(envelope, zoom) {
  const data = located(envelope.records);
  if (!data.length) return null;
  const { radiusMinPixels, emptyStationAlpha } = bikeLegibility(zoom);
  return new deck.ScatterplotLayer({
    id: "citibike",
    data,
    pickable: true,
    autoHighlight: true,
    highlightColor: HOVER_HIGHLIGHT,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: (d) => 26 + 3 * Math.sqrt(d.capacity || d.bikes_available + d.docks_available || 1),
    radiusMinPixels,
    radiusMaxPixels: 14,
    getFillColor: (d) => {
      const total = d.capacity || d.bikes_available + d.docks_available;
      if (!total) return [...MUTED_INK, 160];
      const ratio = d.bikes_available / total;
      const color = lerpColor(SEQUENTIAL_BLUE_LIGHT, SEQUENTIAL_BLUE_DARK, ratio);
      // Near-zero supply is real information (worth knowing this station is out), so it
      // is faded, never hidden -- just deprioritized visually at low zoom.
      const alpha = ratio < 0.05 ? emptyStationAlpha : 190;
      return [...color, alpha];
    },
    getLineColor: [255, 255, 255, 140],
    lineWidthMinPixels: 1,
    stroked: true,
    // Bike-fill ratio and station size both move continuously between refreshes (a few
    // bikes checked in or out), so easing them reads as "the count updated" rather than
    // a flicker -- unlike the categorical jumps on subway/grade colors below.
    transitions: {
      getFillColor: UPDATE_TRANSITION_MS,
      getRadius: UPDATE_TRANSITION_MS,
    },
    // getFillColor's alpha term depends on zoom (via emptyStationAlpha) as well as the
    // envelope, independent of either alone -- both need to be in the trigger key or a
    // refresh with no zoom change (or vice versa) would leave stale colors on the GPU.
    // radiusMinPixels is a plain (non-accessor) prop, so ordinary prop diffing on the
    // freshly-constructed layer already picks up its change; it needs no trigger here.
    updateTriggers: {
      getFillColor: [envelope.fetched_at, zoom],
    },
  });
}

function cameraLayer(envelope) {
  const data = boroughFiltered(located(envelope.records), "area");
  if (!data.length) return null;
  return new deck.ScatterplotLayer({
    id: "cameras",
    data,
    pickable: true,
    autoHighlight: true,
    highlightColor: HOVER_HIGHLIGHT,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: 25,
    radiusMinPixels: 2,
    radiusMaxPixels: 6,
    getLineColor: [255, 255, 255, 120],
    lineWidthMinPixels: 1,
    stroked: true,
    // Online/offline only ever fades between two closely related grays, so easing this
    // (a camera coming back up) reads as a status change settling in, not a muddy blend.
    getFillColor: (d) => (d.is_online ? [141, 153, 174, 200] : [90, 96, 110, 140]),
    transitions: { getFillColor: UPDATE_TRANSITION_MS },
  });
}

function busLayer(envelope) {
  const data = located(envelope.records);
  if (!data.length) return null;
  return new deck.ScatterplotLayer({
    id: "bus",
    data,
    pickable: true,
    autoHighlight: true,
    highlightColor: HOVER_HIGHLIGHT,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: 45,
    radiusMinPixels: 2,
    radiusMaxPixels: 8,
    // Same categorical-jump reasoning as subwayLayer above (a bus's route is a jump,
    // not something that eases): ROUTE_COLORS is keyed by subway line, so most bus
    // route codes fall through to the same neutral fallback subwayLayer uses for an
    // unrecognized route -- reusing that palette, not inventing a bus-specific one.
    getFillColor: (d) => [...(ROUTE_COLORS[busRouteLabel(d.route_id)] || [244, 211, 94]), 210],
    getLineColor: [10, 12, 16],
    lineWidthMinPixels: 1,
    stroked: true,
    updateTriggers: { getFillColor: envelope.fetched_at },
  });
}

// Static GTFS route polylines (SubwayRouteShape), drawn as a faint, always-on backdrop
// under the train dots -- not one of the toggleable FEEDS entries below, since a 24h-TTL
// static reference layer isn't really a "live feed" a user would switch on and off, and
// pushing hundreds of routes at full opacity would read as spaghetti rather than context.
// Low alpha keeps this legible as "the physical track" without competing with the
// (pickable, brighter) marker layers drawn on top of it.
//
// When alerts-banner.js sets `highlightedRoute` (a route chip was clicked), that one
// route's shapes jump to full opacity and a thicker stroke while every other route dims
// further, so a rider reading "the 2 train skips Jackson Av" can see the 2 train's
// actual path. getColor/getWidth read `highlightedRoute` directly (a plain global, same
// as bikeLayer's zoom argument below), and updateTriggers is keyed on it so deck.gl
// re-renders on a click alone -- this layer's own `data` never changes when that
// happens, so ordinary prop diffing would otherwise leave the old colors on the GPU.
const SHAPE_DIM_ALPHA = 80;
const SHAPE_ECLIPSED_ALPHA = 35; // dimmer still: some other route is highlighted instead
const SHAPE_HIGHLIGHT_ALPHA = 230;
const SHAPE_DIM_WIDTH = 1.4;
const SHAPE_HIGHLIGHT_WIDTH = 4;

function shapeAlpha(routeId) {
  if (!highlightedRoute) return SHAPE_DIM_ALPHA;
  return routeId === highlightedRoute ? SHAPE_HIGHLIGHT_ALPHA : SHAPE_ECLIPSED_ALPHA;
}

function shapeWidth(routeId) {
  return routeId === highlightedRoute ? SHAPE_HIGHLIGHT_WIDTH : SHAPE_DIM_WIDTH;
}

function subwayShapesLayer(envelope) {
  if (!envelope || envelope.status === "error") return null;
  const data = envelope.records;
  if (!data.length) return null;
  return new deck.PathLayer({
    id: "subway_shapes",
    data,
    pickable: false,
    widthUnits: "pixels",
    getPath: (d) => d.points.map(([lat, lon]) => [lon, lat]),
    getColor: (d) => [...(ROUTE_COLORS[d.route_id] || [244, 211, 94]), shapeAlpha(d.route_id)],
    getWidth: (d) => shapeWidth(d.route_id),
    updateTriggers: {
      getColor: highlightedRoute,
      getWidth: highlightedRoute,
    },
  });
}

function inspectionsLayer(envelope) {
  const data = boroughFiltered(located(envelope.records), "boro");
  if (!data.length) return null;
  return new deck.ScatterplotLayer({
    id: "dohmh",
    data,
    pickable: true,
    autoHighlight: true,
    highlightColor: HOVER_HIGHLIGHT,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: 30,
    radiusMinPixels: 2,
    radiusMaxPixels: 8,
    // A/B/C is a categorical jump (like subway route color): easing red into green
    // through the palette would show a false intermediate grade, so no transition here.
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
    count: (env) => boroughCountLabel(env.records, "borough", "requests"),
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
    count: (env) => boroughCountLabel(env.records, "area", "cameras"),
  },
  {
    key: "dohmh_inspections",
    label: "Restaurant inspections",
    query: "limit=1000",
    defaultVisible: false, // dense data; opt-in like DOT cameras
    build: inspectionsLayer,
    count: (env) => boroughCountLabel(env.records, "boro", "inspections"),
  },
  {
    key: "mta_bus",
    label: "Buses",
    query: "limit=3000", // covers today's live fleet (~2400 vehicles) with headroom
    defaultVisible: false, // dense data; opt-in like DOT cameras
    build: busLayer,
    count: (env) => `${env.records.length} buses`,
  },
];

const WEATHER_KEY = "weather";
// Static GTFS route shapes (24h TTL): fetched once at boot via the same
// fetchFeed/applyEnvelope machinery every other feed uses (see data-sync.js), but kept
// out of FEEDS/STREAM_KEYS -- it is a background map decoration, not a layer the user
// toggles or a value that needs re-polling every REFRESH_S while unchanged for a day.
const SUBWAY_SHAPES_KEY = "mta_subway_shapes";
let subwayShapesEnvelope = null;
const STREAM_KEYS = FEEDS.map((f) => f.key).concat([WEATHER_KEY]);

function renderLayersNow() {
  if (!overlay) return;
  // Only bikeLayer reads this second argument (for zoom-aware decluttering); every other
  // builder's signature is (envelope) and simply ignores the extra positional arg.
  const zoom = map ? map.getZoom() : NYC.zoom;
  const layers = [];
  // Drawn first (deck.gl stacks later array entries on top), so the route network
  // always sits under every marker layer below.
  const shapes = subwayShapesLayer(subwayShapesEnvelope);
  if (shapes) layers.push(shapes);
  for (const feed of FEEDS) {
    const entry = state.get(feed.key);
    // status === "error" means there is no usable data: the layer is not drawn.
    if (!entry || !entry.visible || !entry.envelope) continue;
    if (entry.envelope.status === "error") continue;
    const layer = feed.build(entry.envelope, zoom);
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
  if (layer.id === "bus") {
    const nextStop = object.next_stop_name
      ? ` → ${escapeHtml(object.next_stop_name)}`
      : "";
    return {
      html: `<b>${escapeHtml(busRouteLabel(object.route_id))} bus</b>${nextStop}<br/>click for details`,
    };
  }
  return null;
}

// A MapLibre IControl (the same onAdd/onRemove contract NavigationControl already uses
// in app.js): flies back to the city-wide default view on click. `map.addControl(
// createRecenterControl(), "top-left")` puts it directly below the existing zoom control.
function createRecenterControl() {
  return {
    onAdd(controlMap) {
      const container = document.createElement("div");
      container.className = "maplibregl-ctrl maplibregl-ctrl-group";
      const button = document.createElement("button");
      button.type = "button";
      button.title = "Recenter on NYC";
      button.setAttribute("aria-label", "Recenter on NYC");
      button.textContent = "⌖"; // target/position glyph, no icon sprite needed
      button.addEventListener("click", () => {
        controlMap.flyTo({ center: [NYC.longitude, NYC.latitude], zoom: NYC.zoom });
      });
      container.appendChild(button);

      // Citi Bike's radiusMinPixels depends on the live zoom level (see bikeLegibility
      // above), which changes continuously as the user zooms with no new envelope data
      // to trigger a rebuild on its own. This is the first point in map-layers.js where a
      // live map instance exists, so it is also the natural place to wire that up.
      controlMap.on("zoom", renderLayers);

      this._container = container;
      this._map = controlMap;
      return container;
    },
    onRemove() {
      if (this._map) this._map.off("zoom", renderLayers);
      if (this._container && this._container.parentNode) {
        this._container.parentNode.removeChild(this._container);
      }
      this._map = null;
      this._container = null;
    },
  };
}

// The borough filter chips in #panel (index.html's #borough-filter). One write site
// for the shared `selectedBorough` global (state.js), mirroring how selectRoute() in
// alerts-banner.js is the sole writer for `highlightedRoute`: update the global,
// reflect the active chip, refresh the three affected layers' sidebar counts (they
// depend on selectedBorough via boroughCountLabel and would otherwise sit stale until
// the next poll/SSE tick), and ask map-layers.js's own renderLayers() to rebuild the
// map -- cameraLayer/layer311/inspectionsLayer's `data` hasn't changed, only which
// records pass the filter, so without this call the map would stay stale too.
const BOROUGH_FILTERED_KEYS = ["dot_cameras", "nyc_311", "dohmh_inspections"];

function setSelectedBorough(borough) {
  if (borough === selectedBorough) return;
  selectedBorough = borough;
  const container = el("borough-filter");
  if (container) {
    for (const chip of container.querySelectorAll(".borough-chip")) {
      const active = chip.dataset.borough === borough;
      chip.classList.toggle("is-active", active);
      chip.setAttribute("aria-pressed", active ? "true" : "false");
    }
  }
  for (const key of BOROUGH_FILTERED_KEYS) {
    const entry = state.get(key);
    if (entry && entry.envelope && typeof setPill === "function") setPill(key, entry.envelope);
  }
  renderLayers();
}

function handleBoroughFilterClick(event) {
  const chip = event.target.closest(".borough-chip[data-borough]");
  if (!chip) return;
  setSelectedBorough(chip.dataset.borough);
}

// Wires the static #borough-filter markup that index.html already renders. Not called
// from app.js's boot sequence (out of scope for this feature -- see the top-of-file
// ownership note); instead this self-initializes on DOMContentLoaded, the same idiom
// app.js itself uses at the bottom of its own file, since these are plain deferred
// scripts with no module system to hand this an explicit call site.
function initBoroughFilter() {
  const container = document.getElementById("borough-filter");
  if (!container) return;
  container.addEventListener("click", handleBoroughFilterClick);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initBoroughFilter);
} else {
  initBoroughFilter();
}
