/*
 * The click-detail panel: one persistent panel, reused by every clickable layer
 * (subway, 311, Citi Bike, DOT cameras, restaurant inspections) instead of five bespoke
 * UIs. `openDetailPanel` owns the panel's DOM; each layer's `*Detail` function only
 * supplies a title and a function that fills in the body. That body-builder may return
 * a cleanup function (clearing a setInterval, cancelling a fetch) which runs when the
 * panel is closed or replaced.
 *
 * Depends on: utils.js (el, escapeHtml, hhmmss), map-layers.js (busRouteLabel).
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

function openDetailPanel(title, buildBody, iconKey) {
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
      ${iconKey ? icon(iconKey) : ""}
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

function cameraDetail(camera) {
  openDetailPanel(
    escapeHtml(camera.name),
    (body) => {
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

      return () => {
        chartCancelled = true;
        clearInterval(timer);
        document.removeEventListener("visibilitychange", onVisibilityChange);
      };
    },
    "dot_cameras"
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

function inspectionDetail(record) {
  openDetailPanel(
    escapeHtml(record.dba || "Unnamed restaurant"),
    (body) => {
      const others = otherInspectionsForCamis(record);
      body.innerHTML =
        fieldsHtml([
          ["Cuisine", escapeHtml(record.cuisine || "—")],
          ["Grade", escapeHtml(record.grade || "ungraded")],
          ["Score", record.score != null ? record.score : "—"],
          ["Inspected", record.inspection_date ? inspectionHistoryDate(record.inspection_date) : "—"],
          ["Latest violation", escapeHtml(record.violation_description || "none recorded")],
        ]) +
        `<p class="detail-subhead">${icon("chart")} Inspection history</p>
         ${inspectionHistoryHtml(others)}`;
    },
    "dohmh_inspections"
  );
}

function service311Detail(record) {
  openDetailPanel(
    escapeHtml(record.complaint_type || "311 request"),
    (body) => {
      body.innerHTML = fieldsHtml([
        ["Descriptor", escapeHtml(record.descriptor || "—")],
        ["Agency", escapeHtml(record.agency || "—")],
        ["Status", escapeHtml(record.status || "—")],
        ["Borough", escapeHtml(record.borough || "—")],
        ["Address", escapeHtml(record.incident_address || "—")],
        ["Created", record.created_at ? hhmmss(record.created_at) : "—"],
      ]);
    },
    "nyc_311"
  );
}

function bikeDetail(record) {
  openDetailPanel(
    escapeHtml(record.name),
    (body) => {
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
    },
    "citibike"
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

function subwayDetail(record) {
  openDetailPanel(
    `${escapeHtml(record.route_id)} train`,
    (body) => {
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
    },
    "subway"
  );
}

// Only the fields BusVehicle actually reports get a row: the SIRI MonitoredCall /
// Occupancy fields on the contract are optional and absent on vehicles that aren't
// currently monitored (see the contract's own docstring) -- that's expected, not a
// bug, so a missing next-stop or occupancy value is omitted rather than shown as "—".
function busDetail(record) {
  const routeLabel = escapeHtml(busRouteLabel(record.route_id));
  const fields = [["Route", record.route_id ? routeLabel : "—"]];
  if (record.next_stop_name) {
    const eta = record.next_stop_eta ? ` · ${hhmmss(record.next_stop_eta)}` : "";
    fields.push(["Next stop", `${escapeHtml(record.next_stop_name)}${eta}`]);
  }
  if (record.stops_away != null) fields.push(["Stops away", record.stops_away]);
  if (record.occupancy) fields.push(["Occupancy", escapeHtml(record.occupancy)]);
  if (record.bearing != null) fields.push(["Bearing", `${Math.round(record.bearing)}°`]);
  openDetailPanel(
    record.route_id ? `${routeLabel} bus` : "Bus",
    (body) => {
      body.innerHTML = fieldsHtml(fields);
    },
    "bus"
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
