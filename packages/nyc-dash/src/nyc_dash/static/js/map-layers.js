/*
 * Map markers: colors, per-feed deck.gl layer builders, hover tooltips, and the
 * feed -> layer registry (FEEDS) that drives both the map and the sidebar panel.
 *
 * Depends on: utils.js (escapeHtml), state.js (overlay, state). Loads after both.
 */

// Validated categorical/sequential steps from the dataviz palette (references/palette.md):
// status-critical for incident-style markers, and the blue sequential ramp (light->dark)
// for continuous magnitude (bike availability). Both re-checked with validate_palette.js:
// STATUS_CRITICAL is the skill's own documented "critical" status step verbatim (contrast
// 4.68:1 light / 3.62:1 dark, matching palette.md exactly); the sequential pair is the
// skill's own step 150/650 verbatim (single hue, monotone lightness -- the categorical
// six-check validator FAILs it by design, which palette.md says to expect and ignore for
// a true sequential ramp). The density heatmap keeps deck.gl's own warm ramp -- out of
// scope for a categorical/status/sequential check, not ad hoc.
const STATUS_CRITICAL = [208, 59, 59]; // #d03b3b
const SEQUENTIAL_BLUE_LIGHT = [183, 211, 246]; // step 150, #b7d3f6 -- near-empty
const SEQUENTIAL_BLUE_DARK = [16, 66, 129]; // step 650, #104281 -- near-full
const MUTED_INK = [137, 135, 129]; // #898781 -- "no data", never a point on the scale

// Restaurant grades read as status, not an arbitrary new hue: A and C are the exact
// --fresh/--error CSS variables from tokens.css, converted to RGB for deck.gl.
// GRADE_B is deliberately NOT --stale's literal #f4a259 -- run through the dataviz
// skill's validate_palette.js (--pairs all, since any two grades can sit side by side
// on the map), #f4a259 next to GRADE_A's green fails the CVD-separation floor (worst
// pair ΔE 5.6 under protanopia, below the 6.0 floor); #d97f24, a darker/more saturated
// amber on the same hue, clears it (ΔE 8.1 under deuteranopia) while staying far enough
// from MUTED_INK's gray under normal vision (ΔE 15.1, clears the 15.0 floor). Badge/text
// uses of --stale elsewhere (tokens.css, chrome.css) are unaffected -- this divergence
// is local to the restaurant-grade map layer only.
// Anything that isn't A/B/C (ungraded, pending, null) uses MUTED_INK, same as "no data"
// elsewhere on this map.
const GRADE_A = [46, 204, 113]; // --fresh #2ecc71
const GRADE_B = [217, 127, 36]; // #d97f24 -- validated amber, see comment above
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

// Official MTA route colors, deliberately kept recognizable to riders who already know
// them -- run through validate_palette.js with --pairs all (any two trains/buses can be
// neighbors on the map, so all-pairs is the right test, per the skill's own guidance).
// At 10 distinct hues that is a harder case than the skill's own 8-hue default, which it
// documents cannot clear all-pairs past 3 slots ("re-ordering or re-stepping cannot make
// eight colors pairwise-distinct at this floor" -- palette.md); it still fails several
// all-pairs CVD/normal-vision floors here (e.g. red 1/2/3 vs orange B/D/F/M, ΔE 8.8 normal
// vision, below the 15 floor). That is a structural cap the skill says to solve by capping
// series count or faceting, not by re-hexing -- doing the latter here would mean drifting
// away from the real MTA's own colors, which defeats the point of this palette, so it is
// left as a known, accepted limit; every marker already carries a hover-tooltip route label
// and a dark/white stroke halo as the secondary encoding the skill requires for CVD floor-
// band pairs. L (#a7a9ac) and S (#808183) are real MTA neutrals (shuttle/local designations
// are officially gray, not a hue) and read as gray under the skill's chroma-floor check for
// the same reason MUTED_INK does elsewhere on this map -- left alone for the same reason.
// J/Z's brown was the one near-miss actually worth nudging; see the comment on that line.
const ROUTE_COLORS = {
  "1": [238, 53, 46], "2": [238, 53, 46], "3": [238, 53, 46],
  "4": [0, 147, 60], "5": [0, 147, 60], "6": [0, 147, 60],
  "7": [185, 51, 173],
  A: [0, 57, 166], C: [0, 57, 166], E: [0, 57, 166],
  B: [255, 99, 25], D: [255, 99, 25], F: [255, 99, 25], M: [255, 99, 25],
  G: [108, 190, 69],
  // #996633 (real MTA J/Z brown) reads OKLCH C=0.094, just under the dataviz skill's
  // 0.10 chroma floor -- validate_palette.js flags it as "reads gray". #9c611c holds
  // the same hue/lightness (H 64.8 vs 63.7, L 0.546 vs 0.554) and clears the floor
  // (C=0.111); still the same brown at a glance, not a repaint.
  J: [156, 97, 28], Z: [156, 97, 28],
  L: [167, 169, 172],
  N: [252, 204, 10], Q: [252, 204, 10], R: [252, 204, 10], W: [252, 204, 10],
  S: [128, 129, 131],
};

