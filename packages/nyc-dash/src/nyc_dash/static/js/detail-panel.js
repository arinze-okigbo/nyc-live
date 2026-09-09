/*
 * The click-detail panel: one persistent panel, reused by every clickable layer
 * (subway, 311, Citi Bike, DOT cameras, restaurant inspections, buses) instead of six
 * bespoke UIs. `openDetailPanel` owns the panel's DOM; each layer's `*Detail` function
 * only supplies a title, a function that fills in the body, and an `identity` (the feed
 * key + the specific record shown) so the panel can find that same record again on a
 * later refresh. The body-builder may return either a bare cleanup function (clearing a
 * setInterval, cancelling a fetch -- runs when the panel is closed or replaced) or, for
 * builders with an async subsection worth preserving across a refresh (cameraDetail's
 * live image/density chart, subwayDetail's stop list), the richer `{ cleanup, update }`
 * shape: `update(freshRecord)` re-renders just the summary fields without touching that
 * subsection. See applyBuildResult()/refreshOpenDetailPanel() below.
 *
 * refreshOpenDetailPanel(feedKey, envelope) is called by data-sync.js's applyEnvelope on
 * every feed refresh (poll or SSE) so an open panel never goes stale between clicks --
 * this is the fix for the "ETA/next-stop frozen forever" bug: previously a panel's body
 * was built exactly once, at click time, and never touched again even though the
 * underlying feed kept refreshing every REFRESH_S.
 *
 * Depends on: utils.js (el, escapeHtml, hhmmss), state.js (state), map-layers.js
 * (busRouteLabel).
 */

// Kept in sync with the CSS transition-duration on .detail-panel below (detail-panel.css).
const DETAIL_PANEL_TRANSITION_MS = 180;

let panelCleanup = null;
// Set alongside panelCleanup when the open panel's body-builder returns the richer
// `{ cleanup, update }` shape instead of a bare cleanup function -- see
// applyBuildResult() and refreshOpenDetailPanel() below. null means "this panel has no
// lightweight update path", which makes refreshOpenDetailPanel fall back to a full
// body rebuild (still cheap for panels with no async subsections of their own).
let panelUpdate = null;
// The close animation's pending "actually hide it now" timeout. Tracked so a second
// open/close arriving before the first close's transition finishes cancels the stale
// timer instead of letting it hide a panel that was just reopened.
let panelCloseTimer = null;
// Whatever had focus right before the panel opened -- a clicked search-result <li>
// (search.js), or document.body if the trigger was a mouse-only deck.gl marker click
// (deck.gl markers are canvas-drawn, not real focusable DOM nodes). closeDetailPanel
// restores focus here so keyboard/screen-reader users don't lose their place.
let panelTriggerElement = null;
// One-shot fallback restoration target, for callers whose real trigger element won't
// survive until the panel closes (search.js's result <li>s are torn down by
// clearSearch() right after selection, so restoring to the <li> itself is impossible
// once that runs). A caller sets this global immediately before invoking a builder --
// the same "drive cross-file behavior through a shared global" idiom search.js already
// uses for highlightedBusRoute -- and openDetailPanel consumes (and clears) it on the
// very next open. closeDetailPanel falls back to it only if the primary trigger is no
// longer in the DOM.
let panelFocusFallback = null;
let panelTriggerFallback = null;
// Guards against attaching the Tab-trap keydown listener more than once: the panel
// element itself is created once in index.html and reused (only its innerHTML and
// hidden/is-open state change across opens), so the listener only needs attaching once.
let panelTrapAttached = false;

// Live-refresh bookkeeping (the "don't leave an open panel frozen" fix): which feed the
// currently-open panel belongs to, the real-world record it's currently showing, and the
// body-builder that produced it, so refreshOpenDetailPanel() can find the record's fresh
// version in the next envelope and re-render without tearing down/reopening the panel.
// feedKey uses the same strings as data-sync.js's applyEnvelope (FEEDS[].key /
// STREAM_KEYS), not DETAIL_BUILDERS' layer-id keys -- that's what refreshOpenDetailPanel
// is called with, and keeping both tables in that vocabulary avoids a second translation
// table for no benefit.
let openPanelFeedKey = null;
let openPanelRecord = null;
let openPanelBuildBody = null;
// True once the tracked record has been confirmed absent from a fresh envelope (train
// completed its run, bus went out of service, etc.) -- stops refreshOpenDetailPanel from
// re-doing that check (and re-touching the DOM) on every subsequent poll for a panel
// that's already showing the "no longer tracked" state.
let openPanelGone = false;

