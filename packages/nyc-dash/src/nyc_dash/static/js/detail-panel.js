/*
 * The click-detail panel: one persistent panel, reused by every clickable layer
 * (subway, 311, Citi Bike, DOT cameras, restaurant inspections) instead of five bespoke
 * UIs. `openDetailPanel` owns the panel's DOM; each layer's `*Detail` function only
 * supplies a title and a function that fills in the body. That body-builder may return
 * a cleanup function (clearing a setInterval, cancelling a fetch) which runs when the
 * panel is closed or replaced.
 *
 * Depends on: utils.js (el, escapeHtml, hhmmss).
 */

// Kept in sync with the CSS transition-duration on .detail-panel below (detail-panel.css).
const DETAIL_PANEL_TRANSITION_MS = 180;

let panelCleanup = null;
// The close animation's pending "actually hide it now" timeout. Tracked so a second
// open/close arriving before the first close's transition finishes cancels the stale
// timer instead of letting it hide a panel that was just reopened.
let panelCloseTimer = null;

function prefersReducedMotion() {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function closeDetailPanel() {
  if (panelCleanup) {
    panelCleanup();
    panelCleanup = null;
  }
  if (panelCloseTimer) {
    clearTimeout(panelCloseTimer);
    panelCloseTimer = null;
  }
  const panel = el("detail-panel");
  if (panel.hidden) return;
  if (prefersReducedMotion()) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  panel.classList.remove("is-open");
  panelCloseTimer = setTimeout(() => {
    panel.hidden = true;
    panel.innerHTML = "";
    panelCloseTimer = null;
  }, DETAIL_PANEL_TRANSITION_MS);
}

function openDetailPanel(title, buildBody) {
  closeDetailPanel(); // clears any previous camera refresh / in-flight fetch
  if (panelCloseTimer) {
    // The call above just scheduled a deferred hide+clear because a previous panel was
    // open (see closeDetailPanel). We're about to overwrite that panel's content
    // synchronously below, so that deferred timer must not be allowed to fire later
    // and wipe out what we're opening now.
    clearTimeout(panelCloseTimer);
    panelCloseTimer = null;
  }
  const panel = el("detail-panel");
  panel.hidden = false;
  panel.classList.remove("is-open");
  panel.innerHTML = `
    <div class="detail-panel-head">
      <span class="detail-panel-title">${title}</span>
      <button type="button" class="detail-panel-close" id="detail-panel-close" aria-label="Close">×</button>
    </div>
    <div class="detail-panel-body" id="detail-panel-body"></div>`;
  const closeBtn = el("detail-panel-close");
  closeBtn.addEventListener("click", closeDetailPanel);
  panelCleanup = buildBody(el("detail-panel-body")) || null;
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
function emptyStateHtml(icon, message) {
  return `<div class="detail-empty-state">
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

function cameraDetail(camera) {
  openDetailPanel(escapeHtml(camera.name), (body) => {
    body.innerHTML =
      fieldsHtml([
        ["Status", camera.is_online ? "online" : "offline"],
        ["Roadway", escapeHtml(camera.roadway || "—")],
        ["Direction", escapeHtml(camera.direction || "—")],
        ["Area", escapeHtml(camera.area || "—")],
      ]) +
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
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
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
        const trip = trips.get(record.trip_id);
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