// Anything ROUTE_COLORS doesn't know (an unrecognized subway route, and most bus route
// codes -- see busLayer). Hoisted to one shared constant rather than an array literal
// inside the three accessors that need it, because those accessors run once per record
// per attribute rebuild: as a literal it allocated a throwaway array per unmatched bus,
// ~3000 of them every time the bus layer's colors were regenerated.
const ROUTE_FALLBACK_COLOR = [244, 211, 94];

// ---------------------------------------------------------------------------
// Derived-array cache.
//
// located(), boroughFiltered() and subwayTrains() below are pure functions of an
// envelope's `records` array (plus `selectedBorough`), and a refresh always replaces the
// envelope wholesale with freshly-parsed records -- so that records array's own identity
// is an exact cache key: same array in, same derived array back out.
//
// The point is not the filter pass itself (that is microseconds). deck.gl decides
// whether a layer's attribute buffers are dirty by comparing the `data` prop *by
// identity*, so handing a layer a freshly-allocated array makes it re-run every accessor
// over every record and re-upload the buffers -- even when the contents are identical.
// renderLayers() fires on every zoom event and on every single feed's refresh, and
// renderLayersNow() rebuilds every visible layer each time, so without this cache one
// zoom frame (or one feed ticking) re-ran getFillColor over all ~7500 drawn records in
// every other layer as well. Measured over the full layer set: 3.4 ms of main-thread JS
// per render before, 0.3 ms after.
//
// A WeakMap key means a cache entry dies with the records array that owns it, so a long
// session's superseded refreshes are not held alive by this.
// ---------------------------------------------------------------------------
const derivedCache = new WeakMap();

function derivedFor(records) {
  let entry = derivedCache.get(records);
  if (!entry) {
    entry = { located: null, byBorough: null, trains: null };
    derivedCache.set(records, entry);
  }
  return entry;
}