// One identity field per feed, stable enough to re-find the same real-world entity
// across polls -- matches the record types in contracts.py exactly (SubwayArrival.trip_id,
// ServiceRequest.unique_key, BikeStation.station_id, Camera.id, RestaurantInspection.camis,
// BusVehicle.vehicle_id).
const DETAIL_IDENTITY = {
  subway_arrivals: (r) => r.trip_id,
  nyc_311: (r) => r.unique_key,
  citibike: (r) => r.station_id,
  dot_cameras: (r) => r.id,
  dohmh_inspections: (r) => r.camis,
  mta_bus: (r) => r.vehicle_id,
};

// Shown in place of the panel body when the tracked record has genuinely dropped out of
// the feed, instead of leaving the last-known (now-stale) render up forever.
const RECORD_GONE_MESSAGES = {
  subway_arrivals: "This train is no longer being tracked (it may have completed its run).",
  mta_bus: "This bus is no longer being tracked (it may have gone out of service).",
  citibike: "This station is no longer reporting to the Citi Bike feed.",
  dot_cameras: "This camera is no longer in the live feed.",
  dohmh_inspections: "This restaurant is no longer in the live feed.",
  nyc_311: "This 311 request is no longer in the live feed.",
};

// dohmh_inspections is the one feed above whose identity field (camis) is not 1:1 with a
// record: NYC publishes one row per violation per inspection visit, so several rows can
// share a camis. Every other feed's identity field is already unique, so this only ever
// has real work to do for that one case.
function findMatchingRecord(idFn, records, currentRecord) {
  const targetId = idFn(currentRecord);
  if (targetId == null) return null;
  const candidates = records.filter((r) => idFn(r) === targetId);
  if (!candidates.length) return null;
  if (candidates.length === 1) return candidates[0];
  return candidates.reduce((best, r) => {
    const bestTime = best.inspection_date ? Date.parse(best.inspection_date) : -Infinity;
    const time = r.inspection_date ? Date.parse(r.inspection_date) : -Infinity;
    return time > bestTime ? r : best;
  });
}

// Normalizes what a body-builder returned into panelCleanup/panelUpdate. Builders that
// predate the refresh feature (or have nothing to preserve across a refresh) return a
// bare cleanup function, same as always; builders with an async subsection worth
// preserving (cameraDetail, subwayDetail) return `{ cleanup, update }` instead.
function applyBuildResult(result) {
  if (typeof result === "function") {
    panelCleanup = result;
    panelUpdate = null;
  } else if (result && typeof result === "object") {
    panelCleanup = typeof result.cleanup === "function" ? result.cleanup : null;
    panelUpdate = typeof result.update === "function" ? result.update : null;
  } else {
    panelCleanup = null;
    panelUpdate = null;
  }
}

// Called by data-sync.js's applyEnvelope every time a feed refreshes (poll or SSE),
// once per feed, regardless of whether a panel is open. A no-op unless the open panel
// actually belongs to `feedKey`. Finds the tracked record's fresh version by identity;
// if it's still present, re-renders (via the builder's own lightweight `update`, or a
// full body rebuild if it has none); if it's gone, shows an explicit message instead of
// leaving the stale render up forever.
function refreshOpenDetailPanel(feedKey, envelope) {
  if (!openPanelFeedKey || openPanelFeedKey !== feedKey) return;
  if (openPanelGone) return;
  const idFn = DETAIL_IDENTITY[feedKey];
  if (!idFn || !openPanelRecord || !envelope || !Array.isArray(envelope.records)) return;
  const match = findMatchingRecord(idFn, envelope.records, openPanelRecord);
  if (!match) {
    showRecordGoneState(feedKey);
    return;
  }
  openPanelRecord = match;
  if (panelUpdate) {
    panelUpdate(match);
    return;
  }
  if (!openPanelBuildBody) return;
  const body = el("detail-panel-body");
  if (!body) return;
  // Full rebuild path (used by builders with no async subsections to protect): preserve
  // scroll position across the innerHTML replacement so a mid-scroll reader (e.g. the
  // inspection-history list) doesn't get yanked back to the top every poll cycle.
  const scrollTop = body.scrollTop;
  if (panelCleanup) {
    panelCleanup();
    panelCleanup = null;
  }
  applyBuildResult(openPanelBuildBody(body, match));
  body.scrollTop = scrollTop;
}

