/*
 * The sidebar: the per-feed layer list (checkbox + status pill + detail line) and the
 * header's health/weather badges. Everything here reads envelope.status and nothing
 * else -- see the degradation rules in app.js.
 *
 * Depends on: utils.js (el, hhmmss), state.js (state), map-layers.js (FEEDS, renderLayers).
 */

// Pictogram + accent color for each layer, matching that layer's actual map marker
// color (see map-layers.js's ROUTE_COLORS/STATUS_CRITICAL/SEQUENTIAL_BLUE_*/GRADE_*)
// so the list reads as "this is a train/bike/camera/etc." at a glance, not just a
// color chip. Deliberately a local lookup keyed by feed.key rather than a new field
// on FEEDS -- map-layers.js is owned by another agent right now.
//
// density and citibike are continuous ramps on the map (heatmap warm ramp, blue
// fill-level ramp) and dohmh_inspections is a categorical A/B/C grade -- there's no
// single "the" color for any of those, so each picks one representative stop from
// that same ramp/palette rather than inventing a new hue.
const LAYER_ICON = {
  subway_arrivals: { icon: "subway", color: "#f4d35e" }, // ROUTE_COLORS fallback
  density: { icon: "density", color: "#f2994a" }, // mid-stop of the heatmap's warm ramp
  nyc_311: { icon: "nyc_311", color: "#d03b3b" }, // STATUS_CRITICAL
  citibike: { icon: "citibike", color: "#104281" }, // SEQUENTIAL_BLUE_DARK, near-full
  dot_cameras: { icon: "dot_cameras", color: "#898781" }, // MUTED_INK
  dohmh_inspections: { icon: "dohmh_inspections", color: "#2ecc71" }, // GRADE_A
  mta_bus: { icon: "bus", color: "#f4d35e" }, // ROUTE_COLORS fallback, same as subway_arrivals
};

function layerIconHtml(key) {
  const spec = LAYER_ICON[key];
  if (!spec) return "";
  return icon(spec.icon, `layer-marker layer-marker-${key}`);
}

function buildPanel() {
  const list = el("layers");
  for (const feed of FEEDS) {
    const li = document.createElement("li");
    li.id = `layer-${feed.key}`;
    li.innerHTML = `
      <div class="layer-head">
        ${layerIconHtml(feed.key)}
        <input type="checkbox" id="toggle-${feed.key}" ${feed.defaultVisible ? "checked" : ""} />
        <label for="toggle-${feed.key}">${feed.label}</label>
        <span class="pill" id="pill-${feed.key}" data-status="loading">loading…</span>
      </div>
      <div class="detail" id="detail-${feed.key}"></div>`;
    list.appendChild(li);
    state.set(feed.key, { envelope: null, visible: feed.defaultVisible });
    el(`toggle-${feed.key}`).addEventListener("change", (ev) => {
      state.get(feed.key).visible = ev.target.checked;
      renderLayers();
    });
    // Flash animation is CSS-driven (see .pill.flash in layers-panel.css); this just
    // clears the class once it finishes so the same pill can flash again later.
    el(`pill-${feed.key}`).addEventListener("animationend", (ev) => {
      if (ev.animationName === "pill-flash") ev.target.classList.remove("flash");
    });
  }
  collapseLegend();
}

// The legend is useful once, then mostly consumes vertical space -- fold it into a
// native <details>/<summary> so it collapses without any extra JS state to track.
// This only rearranges the existing #panel DOM that index.html already rendered
// (moves the live heading/list nodes); it does not touch index.html or recreate
// their content, since that file is out of scope here.
function collapseLegend() {
  const heading = Array.from(document.querySelectorAll("#panel h2")).find(
    (h) => h.textContent.trim() === "Legend"
  );
  const legend = document.querySelector("#panel .legend");
  if (!heading || !legend || heading.nextElementSibling !== legend) return;
  const details = document.createElement("details");
  details.id = "legend-details";
  details.open = true;
  const summary = document.createElement("summary");
  summary.textContent = heading.textContent;
  details.append(summary, legend);
  heading.replaceWith(details);
}

