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

let panelCleanup = null;

function closeDetailPanel() {
  if (panelCleanup) {
    panelCleanup();
    panelCleanup = null;
  }
  const panel = el("detail-panel");
  panel.hidden = true;
  panel.innerHTML = "";
}

function openDetailPanel(title, buildBody) {
  closeDetailPanel(); // also clears any previous camera refresh / in-flight fetch
  const panel = el("detail-panel");
  panel.hidden = false;
  panel.innerHTML = `
    <div class="detail-panel-head">
      <span class="detail-panel-title">${title}</span>
      <button type="button" class="detail-panel-close" id="detail-panel-close" aria-label="Close">×</button>
    </div>
    <div class="detail-panel-body" id="detail-panel-body"></div>`;
  el("detail-panel-close").addEventListener("click", closeDetailPanel);
  panelCleanup = buildBody(el("detail-panel-body")) || null;
}

function fieldsHtml(pairs) {
  return `<dl class="detail-fields">${pairs
    .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${value}</dd>`)
    .join("")}</dl>`;
}

function cameraDetail(camera) {
  openDetailPanel(escapeHtml(camera.name), (body) => {
    body.innerHTML =
      fieldsHtml([
        ["Status", camera.is_online ? "online" : "offline"],
        ["Roadway", escapeHtml(camera.roadway || "—")],
        ["Direction", escapeHtml(camera.direction || "—")],
        ["Area", escapeHtml(camera.area || "—")],
      ]) +
      `<p class="detail-subhead">Live view</p>
       <div class="camera-live">
         <img id="camera-live-img" alt="Live view of ${escapeHtml(camera.name)}" hidden />
         <p class="camera-live-error" id="camera-live-error" hidden>
           Live image is unavailable right now.
         </p>
       </div>`;
    const img = body.querySelector("#camera-live-img");
    const errNode = body.querySelector("#camera-live-error");
    img.onerror = () => {
      img.hidden = true;
      errNode.hidden = false;
    };
    const refresh = () => {
      img.hidden = false;
      errNode.hidden = true;
      img.src = `${camera.image_url}?_ts=${Date.now()}`;
    };
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
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
       <div id="subway-stop-list" class="detail-loading">loading full stop list…</div>`;
    const listNode = body.querySelector("#subway-stop-list");
    let cancelled = false;
    Promise.all([loadSubwayTrips(), loadSubwayStops()])
      .then(([trips, stops]) => {
        if (cancelled) return;
        const trip = trips.get(record.trip_id);
        if (!trip || !trip.stop_times.length) {
          listNode.textContent = trip
            ? "No stop times reported for this trip."
            : "This train's raw trip data is no longer available (it may have completed its run).";
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
        if (!cancelled) listNode.textContent = `Could not load the full stop list: ${err.message}`;
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