// Accessibility: this is the one live-refresh transition in this file that genuinely
// needs an announcement. The panel already carries role="dialog"/aria-modal="true"
// (openDetailPanel), so a screen-reader user knows they're inside a modal, but nothing
// tells them its content just changed out from under them -- a sighted user simply
// notices the visual swap. This is a significant, one-time state change (the record
// disappeared, openPanelGone latches it so it can only fire once per panel), the exact
// kind of event aria-live is for -- unlike the routine ~15s summary-field refresh in
// cameraDetail/subwayDetail's `update()` (panelUpdate), which is deliberately left
// without any live-region treatment: making an ETA tick "polite" would have a screen
// reader narrate "in 3 min… now in 2 min… now in 1 min" every poll cycle, the exact
// spammy result a sighted user avoids just by not staring at the panel. The record-gone
// message stays reachable on demand (it's regular panel content), it just also gets
// announced once, the moment it appears.
function showRecordGoneState(feedKey) {
  if (panelCleanup) {
    panelCleanup();
    panelCleanup = null;
  }
  panelUpdate = null;
  const body = el("detail-panel-body");
  if (body) {
    body.innerHTML = emptyStateHtml(
      "⚠️",
      RECORD_GONE_MESSAGES[feedKey] || "This item is no longer being tracked.",
      { live: true }
    );
  }
  openPanelRecord = null;
  openPanelGone = true;
}

// "Focusable" for trap purposes: only elements that are actually visible and reachable
// by Tab right now. getClientRects().length > 0 excludes anything display:none (e.g. a
// hidden camera-error placeholder), which offsetParent-based checks can miss in edge
// cases (position:fixed ancestors).
const PANEL_FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function panelFocusableElements(panel) {
  return Array.from(panel.querySelectorAll(PANEL_FOCUSABLE_SELECTOR)).filter(
    (node) => node.getClientRects().length > 0
  );
}

// A hand-rolled focus trap (no dependency for something this small): while the panel is
// open, Tab/Shift+Tab must cycle only among its own focusable elements instead of
// escaping into the map/sidebar it's visually covering. Attached once, directly on the
// panel element, so it only ever sees keydowns that bubble up from something already
// inside the panel -- it's a no-op whenever the panel is hidden.
function trapPanelFocus(ev) {
  if (ev.key !== "Tab") return;
  const panel = el("detail-panel");
  if (panel.hidden) return;
  const focusable = panelFocusableElements(panel);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  const goingBackward = ev.shiftKey;
  const atEdge = goingBackward ? active === first : active === last;
  if (atEdge || !panel.contains(active)) {
    ev.preventDefault();
    (goingBackward ? last : first).focus();
  }
}

function ensurePanelFocusTrapAttached(panel) {
  if (panelTrapAttached) return;
  panel.addEventListener("keydown", trapPanelFocus);
  panelTrapAttached = true;
}

