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