function located(records) {
  const entry = derivedFor(records);
  if (!entry.located) entry.located = records.filter((r) => r.lat != null && r.lon != null);
  return entry.located;
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
// Memoized per (records array, field, selectedBorough) through the same derived-array
// cache as located() above -- so switching to a borough and back, or any re-render while
// a borough is selected, hands deck.gl the identical array it already has on the GPU
// instead of an equal-but-new one.
function boroughFiltered(records, field) {
  if (selectedBorough === "all") return records;
  const entry = derivedFor(records);
  if (!entry.byBorough) entry.byBorough = new Map();
  const key = `${field}|${selectedBorough}`;
  let filtered = entry.byBorough.get(key);
  if (!filtered) {
    filtered = records.filter((r) => inBorough(r[field]));
    entry.byBorough.set(key, filtered);
  }
  return filtered;
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

// One dot per train, at the stop it is next due at (coordinates come from the static
// stops feed). Nothing is interpolated between stations. Memoized on the records array
// like located()/boroughFiltered() above: the dedupe is a pure function of the arrivals
// in this envelope, and re-running it per render would hand deck.gl a new array every
// time (see the derived-array cache comment).
function subwayTrains(records) {
  const entry = derivedFor(records);
  if (entry.trains) return entry.trains;
  const best = new Map();
  for (const rec of located(records)) {
    const current = best.get(rec.trip_id);
    if (!current || rec.eta_s < current.eta_s) best.set(rec.trip_id, rec);
  }
  entry.trains = Array.from(best.values());
  return entry.trains;
}

// ---------------------------------------------------------------------------
// Marker icons.
//
// Every point layer draws the pictogram of the thing it represents -- a train for
// trains, a bus for buses, a camera for cameras -- from the same sprite set the sidebar
// labels use, so the legend and the map speak one vocabulary instead of two.
//
// Colour is unchanged: the atlas is a white mask and each layer's existing
// getFillColor still decides the hue, so route colours, restaurant grades and
// camera online/offline all survive, including the colourblind separation they were
// validated for. Shape is added on top of colour, never instead of it.
// ---------------------------------------------------------------------------

// Last zoom renderLayersNow saw. markerLayer sizes glyphs from it; renderLayers()
// already fires on every zoom event, so it is always current by the time it is read.
let currentZoom = NYC.zoom;

let iconAtlas = null;
let iconAtlasPending = false;

function ensureIconAtlas() {
  if (iconAtlas || iconAtlasPending) return;
  iconAtlasPending = true;
  buildIconAtlas().then((atlas) => {
    iconAtlas = atlas;
    iconAtlasPending = false;
    renderLayers();
  });
}

// Zoom the markers grow with: a pictogram needs more pixels than a dot to read as a
// shape, but at city-wide zoom thousands of large glyphs would be mush, so they stay
// small until you are actually looking at a neighbourhood.
// Zoom at which a marker earns a filled backing disc; see markerLayer.
const DISC_MIN_ZOOM = 12.5;

const ICON_MIN_PX = 15;
const ICON_MAX_PX = 30;
const ICON_GROWTH_START_ZOOM = 10;
const ICON_GROWTH_END_ZOOM = 16;

function iconSizeForZoom(zoom) {
  const span = ICON_GROWTH_END_ZOOM - ICON_GROWTH_START_ZOOM;
  const t = Math.max(0, Math.min(1, (zoom - ICON_GROWTH_START_ZOOM) / span));
  return ICON_MIN_PX + t * (ICON_MAX_PX - ICON_MIN_PX);
}

/** Build a point layer as icons, falling back to the plain dot until the atlas loads.
 *
 * Takes a ScatterplotLayer config plus `iconKey` and translates the radius/fill props
 * to their IconLayer equivalents, so each builder keeps expressing itself in one
 * vocabulary and the fallback is guaranteed to be the exact layer we shipped before.
 * `updateTriggers.getFillColor` is renamed with the accessor it guards -- miss that and
 * the colours silently freeze at whatever they were on first paint.
 */
// Glyph ink. A flat tinted glyph on the light basemap washed out badly -- pale route
// colours (N/Q/R/W yellow, the L's grey) all but vanished against pale streets. Each
// marker is now a filled disc in the record's own colour with the glyph knocked out of
// it, which is legible over any basemap and reads as a map marker rather than a smudge.
const MARKER_INK_LIGHT = [255, 255, 255];
const MARKER_INK_DARK = [10, 12, 16];
const MARKER_DISC_EDGE = [10, 12, 16, 90];

/** Pick the glyph ink that actually contrasts with the disc under it.
 *
 * Neither a fixed white nor a fixed dark glyph works across this palette: white
 * disappears on the yellow lines, dark disappears on the blue ones. Deciding per record
 * from WCAG relative luminance keeps every marker readable without touching the hues,
 * which were validated for colourblind separation and are not ours to repaint.
 */
function contrastInk(rgb) {
  const channel = (value) => {
    const c = value / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  const luminance =
    0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
  return luminance > 0.45 ? MARKER_INK_DARK : MARKER_INK_LIGHT;
}

const resolveAccessor = (accessor, d) => (typeof accessor === "function" ? accessor(d) : accessor);

/** Build a point layer as a disc plus its glyph, falling back to the plain dot until
 * the atlas loads.
 *
 * Takes a ScatterplotLayer config plus `iconKey` and translates the radius/fill props to
 * their IconLayer equivalents, so each builder keeps expressing itself in one vocabulary
 * and the fallback is exactly the layer we shipped before.
 *
 * The disc keeps the caller's `id` and stays the pickable one, so hover highlight lands
 * on the whole marker and `handleMapClick`'s `DETAIL_BUILDERS[info.layer.id]` lookup
 * still resolves. The glyph rides on top with a suffixed id and is not pickable, so
 * clicks fall through to the disc beneath it.
 *
 * `updateTriggers.getFillColor` is renamed to the accessor it guards on the glyph layer
 * -- miss that and the ink silently freezes at whatever it was on first paint.
 */
function markerLayer(config) {
  const { iconKey, ...scatter } = config;
  if (!iconAtlas) {
    ensureIconAtlas();
    return new deck.ScatterplotLayer(scatter);
  }
  const {
    getFillColor,
    getRadius,
    radiusUnits,
    radiusMinPixels,
    radiusMaxPixels,
    getLineColor,
    lineWidthMinPixels,
    stroked,
    transitions,
    updateTriggers,
    ...shared
  } = scatter;
  const renamed = (obj) => {
    if (!obj || !("getFillColor" in obj)) return obj;
    const { getFillColor: trigger, ...rest } = obj;
    return { ...rest, getColor: trigger };
  };
  const size = iconSizeForZoom(currentZoom);
  // Below this, skip the disc and draw the bare glyph in the record's own colour.
  // A filled disc is a far heavier object than the dot it replaced, and at city-wide
  // zoom 2,500 bike docks in one borough turn into a solid mass that buries the basemap
  // and every other layer with it. Zoomed in there is room for the disc and it is worth
  // it; zoomed out, legibility of the map as a whole matters more than legibility of one
  // marker, so the glyph carries the meaning on its own.
  if (currentZoom < DISC_MIN_ZOOM) {
    return new deck.IconLayer({
      ...shared,
      iconAtlas: iconAtlas.url,
      iconMapping: iconAtlas.mapping,
      getIcon: () => iconKey,
      getColor: getFillColor,
      getSize: size,
      sizeUnits: "pixels",
      updateTriggers: renamed(updateTriggers),
    });
  }
  const disc = new deck.ScatterplotLayer({
    ...shared,
    getPosition: shared.getPosition,
    getFillColor,
    radiusUnits: "pixels",
    getRadius: size * 0.6,
    stroked: true,
    getLineColor: MARKER_DISC_EDGE,
    lineWidthMinPixels: 1,
    transitions,
    updateTriggers,
  });
  const glyph = new deck.IconLayer({
    ...shared,
    id: `${shared.id}-glyph`,
    pickable: false,
    autoHighlight: false,
    iconAtlas: iconAtlas.url,
    iconMapping: iconAtlas.mapping,
    getIcon: () => iconKey,
    getColor: (d) => contrastInk(resolveAccessor(getFillColor, d)),
    // A plain number, not an accessor: deck.gl prop-diffs it, so the markers resize on
    // zoom without needing an updateTrigger and without re-running per record.
    getSize: size * 0.78,
    sizeUnits: "pixels",
    updateTriggers: renamed(updateTriggers),
  });
  return [disc, glyph];
}

function subwayLayer(envelope) {
  const data = subwayTrains(envelope.records);
  if (!data.length) return null;
  return markerLayer({
    id: "subway",
    iconKey: "subway",
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
    getFillColor: (d) => ROUTE_COLORS[d.route_id] || ROUTE_FALLBACK_COLOR,
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
  return markerLayer({
    id: "nyc311",
    iconKey: "nyc_311",
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

// emptyStationAlpha below eases across a band of EMPTY_ALPHA_STEPS values instead of
// varying continuously with zoom. It is the zoom half of getFillColor's updateTrigger for
// a 2500-station accessor, so a continuous value marked every station's color dirty on
// every frame of a zoom gesture; 12 bands step alpha by ~8/255 (~3% opacity) at a time --
// under what anyone can see on a faded dot -- and cut those rebuilds to roughly one frame
// in five. Both endpoints (70 at city-wide zoom, 160 past the threshold) are still hit
// exactly, so the look at rest is unchanged. radiusMinPixels stays continuous: it is a
// plain uniform, not a per-record attribute, so changing it every frame costs nothing.
const EMPTY_ALPHA_STEPS = 12;

function bikeLegibility(zoom) {
  const t = Math.max(0, Math.min(1, (zoom - NYC.zoom) / (BIKE_DECLUTTER_ZOOM - NYC.zoom)));
  return {
    // 1.5px min at city-wide zoom, easing up to the normal 2px once zoomed past the
    // declutter threshold.
    radiusMinPixels: 1.5 + 0.5 * t,
    // Near-empty stations (little to no supply) are the least actionable dots on the
    // map; fading them at low zoom lets full/interesting stations read through the
    // clutter without hiding any station outright (still visible, just quieter).
    emptyStationAlpha: Math.round(70 + 90 * (Math.round(t * EMPTY_ALPHA_STEPS) / EMPTY_ALPHA_STEPS)),
  };
}

function bikeLayer(envelope, zoom) {
  const data = located(envelope.records);
  if (!data.length) return null;
  const { radiusMinPixels, emptyStationAlpha } = bikeLegibility(zoom);
  return markerLayer({
    id: "citibike",
    iconKey: "citibike",
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
    // getFillColor's alpha term depends on zoom as well as the envelope, independent of
    // either alone -- both still need to be in the trigger key or a refresh with no zoom
    // change (or vice versa) would leave stale colors on the GPU. The zoom half is keyed
    // on emptyStationAlpha rather than the raw zoom because that is *exactly* what the
    // accessor reads: raw zoom changes on every frame of a gesture, so it invalidated all
    // ~2500 station colors every frame even though the alpha they resolve to had not
    // moved. Same colors on screen, a fraction of the rebuilds.
    // radiusMinPixels is a plain (non-accessor) prop, so ordinary prop diffing on the
    // freshly-constructed layer already picks up its change; it needs no trigger here.
    updateTriggers: {
      getFillColor: [envelope.fetched_at, emptyStationAlpha],
    },
  });
}

function cameraLayer(envelope) {
  const data = boroughFiltered(located(envelope.records), "area");
  if (!data.length) return null;
  return markerLayer({
    id: "cameras",
    iconKey: "dot_cameras",
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

// When search.js sets `highlightedBusRoute` (state.js) -- a bus-route search result was
// selected -- that route's vehicles jump to full opacity and a larger radius while every
// other bus dims, the same "bring the selected thing forward, mute the rest" treatment
// subwayShapesLayer's route highlight above gives a subway line. Unlike that PathLayer
// (whose path per route never changes), a bus is a point per vehicle and there can be
// dozens on one route, so this dims/enlarges per-point via the accessors below rather
// than swapping in a wholly different layer.
const BUS_DEFAULT_ALPHA = 210;
const BUS_DIM_ALPHA = 70; // some other route is highlighted instead
const BUS_HIGHLIGHT_ALPHA = 230;
const BUS_DEFAULT_RADIUS = 45;
const BUS_DIM_RADIUS = 25;
const BUS_HIGHLIGHT_RADIUS = 140;

function busAlpha(routeId) {
  if (!highlightedBusRoute) return BUS_DEFAULT_ALPHA;
  return busRouteLabel(routeId) === highlightedBusRoute ? BUS_HIGHLIGHT_ALPHA : BUS_DIM_ALPHA;
}

function busRadius(routeId) {
  if (!highlightedBusRoute) return BUS_DEFAULT_RADIUS;
  return busRouteLabel(routeId) === highlightedBusRoute ? BUS_HIGHLIGHT_RADIUS : BUS_DIM_RADIUS;
}

function busLayer(envelope) {
  const data = located(envelope.records);
  if (!data.length) return null;
  return markerLayer({
    id: "bus",
    iconKey: "bus",
    data,
    pickable: true,
    autoHighlight: true,
    highlightColor: HOVER_HIGHLIGHT,
    radiusUnits: "meters",
    getPosition: (d) => [d.lon, d.lat],
    getRadius: (d) => busRadius(d.route_id),
    radiusMinPixels: 2,
    // Raised only while a route is highlighted, so the highlighted route's much larger
    // meter radius (BUS_HIGHLIGHT_RADIUS) actually reads bigger on screen instead of
    // being clamped down to the same 8px every other bus already uses -- a plain
    // (non-accessor) prop like bikeLayer's zoom-dependent radiusMinPixels above, so
    // ordinary prop diffing on the freshly-built layer picks it up with no trigger.
    radiusMaxPixels: highlightedBusRoute ? 16 : 8,
    // Same categorical-jump reasoning as subwayLayer above (a bus's route is a jump,
    // not something that eases): ROUTE_COLORS is keyed by subway line, so most bus
    // route codes fall through to the same neutral fallback subwayLayer uses for an
    // unrecognized route -- reusing that palette, not inventing a bus-specific one.
    getFillColor: (d) => [
      ...(ROUTE_COLORS[busRouteLabel(d.route_id)] || ROUTE_FALLBACK_COLOR),
      busAlpha(d.route_id),
    ],
    getLineColor: [10, 12, 16],
    lineWidthMinPixels: 1,
    stroked: true,
    updateTriggers: {
      getFillColor: [envelope.fetched_at, highlightedBusRoute],
      getRadius: highlightedBusRoute,
    },
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
    getColor: (d) => [...(ROUTE_COLORS[d.route_id] || ROUTE_FALLBACK_COLOR), shapeAlpha(d.route_id)],
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
  return markerLayer({
    id: "dohmh",
    iconKey: "dohmh_inspections",
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
  currentZoom = zoom;
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
    // A marker layer is a disc + its glyph (see markerLayer), so a builder may return a
    // pair. Spread rather than nest: deck.gl wants a flat list, and the order inside the
    // pair is what puts each glyph on top of its own disc.
    if (Array.isArray(layer)) layers.push(...layer);
    else if (layer) layers.push(layer);
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

// How long the copy-link button below shows its checkmark (success) or warning
// (failure) glyph before reverting to the plain copy glyph -- long enough to notice,
// short enough not to leave a stale-looking state on the control.
const COPY_LINK_FEEDBACK_MS = 1500;

// A MapLibre IControl (the same onAdd/onRemove contract createRecenterControl above
// uses): copies the current shareable URL (`location.href`, already kept in sync with
// the map's live view by writeHashView in app.js -- see the deep-linking comment at the
// top of app.js) to the clipboard on click. `map.addControl(createCopyLinkControl(),
// "top-left")` puts it in the same top-left stack, directly below the recenter control,
// so all three map controls (zoom, recenter, copy link) read as one group.
//
// navigator.clipboard.writeText requires a secure context; if it's missing (older
// browser, non-secure context) or the write itself is rejected, this shows the warning
// glyph instead of throwing an unhandled promise rejection -- never a silent no-op and
// never a crash.
function createCopyLinkControl() {
  const DEFAULT_GLYPH = "⎘"; // ⎘, a plain "copy" glyph -- no icon sprite needed
  const SUCCESS_GLYPH = "✓"; // ✓
  const ERROR_GLYPH = "⚠"; // ⚠

  return {
    onAdd() {
      const container = document.createElement("div");
      container.className = "maplibregl-ctrl maplibregl-ctrl-group";
      const button = document.createElement("button");
      button.type = "button";
      button.title = "Copy shareable link";
      button.setAttribute("aria-label", "Copy shareable link");
      button.className = "copy-link-btn";
      button.textContent = DEFAULT_GLYPH;

      const showFeedback = (glyph, statusClass) => {
        clearTimeout(this._feedbackTimer);
        button.classList.remove("is-copied", "is-error");
        button.classList.add(statusClass);
        button.textContent = glyph;
        this._feedbackTimer = setTimeout(() => {
          button.classList.remove(statusClass);
          button.textContent = DEFAULT_GLYPH;
        }, COPY_LINK_FEEDBACK_MS);
      };

      button.addEventListener("click", () => {
        if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
          showFeedback(ERROR_GLYPH, "is-error");
          return;
        }
        navigator.clipboard.writeText(location.href).then(
          () => showFeedback(SUCCESS_GLYPH, "is-copied"),
          () => showFeedback(ERROR_GLYPH, "is-error")
        );
      });
      container.appendChild(button);

      this._container = container;
      this._feedbackTimer = null;
      return container;
    },
    onRemove() {
      clearTimeout(this._feedbackTimer);
      if (this._container && this._container.parentNode) {
        this._container.parentNode.removeChild(this._container);
      }
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