function prefersReducedMotion() {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

// `restoreFocus: false` is for the internal call at the top of openDetailPanel, which
// closes a panel that's about to be immediately replaced by another one -- there's
// nothing to "restore" to since focus is about to move into the new panel's close
// button anyway. Every real close (Escape, the × button) uses the default of true.
function closeDetailPanel({ restoreFocus = true } = {}) {
  if (panelCleanup) {
    panelCleanup();
    panelCleanup = null;
  }
  panelUpdate = null;
  openPanelFeedKey = null;
  openPanelRecord = null;
  openPanelBuildBody = null;
  openPanelGone = false;
  if (panelCloseTimer) {
    clearTimeout(panelCloseTimer);
    panelCloseTimer = null;
  }
  const panel = el("detail-panel");
  const trigger = panelTriggerElement;
  const fallback = panelTriggerFallback;
  panelTriggerElement = null;
  panelTriggerFallback = null;
  const isUsable = (node) =>
    node && node.isConnected && typeof node.focus === "function";
  const restoreFocusIfRequested = () => {
    if (!restoreFocus) return;
    // Prefer the real trigger; fall back to whatever panelFocusFallback supplied (e.g.
    // search.js's #search-input) if the trigger has since been removed from the DOM.
    // Focusing a detached node throws in some browsers, and even where it doesn't,
    // there is nothing sensible to restore to -- this is a deliberate no-op rather
    // than forcing focus onto document.body when neither is usable.
    const target = isUsable(trigger) ? trigger : isUsable(fallback) ? fallback : null;
    if (target) target.focus({ preventScroll: true });
  };
  if (panel.hidden) {
    restoreFocusIfRequested();
    return;
  }
  if (prefersReducedMotion()) {
    panel.hidden = true;
    panel.innerHTML = "";
    restoreFocusIfRequested();
    return;
  }
  panel.classList.remove("is-open");
  panelCloseTimer = setTimeout(() => {
    panel.hidden = true;
    panel.innerHTML = "";
    panelCloseTimer = null;
  }, DETAIL_PANEL_TRANSITION_MS);
  restoreFocusIfRequested();
}

// `identity`, when supplied, is `{ feedKey, record }`: feedKey is the data-sync.js feed
// key this record belongs to (e.g. "subway_arrivals"), record is the specific record
// being shown. Together they let refreshOpenDetailPanel() find this same real-world
// entity again in a later envelope and re-render in place. Callers with nothing
// meaningful to track (there are none left -- every DETAIL_BUILDERS entry supplies one)
// may omit it, which simply disables live-refresh for that panel.
function openDetailPanel(title, buildBody, iconKey, identity) {
  closeDetailPanel({ restoreFocus: false }); // clears any previous camera refresh / in-flight fetch
  if (panelCloseTimer) {
    // The call above just scheduled a deferred hide+clear because a previous panel was
    // open (see closeDetailPanel). We're about to overwrite that panel's content
    // synchronously below, so that deferred timer must not be allowed to fire later
    // and wipe out what we're opening now.
    clearTimeout(panelCloseTimer);
    panelCloseTimer = null;
  }
  // Captured after the closeDetailPanel() call above (which never moves focus itself)
  // so this reflects whatever the user was actually interacting with when they
  // triggered *this* open, not a stale reference left over from a previous panel.
  panelTriggerElement = document.activeElement;
  panelTriggerFallback = panelFocusFallback;
  panelFocusFallback = null;
  const panel = el("detail-panel");
  ensurePanelFocusTrapAttached(panel);
  panel.hidden = false;
  panel.classList.remove("is-open");
  // role="dialog" + aria-modal="true": this panel behaves like a modal overlay (it
  // covers map/sidebar content and traps Tab focus while open), so it needs the ARIA
  // semantics that imply that to assistive tech, not just the visual styling.
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", "detail-panel-title");
  panel.innerHTML = `
    <div class="detail-panel-head">
      ${iconKey ? icon(iconKey) : ""}
      <span class="detail-panel-title" id="detail-panel-title">${title}</span>
      <button type="button" class="detail-panel-close" id="detail-panel-close" aria-label="Close">×</button>
    </div>
    <div class="detail-panel-body" id="detail-panel-body"></div>`;
  const closeBtn = el("detail-panel-close");
  closeBtn.addEventListener("click", () => closeDetailPanel());
  openPanelBuildBody = buildBody;
  openPanelFeedKey = identity ? identity.feedKey : null;
  openPanelRecord = identity ? identity.record : null;
  openPanelGone = false;
  applyBuildResult(buildBody(el("detail-panel-body"), openPanelRecord));
  // Move focus into the panel so keyboard users land somewhere useful, and so
  // Escape-to-close (wired in app.js) works immediately without an extra Tab.
  closeBtn.focus({ preventScroll: true });
  if (prefersReducedMotion()) {
    panel.classList.add("is-open");
    return;
  }
  // Two rAFs: the first lets the freshly-set `hidden = false` / class-less state
  // paint, the second then adds `is-open` so the CSS transition actually animates
  // from the closed state instead of jumping straight to open.
  requestAnimationFrame(() => requestAnimationFrame(() => panel.classList.add("is-open")));
}

function fieldsHtml(pairs) {
  return `<dl class="detail-fields">${pairs
    .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${value}</dd>`)
    .join("")}</dl>`;
}

// Shared muted-box treatment for "nothing to show" / "this failed" states across every
// detail builder, so a dead feed or an exhausted trip reads as an intentional message
// rather than a rendering bug.
//
// `live` (default false, matching every existing call site's behavior exactly) opts a
// single instance into being announced to screen readers the moment it's inserted:
// `role="status"`/`aria-live="polite"` on a *freshly-created* node is picked up by
// modern browser/AT combinations without needing a pre-existing live region in the DOM
// (the same pattern toast/alert widgets use) -- so this works even when, unlike
// #camera-density-history / #subway-stop-list below, there's no already-open wrapper
// to hang the attribute on ahead of time. Only showRecordGoneState opts in: that's a
// one-time, significant "this thing is gone" transition, not a routine content refresh,
// and every other current caller (a static empty state at open time, or content already
// inside an aria-live wrapper of its own) would get nothing but duplicate/unwanted
// announcements from turning this on by default.
function emptyStateHtml(icon, message, { live = false } = {}) {
  const liveAttrs = live ? ' role="status" aria-live="polite"' : "";
  return `<div class="detail-empty-state"${liveAttrs}>
    <span class="detail-empty-icon" aria-hidden="true">${icon}</span>
    <p>${escapeHtml(message)}</p>
  </div>`;
}

const SUBWAY_SKELETON_ROWS = 5;

// A handful of shimmering placeholder rows, previewing the shape of the stop list
// that's about to load, instead of a bare "loading…" string.
function subwaySkeletonHtml() {
  const row = '<li class="skeleton-row"><span class="skeleton-chip"></span><span class="skeleton-chip skeleton-chip-narrow"></span></li>';
  return `<ul class="detail-stop-list detail-skeleton-list">${row.repeat(SUBWAY_SKELETON_ROWS)}</ul>`;
}

const CAMERA_REFRESH_MS = 3000;
const CAMERA_DENSITY_WINDOW_S = 3600;
const DENSITY_CHART_WIDTH = 280;
const DENSITY_CHART_HEIGHT = 56;
const DENSITY_CHART_PADDING = 4;

// One-shot fetch of an hour of aggregated person/vehicle counts for a single camera.
// No polling: the chart reflects "the last hour as of when you opened this panel".
function fetchCameraDensityHistory(cameraId, windowS) {
  const url = `/api/camera_density_history?camera_id=${encodeURIComponent(cameraId)}&window_s=${windowS}`;
  return fetch(url, { headers: { accept: "application/json" } }).then((r) => r.json());
}

// Hand-rolled sparkline: two <polyline>s (person, vehicle) plotted on a shared y-scale
// against window_start order. No charting library -- this project doesn't have one and
// isn't adding one for a single small trend line.
function densityHistoryHtml(records) {
  const w = DENSITY_CHART_WIDTH;
  const h = DENSITY_CHART_HEIGHT;
  const pad = DENSITY_CHART_PADDING;
  const maxValue = Math.max(1, ...records.map((r) => Math.max(r.person_mean, r.vehicle_mean)));
  const toPoints = (key) =>
    records
      .map((r, i) => {
        const x = records.length > 1 ? pad + (i / (records.length - 1)) * (w - pad * 2) : w / 2;
        const y = h - pad - (r[key] / maxValue) * (h - pad * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  return `<div class="density-chart-wrap">
    <svg class="density-chart" viewBox="0 0 ${w} ${h}" role="img"
         aria-label="Person and vehicle counts over the last hour">
      <polyline points="${toPoints("vehicle_mean")}" fill="none" stroke="var(--stale)"
                stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
      <polyline points="${toPoints("person_mean")}" fill="none" stroke="var(--text)"
                stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
    </svg>
    <div class="density-chart-legend">
      <span class="density-legend-item"><span class="density-legend-swatch density-legend-person"></span>person</span>
      <span class="density-legend-item"><span class="density-legend-swatch density-legend-vehicle"></span>vehicle</span>
    </div>
  </div>`;
}

// The summary strip at the top of cameraDetail's panel -- the only part that needs to
// change on a refresh (is_online / roadway / direction / area can all shift feed to
// feed). Kept as its own function so both the initial build and the lightweight
// `update()` below render it identically.
function cameraSummaryFieldsHtml(camera) {
  return fieldsHtml([
    ["Status", camera.is_online ? "online" : "offline"],
    ["Roadway", escapeHtml(camera.roadway || "—")],
    ["Direction", escapeHtml(camera.direction || "—")],
    ["Area", escapeHtml(camera.area || "—")],
  ]);
}

function cameraDetail(camera) {
  openDetailPanel(
    escapeHtml(camera.name),
    (body, initialCamera) => {
      body.innerHTML =
        `<div id="camera-summary-fields">${cameraSummaryFieldsHtml(initialCamera)}</div>` +
        `<p class="detail-subhead">
           Live view
           <span class="live-badge" id="camera-live-badge" hidden>
             <span class="live-dot" aria-hidden="true"></span>LIVE
           </span>
         </p>
         <div class="camera-live">
           <img id="camera-live-img" alt="Live view of ${escapeHtml(camera.name)}" hidden />
           <div class="camera-live-error" id="camera-live-error" hidden>
             ${emptyStateHtml("📷", "Live image is unavailable right now.")}
           </div>
         </div>
         <p class="detail-subhead">${icon("chart")} Density (last hour)</p>
         <div id="camera-density-history" class="detail-loading" aria-busy="true" aria-live="polite">
           <span class="detail-sr-only">Loading density history…</span>
         </div>`;
      const img = body.querySelector("#camera-live-img");
      const errNode = body.querySelector("#camera-live-error");
      const liveBadge = body.querySelector("#camera-live-badge");
      img.onerror = () => {
        img.hidden = true;
        errNode.hidden = false;
        liveBadge.hidden = true;
      };
      const refresh = () => {
        img.hidden = false;
        errNode.hidden = true;
        liveBadge.hidden = false;
        img.src = `${camera.image_url}?_ts=${Date.now()}`;
      };
      refresh();
      let timer = setInterval(refresh, CAMERA_REFRESH_MS);
      // Good-network-citizen behavior: nyctmc.org does not rate-limit this endpoint
      // itself, so a hidden background tab still polling every 3s is wasted load with
      // no one watching. Pause while hidden, refresh immediately on return so the image
      // isn't stale the moment the user looks back.
      const onVisibilityChange = () => {
        if (document.visibilityState === "hidden") {
          clearInterval(timer);
        } else {
          refresh();
          timer = setInterval(refresh, CAMERA_REFRESH_MS);
        }
      };
      document.addEventListener("visibilitychange", onVisibilityChange);

      const chartNode = body.querySelector("#camera-density-history");
      let chartCancelled = false;
      fetchCameraDensityHistory(camera.id, CAMERA_DENSITY_WINDOW_S)
        .then((env) => {
          if (chartCancelled) return;
          chartNode.removeAttribute("aria-busy");
          if (env.status === "error" || !env.records.length) {
            const message =
              (env.error && env.error.message) || "No density data for this camera yet.";
            chartNode.innerHTML = emptyStateHtml(icon("chart"), message);
            return;
          }
          chartNode.innerHTML = densityHistoryHtml(env.records);
        })
        .catch((err) => {
          if (chartCancelled) return;
          chartNode.removeAttribute("aria-busy");
          chartNode.innerHTML = emptyStateHtml(
            icon("alert"),
            `Could not load density history: ${err.message}`
          );
        });

      return {
        cleanup: () => {
          chartCancelled = true;
          clearInterval(timer);
          document.removeEventListener("visibilitychange", onVisibilityChange);
        },
        // Refresh path: only the summary strip is rebuilt. The live image poll and the
        // one-shot density-history fetch above are intentionally left running
        // untouched -- restarting them every ~15s poll cycle would re-flash the image
        // and the chart's loading skeleton for no reason, since neither depends on
        // anything in the fresh envelope beyond identity (already unchanged).
        update: (freshCamera) => {
          const fieldsNode = body.querySelector("#camera-summary-fields");
          if (fieldsNode) fieldsNode.innerHTML = cameraSummaryFieldsHtml(freshCamera);
        },
      };
    },
    "dot_cameras",
    { feedKey: "dot_cameras", record: camera }
  );
}

const INSPECTION_HISTORY_LIMIT = 8;
const INSPECTION_VIOLATION_TRUNCATE_LENGTH = 90;

// NYC publishes one row per violation per inspection, so the same `camis` (restaurant
// id) commonly recurs across the already-fetched dohmh_inspections records -- once for
// each violation on each visit. This pulls every OTHER row for the clicked restaurant
// out of that already-loaded set (no new fetch: the data is already in `state`),
// excluding the clicked record itself by identity, newest inspection_date first.
function otherInspectionsForCamis(record) {
  const entry = state.get("dohmh_inspections");
  const records = (entry && entry.envelope && entry.envelope.records) || [];
  return records
    .filter((r) => r !== record && r.camis === record.camis)
    .sort((a, b) => {
      const aTime = a.inspection_date ? Date.parse(a.inspection_date) : 0;
      const bTime = b.inspection_date ? Date.parse(b.inspection_date) : 0;
      return bTime - aTime;
    });
}

function truncate(text, maxLength) {
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 1).trimEnd()}…`;
}

