# nyc-dash

FastAPI + MapLibre GL / deck.gl dashboard over the `nyc-live` service layer.

```bash
just dash                       # or: uv run nyc-dash
uv run nyc-dash --host 0.0.0.0 --port 9000 --reload --log-level debug
```

Then open <http://127.0.0.1:8080>. API docs are at `/docs`.

This process reads `nyc_live.services` only. It never fetches an upstream itself, never
imports `nyc_live.feeds`, and never fabricates a record: a feed that is down comes back
empty with the error attached, and the corresponding map layer is hidden.

## API

| Endpoint | Returns |
|---|---|
| `GET /api` | the route table (key, label, feed, geo support, description) |
| `GET /api/<feed>` | that feed's `contracts.Envelope`, serialised unchanged |
| `GET /api/health` | `FeedRegistry.health()` plus the DuckDB store state |
| `GET /api/stream` | Server-Sent Events, one envelope per feed per cycle |
| `GET /` | the single-page dashboard (`index.html`, `app.js`, `style.css`) |

Feed keys (aliases in brackets): `dot_cameras` (`cameras`), `density`, `subway_arrivals`
(`subway`), `mta_subway` (`subway_trips`), `mta_subway_stops` (`subway_stops`, `stops`),
`mta_subway_alerts` (`subway_alerts`, `alerts`), `citibike` (`bikes`), `nyc_311` (`311`),
`dohmh_inspections` (`inspections`), `weather`, `mta_bus` (`bus`), `ny511_cameras`
(`ny511`).

Every feed response is the whole envelope:

```json
{"feed": "citibike", "status": "fresh", "fetched_at": "...", "stale_after": "...",
 "records": [...], "error": null, "query": null, "total_before_filter": 2189,
 "truncated": false}
```

* `fresh` — younger than the feed's TTL.
* `stale` — the TTL expired and the refresh failed; `records` is the last good snapshot
  taken at `fetched_at` and `error` says why it could not be refreshed.
* `error` — no usable data; `records` is `[]` and `error` says why.

A down feed is **HTTP 200 with `status="error"`**, because the browser needs the envelope
to render that layer's status pill. HTTP 4xx is reserved for bad requests: an unknown feed
key (404), `lat` without `lon` (400), a geo filter on a feed whose records have no
coordinates such as `mta_subway` (400), out-of-range arguments (422).

Query arguments, where the service layer supports them: `lat`, `lon`, `radius_m` (default
per feed), `limit`; plus `stop_id` and `horizon_s` (`subway_arrivals`), `complaint_type`
(`nyc_311`), `camera_id` and `window_s` (`density`). Filtered responses set `query`,
`total_before_filter`, `truncated`, and `distance_m` on each record.

## Live updates

`GET /api/stream` (`text/event-stream`) pushes one event per feed per cycle, then a
`health` event, then waits `interval_s` (default 15, keepalive comments every ~15 s):

```
event: ready
data: {"feeds": ["subway_arrivals", ...], "interval_s": 15.0, "server_time": "..."}

event: citibike
data: {"feed": "citibike", "status": "fresh", ...}

event: health
data: {"checked_at": "...", "store": {...}, "feeds": [...]}
```

Arguments: `feeds` (comma-separated keys; defaults to the map layers), `interval_s`
(0.5–300), `cycles` (stop after N cycles; 0 = until the client disconnects), and the same
`lat`/`lon`/`radius_m`/`limit` as the feed endpoints. A feed that fails is pushed as its
own `status="error"` envelope, so one dead feed never stops the others from updating. The
stream does not bypass the cache: `CachedFeed` still enforces each feed's TTL.

**Polling fallback.** Every event name is also a `GET /api/<feed>` endpoint returning the
identical envelope, so a client that cannot hold an SSE connection open (no `EventSource`,
a buffering proxy, a stream error) polls those endpoints on the same cadence instead.
`app.js` switches to `setInterval` polling on the first `EventSource` error and shows
"polling" in the connection pill.

## Frontend

One `index.html`, one `app.js`, one `style.css`; no build step. MapLibre GL and deck.gl are
loaded from pinned CDN URLs (`unpkg.com/maplibre-gl@4.7.1`, `unpkg.com/deck.gl@9.0.0`) and
the basemap is OpenFreeMap's keyless `liberty` style — no API key anywhere.

Layers: subway trains at their next stop's coordinates (ScatterplotLayer, stop coordinates
come from the static stops feed — nothing is interpolated between stations), camera density
(HeatmapLayer over nyc-vision aggregates), 311 requests (ScatterplotLayer), Citi Bike
stations coloured by how full they are, DOT camera locations, and a weather badge.

Degradation is per layer, driven only by `envelope.status`: `fresh` shows the record count
and fetch time, `stale` keeps the last good records on the map and shows "last good
HH:MM:SS" plus the refresh error, `error` hides that layer and shows
`envelope.error.message`. Nothing is ever drawn from placeholder data, and one dead feed
changes nothing about the others. If the CDN itself is unreachable, a banner says so and
the status pills keep working without a map.

## Tests

`tests/dash/` drives the app through `TestClient` / `httpx.ASGITransport` with fake
adapters behind the real `CachedFeed`, so no network and no on-disk DuckDB is touched.
`tests/dash/test_first_paint.py` is the browser half of the Phase 4 gate (first paint under
2 s on a warm cache) and needs the `playwright` package plus reachable `unpkg.com` /
`tiles.openfreemap.org`; it skips with a reason naming whichever is missing.
