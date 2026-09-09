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

  // The four below are drawn as filled silhouettes rather than in the outline style the
  // glyphs above use. That is deliberate, not drift: the outline style was drawn for the
  // 14px sidebar, and on the map an IconLayer marker is only ICON_MIN_PX (15px) across,
  // where a 1.1-wide interior stroke lands on ~1 screen pixel and greys out. Each of these was
  // rasterised at 15px and 30px through iconGlyphDataUri (the exact path that ships) and
  // picked for the reading that survived the 15px pass, filled where an outline collapsed.

  // 511NY events: an impact burst, NOT the traffic cone this started as -- a filled cone
  // reads as a solid triangle at 15px, one hue away from `alert`'s warning triangle, and
  // cone-plus-detached-base put a 2-unit base bar on a half-pixel row that washed out to
  // ~50% grey. The spike radii are deliberately uneven: an even 7- or 8-point star reads
  // as a sparkle or a sun, an uneven one reads as an impact, and neither reads as a triangle.
  incident: `<path d="M7.43 1.52 9.03 5.29 11.7 4.48 10.76 7.11 14.04 8.83 10.42 9.6 11.32 13.59 8.25 10.89 6.2 12.77 5.9 10 2.19 9.87 5.13 7.6 2.58 4.41 6.52 5.51Z" fill="currentColor" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>`,

  // Elevator/escalator outages: the ISO up/down arrow pair. An elevator-car outline with
  // arrows beside it shrank the arrows to ~2px nubs, leaving a near-featureless rectangle
  // of the same family as `bus`/`subway`; two arrows at full height read on their own.
  elevator: `<path d="M4.4 1.6 7.6 6.3H1.2L4.4 1.6Z" fill="currentColor"/><rect x="3.1" y="5.7" width="2.6" height="8.7" rx="0.5" fill="currentColor"/><path d="M11.6 14.4 8.4 9.7h6.4l-3.2 4.7Z" fill="currentColor"/><rect x="10.3" y="1.6" width="2.6" height="8.7" rx="0.5" fill="currentColor"/>`,

  // Air quality: haze cloud over suspended particulate. Wind-curl lines (the other obvious
  // choice) merged into an unreadable smear at 15px. The three particles are evenly spaced
  // on one row on purpose -- scattering them, or dropping to two, reads as a face.
  air_quality: `<circle cx="5.9" cy="6.1" r="2.7" fill="currentColor"/><circle cx="10" cy="5.3" r="3.4" fill="currentColor"/><rect x="3.2" y="6.1" width="9.6" height="3.4" rx="1.7" fill="currentColor"/><circle cx="4.6" cy="13" r="1.15" fill="currentColor"/><circle cx="8" cy="13" r="1.15" fill="currentColor"/><circle cx="11.4" cy="13" r="1.15" fill="currentColor"/>`,

  // NYC Ferry: hull, deckhouse, funnel. Outlining the hull turned it into a basket at 15px
  // (the hollow interior is what a bucket looks like); the funnel is what keeps the filled
  // version reading as a vessel rather than a bowl.
  ferry: `<path d="M1.9 9.2h12.2l-2.2 4.4H4.1L1.9 9.2Z" fill="currentColor"/><path d="M4.9 8.6V4.8a.8.8 0 0 1 .8-.8h4.6a.8.8 0 0 1 .8.8v3.8H4.9Z" fill="currentColor"/><rect x="7.4" y="1.4" width="1.3" height="2.8" rx="0.65" fill="currentColor"/>`,

  alert: `<path d="M8 2.2 14.3 13a.9.9 0 0 1-.8 1.4H2.5a.9.9 0 0 1-.8-1.4L8 2.2Z" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/><path d="M8 6.4v3M8 11.6h.01" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>`,

  chart: `<path d="M2.5 13.5h11M4 13V9.5M7.3 13V6M10.6 13V8M13.9 13V4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>`,
};

function icon(key, className) {
  const svg = ICONS[key];
  if (!svg) return "";
  const cls = className ? ` ${className}` : "";
  return `<svg class="icon${cls}" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">${svg}</svg>`;
}

// ---------------------------------------------------------------------------
// Map sprite atlas.
//
// The same pictograms, packed into one canvas for deck.gl's IconLayer, so a marker on
// the map is the shape of the thing it represents instead of a coloured dot -- and the
// shape matches the one already beside that layer's name in the sidebar, so the legend
// reads as the legend for the map rather than a separate vocabulary.
//
// Every cell is rendered as a WHITE glyph and mapped with `mask: true`. That hands the
// colour back to the layer's own getColor accessor, so per-record colour survives
// untouched: subway/bus route colours, restaurant grade colours, camera online/offline.
// Those hues were validated for colourblind separation in an earlier pass; baking colour
// into the atlas would have thrown that away and made every marker of a layer identical.
// ---------------------------------------------------------------------------

// Render resolution per glyph: four times the largest on-screen size, so the sprite
// stays crisp on retina and when zoomed-in markers grow.
const ICON_ATLAS_CELL = 64;

const ICON_ATLAS_KEYS = Object.keys(ICONS);

function iconGlyphDataUri(key) {
  // `color` resolves the icons' own `currentColor` to white; see the mask note above.
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" ` +
    `width="${ICON_ATLAS_CELL}" height="${ICON_ATLAS_CELL}" color="#ffffff">${ICONS[key]}</svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function iconAtlasMapping() {
  const mapping = {};
  ICON_ATLAS_KEYS.forEach((key, index) => {
    mapping[key] = {
      x: index * ICON_ATLAS_CELL,
      y: 0,
      width: ICON_ATLAS_CELL,
      height: ICON_ATLAS_CELL,
      mask: true,
    };
  });
  return mapping;
}

/** Resolves to `{url, mapping}` for deck.gl's IconLayer. Never rejects: a glyph that
 * fails to rasterise is left transparent and the rest of the atlas still ships, because
 * one bad icon must not take the whole map down to no markers at all. */
function buildIconAtlas() {
  const canvas = document.createElement("canvas");
  canvas.width = ICON_ATLAS_CELL * ICON_ATLAS_KEYS.length;
  canvas.height = ICON_ATLAS_CELL;
  const ctx = canvas.getContext("2d");
  const draws = ICON_ATLAS_KEYS.map(
    (key, index) =>
      new Promise((resolve) => {
        const img = new Image();
        img.onload = () => {
          ctx.drawImage(img, index * ICON_ATLAS_CELL, 0, ICON_ATLAS_CELL, ICON_ATLAS_CELL);
          resolve();
        };
        img.onerror = () => resolve();
        img.src = iconGlyphDataUri(key);
      })
  );
  return Promise.all(draws).then(() => ({ url: canvas.toDataURL(), mapping: iconAtlasMapping() }));
}