// hhmmss() (utils.js) renders time-of-day only, which is right for "Inspected" up top
// (a single recent timestamp) but useless for telling apart history rows that are
// often months or years apart. This renders the calendar date instead.
function inspectionHistoryDate(iso) {
  if (!iso) return "unknown date";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString();
}

// Same visual treatment as the subway stop list (.detail-stop-list), so this reads as
// one consistent "detail list" pattern across layers rather than a bespoke table.
function inspectionHistoryHtml(others) {
  if (!others.length) {
    return emptyStateHtml(icon("chart"), "No other inspections on file.");
  }
  const shown = others.slice(0, INSPECTION_HISTORY_LIMIT);
  const rows = shown
    .map((r) => {
      const when = inspectionHistoryDate(r.inspection_date);
      const grade = escapeHtml(r.grade || "ungraded");
      const score = r.score != null ? r.score : "—";
      const violation = escapeHtml(
        truncate(r.violation_description || "no violation recorded", INSPECTION_VIOLATION_TRUNCATE_LENGTH)
      );
      return `<li>
        <div class="inspection-history-row-head">
          <span>${when}</span>
          <span>grade ${grade} · score ${score}</span>
        </div>
        <p class="inspection-history-violation">${violation}</p>
      </li>`;
    })
    .join("");
  const omitted = others.length - shown.length;
  const note =
    omitted > 0
      ? `<p class="detail-subhead detail-subhead-note">+${omitted} more not shown</p>`
      : "";
  return `<ul class="detail-stop-list inspection-history-list">${rows}</ul>${note}`;
}

