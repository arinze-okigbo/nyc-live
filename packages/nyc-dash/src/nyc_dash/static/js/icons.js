/*
 * Shared inline-SVG icon set, one simple pictogram per feed type plus a couple of
 * general-purpose glyphs (alert, chart). No icon font/library dependency -- this
 * project pins only maplibre-gl/deck.gl from a CDN, everything else is hand-rolled to
 * match that minimal-dependency philosophy.
 *
 * Every icon is a 16x16 viewBox, stroke/fill via `currentColor` so callers recolor
 * with plain CSS `color`, and ships `aria-hidden="true"` since it's always paired with
 * visible text (the layer label, the panel title) -- the icon is decoration, not the
 * only carrier of meaning.
 *
 * Depends on: nothing. Loads before status-panel.js and detail-panel.js, both of
 * which call icon(key, [className]).
 */

const ICONS = {
  subway: `<path d="M4 2.5h8a1 1 0 0 1 1 1V10a2.5 2.5 0 0 1-2.5 2.5h-.2l1 1.5H10l-1-1.5H7l-1 1.5H4.7l1-1.5h-.2A2.5 2.5 0 0 1 3 10V3.5a1 1 0 0 1 1-1Z" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/><path d="M4.5 4.5h7M5 8.5h.01M11 8.5h.01" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>`,

  density: `<circle cx="8" cy="5.2" r="1.6" fill="none" stroke="currentColor" stroke-width="1.1"/><path d="M5.2 12c.3-2 1.4-3.2 2.8-3.2s2.5 1.2 2.8 3.2" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/><rect x="1.6" y="9.5" width="3" height="3" rx="0.6" fill="none" stroke="currentColor" stroke-width="1"/><rect x="11.4" y="9.5" width="3" height="3" rx="0.6" fill="none" stroke="currentColor" stroke-width="1"/>`,

  nyc_311: `<path d="M2.5 6.5v3a1 1 0 0 0 1 1h1.2l3.6 2.3a.6.6 0 0 0 .9-.5V4.7a.6.6 0 0 0-.9-.5L4.7 6.5H3.5a1 1 0 0 0-1 1Z" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/><path d="M11 6a2.2 2.2 0 0 1 0 4M12.6 4.3a4.6 4.6 0 0 1 0 7.4" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>`,

  citibike: `<circle cx="4" cy="11.5" r="2.2" fill="none" stroke="currentColor" stroke-width="1.1"/><circle cx="12" cy="11.5" r="2.2" fill="none" stroke="currentColor" stroke-width="1.1"/><path d="M4 11.5 7 5h2.2M12 11.5 9.4 6.3H6.6L5.2 9M7.2 5h2.4" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/>`,

  dot_cameras: `<rect x="2" y="5" width="9" height="6.5" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.1"/><path d="M11 7l3-1.6v6.2L11 10" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/><circle cx="6.3" cy="8.2" r="1.4" fill="none" stroke="currentColor" stroke-width="1"/>`,

  dohmh_inspections: `<path d="M4 2.5v5.2a1.6 1.6 0 0 0 3.2 0V2.5M5.6 2.5v3.8M4 2.5v0" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/><path d="M11 2.5c-1 0-1.6.9-1.6 2.4S10 7.4 11 7.4v6.1" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/><path d="M5.6 8.2v5.3" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>`,

  bus: `<rect x="2" y="3.5" width="12" height="7.5" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.1"/><path d="M2 6.7h12M5.2 3.5v3.2M10.8 3.5v3.2" stroke="currentColor" stroke-width="1" stroke-linecap="round"/><circle cx="4.8" cy="12.2" r="1.1" fill="none" stroke="currentColor" stroke-width="1"/><circle cx="11.2" cy="12.2" r="1.1" fill="none" stroke="currentColor" stroke-width="1"/>`,

  alert: `<path d="M8 2.2 14.3 13a.9.9 0 0 1-.8 1.4H2.5a.9.9 0 0 1-.8-1.4L8 2.2Z" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/><path d="M8 6.4v3M8 11.6h.01" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>`,

  chart: `<path d="M2.5 13.5h11M4 13V9.5M7.3 13V6M10.6 13V8M13.9 13V4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>`,
};

function icon(key, className) {
  const svg = ICONS[key];
  if (!svg) return "";
  const cls = className ? ` ${className}` : "";
  return `<svg class="icon${cls}" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">${svg}</svg>`;
}
