/*
 * The sidebar: the per-feed layer list (checkbox + status pill + detail line) and the
 * header's health/weather badges. Everything here reads envelope.status and nothing
 * else -- see the degradation rules in app.js.
 *
 * Depends on: utils.js (el, hhmmss), state.js (state), map-layers.js (FEEDS, renderLayers).
 */

function buildPanel() {
  const list = el("layers");
  for (const feed of FEEDS) {
    const li = document.createElement("li");
    li.id = `layer-${feed.key}`;
    li.innerHTML = `
      <div class="layer-head">
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
  }
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
  pill.dataset.status = envelope.status;
  detail.dataset.status = envelope.status;
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