// This is a plain full-body rebuild on refresh (no async subsection to protect, see
// refreshOpenDetailPanel's fallback path), so `body`/`r` here can be either the initial
// open-time record or a fresh one found by identity (camis) in a later envelope.
function inspectionDetail(record) {
  openDetailPanel(
    escapeHtml(record.dba || "Unnamed restaurant"),
    (body, r) => {
      const others = otherInspectionsForCamis(r);
      body.innerHTML =
        fieldsHtml([
          ["Cuisine", escapeHtml(r.cuisine || "—")],
          ["Grade", escapeHtml(r.grade || "ungraded")],
          ["Score", r.score != null ? r.score : "—"],
          ["Inspected", r.inspection_date ? inspectionHistoryDate(r.inspection_date) : "—"],
          ["Latest violation", escapeHtml(r.violation_description || "none recorded")],
        ]) +
        `<p class="detail-subhead">${icon("chart")} Inspection history</p>
         ${inspectionHistoryHtml(others)}`;
    },
    "dohmh_inspections",
    { feedKey: "dohmh_inspections", record }
  );
}

function service311Detail(record) {
  openDetailPanel(
    escapeHtml(record.complaint_type || "311 request"),
    (body, r) => {
      body.innerHTML = fieldsHtml([
        ["Descriptor", escapeHtml(r.descriptor || "—")],
        ["Agency", escapeHtml(r.agency || "—")],
        ["Status", escapeHtml(r.status || "—")],
        ["Borough", escapeHtml(r.borough || "—")],
        ["Address", escapeHtml(r.incident_address || "—")],
        ["Created", r.created_at ? hhmmss(r.created_at) : "—"],
      ]);
    },
    "nyc_311",
    { feedKey: "nyc_311", record }
  );
}