// Pulses a pill briefly when its status category actually flips (fresh<->stale<->error),
// not on every poll tick that merely refreshes the same status with new numbers.
// See .pill.flash in layers-panel.css, which no-ops under prefers-reduced-motion.
function flashPill(pill) {
  pill.classList.remove("flash");
  void pill.offsetWidth; // restart the animation if a flash is already mid-flight
  pill.classList.add("flash");
}

// Plain-language summary for the ErrorKinds whose raw message tends to carry upstream
// jargon (dataset ids, SQL-shaped filters, raw HTTP bodies) -- the raw message moves to
// the element's title instead (still "surfaced in the UI", just not the primary text).
// NOT_CONFIGURED and INTERNAL are deliberately absent: their messages in this codebase
// are already written as plain English (e.g. density's "run `just vision run`" hint),
// so summarizing them would throw away real information for no readability gain.
const ERROR_KIND_SUMMARY = {
  upstream_http: "upstream rejected the request",
  upstream_timeout: "upstream is slow to respond",
  upstream_parse: "upstream data looks wrong right now",
  rate_limited: "rate-limited by the upstream",
  not_found: "upstream endpoint not found",
};

function setPill(key, envelope) {
  const pill = el(`pill-${key}`);
  const detail = el(`detail-${key}`);
  if (!pill) return;
  const feed = FEEDS.find((f) => f.key === key);
  const previousStatus = pill.dataset.status;
  pill.dataset.status = envelope.status;
  detail.dataset.status = envelope.status;
  if (previousStatus && previousStatus !== "loading" && previousStatus !== envelope.status) {
    flashPill(pill);
  }
  if (envelope.status === "error") {
    pill.textContent = "error";
    const err = envelope.error;
    const summary = err && ERROR_KIND_SUMMARY[err.kind];
    detail.textContent = summary || (err ? err.message : "feed unavailable");
    detail.title = summary && err ? err.message : "";
    return;
  }
  const counted = feed && feed.count ? feed.count(envelope) : `${envelope.records.length} records`;
  if (envelope.status === "stale") {
    pill.textContent = "stale";
    const err = envelope.error;
    const summary = err && ERROR_KIND_SUMMARY[err.kind];
    const why = err ? ` · ${summary || err.message}` : "";
    detail.textContent = `last good ${hhmmss(envelope.fetched_at)} · ${counted}${why}`;
    detail.title = summary && err ? err.message : "";
    return;
  }
  detail.title = "";
  pill.textContent = "fresh";
  detail.textContent = `${counted} · ${hhmmss(envelope.fetched_at)}`;
}

function applyHealth(payload) {
  const counts = { fresh: 0, stale: 0, error: 0, never_fetched: 0 };
  for (const feed of payload.feeds || []) {
    counts[feed.status] = (counts[feed.status] || 0) + 1;
  }
  const badge = el("health-badge");
  const bad = counts.error;
  badge.dataset.status = bad ? (counts.fresh ? "stale" : "error") : "fresh";
  badge.textContent = `feeds: ${counts.fresh} fresh · ${counts.stale} stale · ${bad} error`;
  badge.title = (payload.feeds || [])
    .map((f) => `${f.feed}: ${f.status}${f.last_error ? ` (${f.last_error.message})` : ""}`)
    .join("\n");
}

function applyWeather(envelope) {
  const badge = el("weather-badge");
  badge.dataset.status = envelope.status;
  if (envelope.status === "error") {
    badge.textContent = "weather unavailable";
    badge.title = envelope.error ? envelope.error.message : "";
    return;
  }
  const report = envelope.records[0];
  if (!report) {
    badge.textContent = "weather: no station reported";
    badge.title = "";
    return;
  }
  const obs = report.observation || {};
  const temp = obs.temperature_c == null ? "—" : `${Math.round(obs.temperature_c)}°C`;
  const text = obs.text ? ` ${obs.text}` : "";
  const suffix =
    envelope.status === "stale" ? ` · last good ${hhmmss(envelope.fetched_at)}` : "";
  badge.textContent = `${temp}${text} · ${report.station_name}${suffix}`;
  badge.title =
    `observed ${hhmmss(obs.observed_at)}` +
    (envelope.error ? `\n${envelope.error.message}` : "");
}
