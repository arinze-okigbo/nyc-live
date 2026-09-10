/*
 * The sidebar: the per-feed layer list (checkbox + status pill + detail line) and the
 * header's health/weather badges. Everything here reads envelope.status and nothing
 * else -- see the degradation rules in app.js.
 *
 * Depends on: utils.js (el, hhmmss, escapeHtml), state.js (state), map-layers.js (FEEDS,
 * renderLayers).
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
  ny511_events: { icon: "incident", color: "#d97f24" }, // GRADE_B amber, the "minor" step
  mta_elevator_outages: { icon: "elevator", color: "#ef476f" }, // GRADE_C
  // --muted slate: a modelled atmospheric value, deliberately not borrowing a status
  // hue, since the marker colour already carries the AQI band on the map.
  air_quality: { icon: "air_quality", color: "#8d99ae" },
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
  // Alert urgency is computed over every configured station's WeatherReport, not just
  // the one below that drives the badge's temperature text -- see applyWeatherAlertSeverity's
  // own comment for why (JFK can have a live rip-current statement while the badge is
  // otherwise showing Central Park's clear skies, and that must still be visible).
  applyWeatherAlertSeverity(envelope.records);
  if (envelope.status === "error") {
    badge.textContent = "weather unavailable";
    badge.title = envelope.error ? envelope.error.message : "";
    renderWeatherForecast([]);
    renderWeatherAlerts([]);
    return;
  }
  const report = envelope.records[0];
  if (!report) {
    badge.textContent = "weather: no station reported";
    badge.title = "";
    renderWeatherForecast([]);
    renderWeatherAlerts([]);
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
    (envelope.error ? `\n${envelope.error.message}` : "") +
    alertTitleSuffix(envelope.records);
  renderWeatherForecast(report.forecast);
  // Unlike the forecast (primary station only, matches the badge's own temperature),
  // alerts are collected across every record in the envelope -- see comment above.
  renderWeatherAlerts(envelope.records);
}

// -- Weather forecast + alerts popover ---------------------------------------
//
// The weather badge only has room for the current observation, but the backend
// already fetches a fuller NWS forecast (`report.forecast`: a handful of upcoming
// named periods, e.g. "Tonight"/"Tomorrow", each with its own temperature/sky/precip/
// wind) that was previously fetched and silently discarded. Surfaced here as a small
// click-to-toggle popover anchored under the badge, not the badge's `title` tooltip:
// a native tooltip can't hold several periods' worth of distinct fields without
// collapsing into a hard-to-scan wall of plain text, can't be dismissed or reached
// without hovering (awkward on touch), and every other "more detail" affordance in
// this app (the click-detail panel) is already click-driven for the same reasons.
// This popover is self-contained here rather than routed through detail-panel.js,
// which is out of scope for this file and owned/being edited elsewhere right now.
//
// Since extended to also list active `WeatherReport.alerts` (real NWS alerts, e.g. a
// rip current statement) in their own section above the forecast periods -- see the
// "Weather alerts" block further down for the severity-badge and aggregation logic.
const FORECAST_PERIODS_SHOWN = 3;

let weatherForecastPeriods = [];
let weatherAlertEntries = [];
let weatherPopoverEl = null;
let weatherPopoverResizeHandler = null;

function periodTempLabel(period) {
  return period.temperature_c == null ? "—" : `${Math.round(period.temperature_c)}°C`;
}

function weatherPeriodHtml(period) {
  const precip =
    period.precip_probability_pct == null
      ? ""
      : ` · ${Math.round(period.precip_probability_pct)}% precip`;
  const wind = period.wind_speed ? ` · ${escapeHtml(period.wind_speed)} wind` : "";
  return `<li>
    <div class="weather-period-head">
      <span class="weather-period-name">${escapeHtml(period.name)}</span>
      <span class="weather-period-temp">${periodTempLabel(period)}</span>
    </div>
    <p class="weather-period-detail">${escapeHtml(period.short_forecast)}${precip}${wind}</p>
  </li>`;
}

// Anchored with `position: fixed` against the badge's own bounding rect rather than
// nested inside the badge -- `.badge` sets `overflow: hidden` (chrome.css) to ellipsize
// long single-line text, which would silently clip a popover appended as its child.
function positionWeatherPopover() {
  if (!weatherPopoverEl) return;
  const rect = el("weather-badge").getBoundingClientRect();
  weatherPopoverEl.style.top = `${rect.bottom + 6}px`;
  weatherPopoverEl.style.left = `${rect.left}px`;
}

function setWeatherPopoverOpen(open) {
  const badge = el("weather-badge");
  if (open) {
    positionWeatherPopover();
    weatherPopoverEl.hidden = false;
    badge.setAttribute("aria-expanded", "true");
    weatherPopoverResizeHandler = positionWeatherPopover;
    window.addEventListener("resize", weatherPopoverResizeHandler);
  } else {
    weatherPopoverEl.hidden = true;
    badge.setAttribute("aria-expanded", "false");
    if (weatherPopoverResizeHandler) {
      window.removeEventListener("resize", weatherPopoverResizeHandler);
      weatherPopoverResizeHandler = null;
    }
  }
}

// Built once, on the first forecast this session actually has data to show -- there's
// nothing to make clickable, and nothing to close a listener over, until then.
function ensureWeatherPopover() {
  if (weatherPopoverEl) return;
  const badge = el("weather-badge");
  const popover = document.createElement("div");
  popover.id = "weather-forecast-popover";
  popover.className = "weather-forecast-popover";
  popover.hidden = true;
  popover.setAttribute("role", "dialog");
  popover.setAttribute("aria-label", "Upcoming forecast");
  document.body.appendChild(popover);
  weatherPopoverEl = popover;
  badge.setAttribute("role", "button");
  badge.setAttribute("tabindex", "0");
  badge.setAttribute("aria-haspopup", "dialog");
  badge.setAttribute("aria-expanded", "false");
  const toggle = (ev) => {
    ev.preventDefault();
    if (!weatherPopoverHasContent()) return;
    setWeatherPopoverOpen(popover.hidden);
  };
  badge.addEventListener("click", toggle);
  badge.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") toggle(ev);
  });
  document.addEventListener("click", (ev) => {
    if (popover.hidden || ev.target === badge || popover.contains(ev.target)) return;
    setWeatherPopoverOpen(false);
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !popover.hidden) setWeatherPopoverOpen(false);
  });
}

// -- Weather alerts (severity badge + popover section) ----------------------
//
// `WeatherReport.alerts` (contracts.py) is real NWS alerts.weather.gov data -- unlike
// the forecast, an empty list is the normal case, so nothing renders when it's empty
// (no "no active alerts" placeholder). When it's non-empty this is safety information,
// not convenience like the forecast, so it gets its own visual channel on the badge
// itself (severity color + pulse for the worst active alert, see chrome.css) rather
// than being something the user only discovers by clicking through.
//
// Severity order matches WeatherAlertSeverity (contracts.py) worst-first, so the badge
// and the popover's ordering always agree on what to lead with.
const ALERT_SEVERITY_RANK = { Extreme: 4, Severe: 3, Moderate: 2, Minor: 1, Unknown: 0 };

// Aggregated across every record in the envelope, not just the one driving the badge's
// temperature (`envelope.records[0]`). This app tracks multiple stations (Central Park,
// La Guardia, JFK); a coastal station can have an active rip-current statement while the
// primary station shows clear skies, and a user must not be left thinking "no alerts"
// just because the station picked for the temperature reading happens to have none.
// Each entry keeps its station_name so the popover can attribute an alert to the right
// location once more than one report is in play.
function collectWeatherAlerts(records) {
  const entries = [];
  for (const report of records || []) {
    for (const alert of report.alerts || []) {
      entries.push({ stationName: report.station_name, alert });
    }
  }
  entries.sort(
    (a, b) => ALERT_SEVERITY_RANK[b.alert.severity] - ALERT_SEVERITY_RANK[a.alert.severity]
  );
  return entries;
}

// Extreme/Severe read as urgently as this app's existing `--error` status color (plus a
// pulse -- see .badge[data-alert-severity] in chrome.css); Moderate/Minor read as
// `--stale`, present but not alarming. Reuses the same two tokens the status pills
// already use rather than inventing a third color for "alert".
function applyWeatherAlertSeverity(records) {
  const badge = el("weather-badge");
  const entries = collectWeatherAlerts(records);
  if (entries.length) {
    badge.dataset.alertSeverity = entries[0].alert.severity.toLowerCase();
  } else {
    delete badge.dataset.alertSeverity;
  }
}

// A short native-tooltip summary of the worst active alert, appended to the badge's
// existing `title` -- a quick hover confirms *why* the badge looks urgent without
// needing to click into the popover for the full list.
function alertTitleSuffix(records) {
  const entries = collectWeatherAlerts(records);
  if (!entries.length) return "";
  const top = entries[0];
  const more = entries.length > 1 ? ` (+${entries.length - 1} more)` : "";
  return `\n⚠ ${top.alert.event} — ${top.alert.severity} (${top.stationName})${more}`;
}

function alertAreaHtml(alert) {
  // area_desc ("Kings (Brooklyn); Southwest Suffolk; ...") tells the user whether the
  // alert actually covers where they are, which the station name alone doesn't -- shown
  // whenever NWS provided one rather than trying to fuzzy-compare it against the
  // station's own location.
  return alert.area_desc
    ? `<p class="weather-alert-area">${escapeHtml(alert.area_desc)}</p>`
    : "";
}

function weatherAlertHtml(entry) {
  const { stationName, alert } = entry;
  const headline = alert.headline
    ? `<p class="weather-alert-headline">${escapeHtml(alert.headline)}</p>`
    : "";
  return `<li class="weather-alert" data-severity="${escapeHtml(alert.severity.toLowerCase())}">
    <div class="weather-alert-head">
      <span class="weather-alert-severity">${escapeHtml(alert.severity)}</span>
      <span class="weather-alert-event">${escapeHtml(alert.event)}</span>
    </div>
    ${headline}
    ${alertAreaHtml(alert)}
    <p class="weather-alert-station">${escapeHtml(stationName)}</p>
  </li>`;
}

// Real forecast periods only -- an empty `forecast` (legitimate: `_report_for` can
// resolve a station with a good observation, see weather.py) removes the popover
// affordance entirely rather than opening onto an empty or fabricated placeholder.
// This now also depends on weatherAlertEntries: the popover (and its click affordance)
// must stay open to a station with alerts but no forecast, and vice versa.
function weatherPopoverHasContent() {
  return weatherForecastPeriods.length > 0 || weatherAlertEntries.length > 0;
}

// Alerts render as their own visually distinct section (see .weather-alert-list in
// chrome.css: left accent border, tinted background, severity-cased text) above the
// forecast periods -- urgent, station-specific information first, routine upcoming
// conditions after, rather than one flat list where an alert would just look like
// another forecast entry.
function renderWeatherPopoverBody() {
  if (!weatherPopoverEl) return;
  const alertsHtml = weatherAlertEntries.length
    ? `<ul class="weather-alert-list">${weatherAlertEntries.map(weatherAlertHtml).join("")}</ul>`
    : "";
  const forecastHtml = weatherForecastPeriods.length
    ? `<ul class="weather-period-list">${weatherForecastPeriods
        .slice(0, FORECAST_PERIODS_SHOWN)
        .map(weatherPeriodHtml)
        .join("")}</ul>`
    : "";
  weatherPopoverEl.innerHTML = alertsHtml + forecastHtml;
  if (!weatherPopoverEl.hidden) positionWeatherPopover();
}

// Shared tail of renderWeatherForecast/renderWeatherAlerts: both feed the same popover
// and must agree on whether the badge gets a click affordance at all.
function updateWeatherPopoverAffordance() {
  const badge = el("weather-badge");
  const hasContent = weatherPopoverHasContent();
  badge.classList.toggle("has-forecast", hasContent);
  if (!hasContent) {
    badge.removeAttribute("role");
    badge.removeAttribute("tabindex");
    badge.removeAttribute("aria-haspopup");
    badge.removeAttribute("aria-expanded");
    if (weatherPopoverEl) setWeatherPopoverOpen(false);
    return;
  }
  ensureWeatherPopover();
  renderWeatherPopoverBody();
}

function renderWeatherForecast(periods) {
  weatherForecastPeriods = periods || [];
  updateWeatherPopoverAffordance();
}

function renderWeatherAlerts(records) {
  weatherAlertEntries = collectWeatherAlerts(records);
  updateWeatherPopoverAffordance();
}