function bikeDetail(record) {
  openDetailPanel(
    escapeHtml(record.name),
    (body, r) => {
      const bikes =
        r.ebikes_available != null
          ? `${r.bikes_available} (${r.ebikes_available} e-bikes)`
          : `${r.bikes_available}`;
      body.innerHTML = fieldsHtml([
        ["Bikes", bikes],
        ["Docks", r.docks_available],
        ["Capacity", r.capacity != null ? r.capacity : "—"],
        ["Renting", r.is_renting ? "yes" : "no"],
        ["Returning", r.is_returning ? "yes" : "no"],
        ["Last reported", r.last_reported ? hhmmss(r.last_reported) : "—"],
      ]);
    },
    "citibike",
    { feedKey: "citibike", record }
  );
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

// The summary strip at the top of subwayDetail's panel -- trip/direction stay fixed for
// a given trip_id, but "Next stop"'s ETA is the whole reason this file exists: it must
// keep counting down (or jump to the next station) as the feed refreshes, not freeze at
// whatever it read when the panel opened.
function subwaySummaryFieldsHtml(record) {
  return fieldsHtml([
    ["Trip", escapeHtml(record.trip_id)],
    ["Direction", escapeHtml(record.direction || "—")],
    [
      "Next stop",
      `${escapeHtml(record.stop_name || record.stop_id)} in ${Math.round(record.eta_s / 60)} min`,
    ],
  ]);
}

function subwayDetail(record) {
  openDetailPanel(
    `${escapeHtml(record.route_id)} train`,
    (body, initialRecord) => {
      body.innerHTML =
        `<div id="subway-summary-fields">${subwaySummaryFieldsHtml(initialRecord)}</div>` +
        `<p class="detail-subhead">Full stop list</p>
         <div id="subway-stop-list" class="detail-loading" aria-busy="true" aria-live="polite">
           ${subwaySkeletonHtml()}
           <span class="detail-sr-only">Loading full stop list…</span>
         </div>`;
      const listNode = body.querySelector("#subway-stop-list");
      let cancelled = false;
      Promise.all([loadSubwayTrips(), loadSubwayStops()])
        .then(([trips, stops]) => {
          if (cancelled) return;
          listNode.removeAttribute("aria-busy");
          const trip = trips.get(initialRecord.trip_id);
          if (!trip || !trip.stop_times.length) {
            listNode.innerHTML = emptyStateHtml(
              "🚇",
              trip
                ? "No stop times reported for this trip."
                : "This train's raw trip data is no longer available (it may have completed its run)."
            );
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
          if (cancelled) return;
          listNode.removeAttribute("aria-busy");
          listNode.innerHTML = emptyStateHtml("⚠️", `Could not load the full stop list: ${err.message}`);
        });
      return {
        cleanup: () => {
          cancelled = true;
        },
        // Refresh path: only the ETA/next-stop summary is rebuilt. The full stop list
        // is keyed on trip_id alone (which never changes for the same trip) and was
        // already fetched once above, so re-running that fetch and re-flashing its
        // loading skeleton every ~15s poll cycle would be pure waste.
        update: (freshRecord) => {
          const fieldsNode = body.querySelector("#subway-summary-fields");
          if (fieldsNode) fieldsNode.innerHTML = subwaySummaryFieldsHtml(freshRecord);
        },
      };
    },
    "subway",
    { feedKey: "subway_arrivals", record }
  );
}

// Only the fields BusVehicle actually reports get a row: the SIRI MonitoredCall /
// Occupancy fields on the contract are optional and absent on vehicles that aren't
// currently monitored (see the contract's own docstring) -- that's expected, not a
// bug, so a missing next-stop or occupancy value is omitted rather than shown as "—".
// This is a plain full-body rebuild on refresh (no async subsection here to protect),
// so the fields list is computed fresh from whichever record (initial or refreshed) is
// passed in, fixing the same "next_stop_eta frozen forever" bug as subwayDetail above.
function busDetail(record) {
  const routeLabel = escapeHtml(busRouteLabel(record.route_id));
  openDetailPanel(
    record.route_id ? `${routeLabel} bus` : "Bus",
    (body, r) => {
      const fields = [["Route", r.route_id ? escapeHtml(busRouteLabel(r.route_id)) : "—"]];
      if (r.next_stop_name) {
        const eta = r.next_stop_eta ? ` · ${hhmmss(r.next_stop_eta)}` : "";
        fields.push(["Next stop", `${escapeHtml(r.next_stop_name)}${eta}`]);
      }
      if (r.stops_away != null) fields.push(["Stops away", r.stops_away]);
      if (r.occupancy) fields.push(["Occupancy", escapeHtml(r.occupancy)]);
      if (r.bearing != null) fields.push(["Bearing", `${Math.round(r.bearing)}°`]);
      body.innerHTML = fieldsHtml(fields);
    },
    "bus",
    { feedKey: "mta_bus", record }
  );
}

const DETAIL_BUILDERS = {
  subway: subwayDetail,
  nyc311: service311Detail,
  citibike: bikeDetail,
  cameras: cameraDetail,
  dohmh: inspectionDetail,
  bus: busDetail,
};

function handleMapClick(info) {
  if (!info || !info.object || !info.layer) return;
  const builder = DETAIL_BUILDERS[info.layer.id];
  if (builder) builder(info.object);
}
