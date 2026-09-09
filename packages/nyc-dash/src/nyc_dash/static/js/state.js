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
