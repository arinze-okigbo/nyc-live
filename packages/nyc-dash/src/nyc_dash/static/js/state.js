/*
 * Shared mutable state and page-wide constants. Loaded early so every other script can
 * read/write these without import machinery (this page has no build step or modules).
 */

const REFRESH_S = 15;
const NYC = { longitude: -73.9855, latitude: 40.7484, zoom: 11.2, pitch: 0, bearing: 0 };
const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

const state = new Map(); // key -> {envelope, visible}
let overlay = null;
let map = null;
let source = null; // EventSource
let pollTimer = null;

// Set by alerts-banner.js when a route chip is clicked, read by map-layers.js's
// subwayShapesLayer() to pick which route's PathLayer paths draw at full emphasis.
// null means "no route highlighted" (every shape draws at its normal, dim opacity).
// Lives here rather than as a module-local in either file because both genuinely
// need to read/write it and there is no import machinery to share it otherwise.
let highlightedRoute = null;

// Borough names as the sidebar's #borough-filter chips (index.html) label them. The
// three record types that actually carry a clean borough field don't agree on case
// (Camera.area and RestaurantInspection.boro title-case them, ServiceRequest.borough
// upper-cases them via Socrata) -- comparisons in map-layers.js's inBorough() are
// always case-insensitive, so this list is just the canonical display label, not an
// assumption about any one feed's raw value.
const BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"];

// Set by map-layers.js's initBoroughFilter() when a chip is clicked, read by
// cameraLayer/layer311/inspectionsLayer (map-layers.js) -- the only three layers whose
// record types carry a clean borough field -- to narrow those layers, and their
// sidebar counts, to one borough. "all" means no filtering. Lives here for the same
// reason as highlightedRoute above: it is read and written from more than one place
// with no import machinery to share it otherwise.
let selectedBorough = "all";
