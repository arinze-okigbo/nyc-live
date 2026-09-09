/*
 * Data flow: one GET per feed on load (fast first data), then Server-Sent Events
 * on /api/stream for updates without a page refresh. If EventSource is missing or
 * the stream errors, we close it and fall back to polling the same /api/<feed>
 * endpoints on the same cadence; the connection pill says which mode is active.
 *
 * Degradation rules (the whole point of this page):
 *   - every layer's pill is driven by envelope.status and nothing else;
 *   - "stale" keeps the last good records on the map and shows when they were good;
 *   - "error" hides that layer and shows envelope.error.message;
 *   - a layer with no data is absent, never a placeholder shape or a fake value;
 *   - one feed failing changes nothing about any other layer.
 *
 * Depends on: utils.js (el, setConnection), state.js (state), map-layers.js
 * (FEEDS, STREAM_KEYS, WEATHER_KEY, renderLayers), status-panel.js (setPill, applyHealth,
 * applyWeather).
 */

function applyEnvelope(key, envelope) {
  if (key === WEATHER_KEY) {
    applyWeather(envelope);
    el("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
    return;
  }
  const entry = state.get(key);
  if (!entry) return;
  entry.envelope = envelope;
  setPill(key, envelope);
  renderLayers();
  el("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
}

function feedUrl(key) {
  const feed = FEEDS.find((f) => f.key === key);
  return feed && feed.query ? `/api/${key}?${feed.query}` : `/api/${key}?limit=10`;
}

async function fetchFeed(key) {
  try {
    const response = await fetch(feedUrl(key), { headers: { accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    applyEnvelope(key, payload);
  } catch (err) {
    // The API itself is unreachable. Say so on that layer; do not invent records.
    applyEnvelope(key, {
      feed: key,
      status: "error",
      fetched_at: null,
      stale_after: null,
      records: [],
      error: { kind: "internal", message: `dashboard API unreachable: ${err.message}` },
    });
  }
}

async function refreshAll() {
  await Promise.all(STREAM_KEYS.map(fetchFeed));
  fetchHealth();
}

async function fetchHealth() {
  try {
    const response = await fetch("/api/health");
    applyHealth(await response.json());
  } catch (err) {
    const badge = el("health-badge");
    badge.dataset.status = "error";
    badge.textContent = "feeds: health unavailable";
    badge.title = err.message;
  }
}

function startPolling(reason) {
  if (pollTimer) return;
  setConnection("polling", `polling every ${REFRESH_S}s`);
  el("connection-badge").title = reason;
  pollTimer = setInterval(refreshAll, REFRESH_S * 1000);
}

function connectStream() {
  if (!("EventSource" in window)) {
    startPolling("EventSource is not supported by this browser");
    return;
  }
  const url = `/api/stream?feeds=${STREAM_KEYS.join(",")}&interval_s=${REFRESH_S}`;
  source = new EventSource(url);
  source.addEventListener("ready", () => setConnection("live", "live (SSE)"));
  source.addEventListener("health", (ev) => applyHealth(JSON.parse(ev.data)));
  for (const key of STREAM_KEYS) {
    source.addEventListener(key, (ev) => applyEnvelope(key, JSON.parse(ev.data)));
  }
  source.onerror = () => {
    source.close();
    source = null;
    startPolling("the SSE stream closed; falling back to polling");
  };
}
