# nyc-dash: the dashboard

`nyc-dash` (`packages/nyc-dash/`) is a FastAPI app plus a single static page that draws the
live feeds on a MapLibre GL map with deck.gl overlays. It reads `nyc_live.services` only: it
never fetches an upstream itself, never imports `nyc_live.feeds`, and never fabricates a
record. `packages/nyc-dash/README.md` is the package's own reference and this page does not
contradict it.

## Running it

```bash
just dash                       # or: uv run nyc-dash
uv run nyc-dash --host 0.0.0.0 --port 9000 --reload --log-level debug
```

The default bind is `127.0.0.1:8080` (`packages/nyc-dash/src/nyc_dash/__main__.py`). Open
<http://127.0.0.1:8080> for the page and `/docs` for the generated OpenAPI reference.
`--reload` watches the package source and the static assets. For a process manager, the
import string is `nyc_dash.app:app`; `create_app()` builds the `Services` (one httpx client,
the feed registry, the frame source, the DuckDB store) in the lifespan and closes them on
shutdown.

The DuckDB store follows the same reader policy as the MCP server
(`services/registry.py::open_store_for_reader`, described in `docs/mcp.md`): read-only if the
file exists, created writable once if it does not, and `None` if it cannot be opened at all.
With no store the `density` endpoint answers `status="error"`, `error.kind="internal"`.

## Endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /api` | the route table: key, label, feed, path, geo support, aliases, default limit, description, plus `health`, `stream` and `stream_default_feeds` |
| `GET /api/<feed>` | that feed's `contracts.Envelope`, serialised unchanged |
| `GET /api/health` | `FeedRegistry.health()` plus the DuckDB store state |
| `GET /api/stream` | Server-Sent Events, one envelope per feed per cycle |
| `GET /` | the single page (`static/index.html` plus the `static/js/` and `static/css/` assets) |
| `GET /docs` | FastAPI's OpenAPI page |

Feed keys, with aliases in brackets (`api.py::ROUTES`):

| Key (aliases) | Feed | Geo | Default limit | Default radius |
|---|---|---|---|---|
| `dot_cameras` (`cameras`) | `dot_cameras` | yes | none | 1000 m |
| `density` | `density` | yes | 500 | 1000 m |
| `camera_density_history` | `density` | no | 500 | n/a |
| `subway_arrivals` (`subway`) | `mta_subway` | yes | 1000 | 500 m |
| `mta_subway` (`subway_trips`) | `mta_subway` | no | 500 | n/a |
| `mta_subway_shapes` (`subway_shapes`) | `mta_subway_shapes` | no | none | n/a |
| `mta_subway_stops` (`subway_stops`, `stops`) | `mta_subway_stops` | yes | none | 1000 m |
| `mta_subway_alerts` (`subway_alerts`, `alerts`) | `mta_subway_alerts` | yes | 200 | 800 m |
| `citibike` (`bikes`) | `citibike` | yes | 2500 | 1000 m |
| `nyc_311` (`311`) | `nyc_311` | yes | 1000 | 1000 m |
| `dohmh_inspections` (`inspections`) | `dohmh_inspections` | yes | 500 | 1000 m |
| `weather` | `weather` | yes | 10 | 50 000 m |
| `mta_bus` (`bus`) | `mta_bus` | yes | 1000 | 1000 m |
| `ny511_cameras` (`ny511`) | `ny511_cameras` | yes | 1000 | 1000 m |

Query arguments, where the service layer supports them: `lat`, `lon`, `radius_m`, `limit`,
plus `stop_id` and `horizon_s` for `subway_arrivals`, `complaint_type` for `nyc_311`, and
`camera_id` and `window_s` for `density`. A filtered response sets `query`,
`total_before_filter`, `truncated`, and `distance_m` on each record, exactly as the service
layer produced them.

`camera_density_history` requires `camera_id` and has no geo support; it reuses the same
`window_s` argument as `density`, but here it means how far back to look (default 300 s at
this HTTP layer, matching `density`'s own default -- pass `window_s=3600` for the trailing
hour, which is what the map's camera-click trend chart does). `mta_subway_shapes` has no
lat/lon of its own (each record is a polyline, not a point), so it is not geo-filterable
either; the frontend fetches it once at boot rather than filtering it.

`GET /api/health` returns the same shape as the MCP `feed_health` tool: `checked_at`, a
`store` object (`path`, `open`, `read_only`) and a `feeds` list of `FeedHealth` entries
(`feed`, `configured`, `status`, `ttl_s`, `last_ok_at`, `last_error`,
`consecutive_failures`, `record_count`). It fetches nothing. A feed no tool has read yet in
this process reports `status: "never_fetched"`, and a key-gated feed with no key reports
`configured: false`. The page uses it for the "feeds: N fresh, N stale, N error" badge.

### Status codes

A down feed is **HTTP 200 with `status="error"`**, because the browser needs the envelope to
render that layer's pill. HTTP 4xx is reserved for a bad request:

* 404: unknown feed key (the message lists the available keys).
* 400: `lat` without `lon`, or a geo filter on a feed whose records have no coordinates
  (`mta_subway`).
* 422: an argument outside its range (`limit` 1 to 50 000, `radius_m` above 0 and up to
  50 000, `window_s` 1 to 86 400, `horizon_s` 0 to 86 400, `interval_s` 0.5 to 300).

An unexpected exception inside a handler is caught, logged, and returned as an
`error.kind="internal"` envelope (`api.crashed`), so one broken handler never takes down the
API or blanks the rest of the page.

## The Envelope the frontend consumes

Every `/api/<feed>` response and every SSE data frame is a `contracts.Envelope` dumped with
`model_dump(mode="json")`. The field-by-field reference, including how a client should read
`status`, `stale_after`, `error.kind` and `query`, is in `docs/mcp.md` under "The Envelope
every tool returns"; it is the same object and is not repeated here. The short form:

* `fresh`: the snapshot is younger than the feed's TTL.
* `stale`: the TTL expired and the refresh failed. `records` is the last good snapshot taken
  at `fetched_at` and `error` says why it could not be refreshed.
* `error`: no usable data. `records` is `[]` and `error` says why.

## Live updates

`GET /api/stream` (`text/event-stream`, `nyc_dash/stream.py`) pushes one event per feed per
cycle, then a `health` event, then waits `interval_s`:

```
event: ready
data: {"feeds": ["subway_arrivals", ...], "interval_s": 15.0, "cycles": null, "server_time": "..."}

event: citibike
data: {"feed": "citibike", "status": "fresh", ...}

event: health
data: {"checked_at": "...", "store": {...}, "feeds": [...]}

: keepalive
```

The event name is the feed key; the data is the same envelope `GET /api/<feed>` returns.
Arguments: `feeds` (comma-separated keys, defaulting to the map layers
`subway_arrivals`, `density`, `nyc_311`, `citibike`, `weather`, `dot_cameras`), `interval_s`
(0.5 to 300, default 15), `cycles` (stop after N cycles; 0 streams until the client
disconnects), and the same `lat`, `lon`, `radius_m` and `limit` as the feed endpoints. Each
route streams with its own `Params` because the per-feed radius and limit defaults differ.

The wait is sliced at 0.25 s so a disconnect is noticed quickly, and a `: keepalive` comment
goes out about every 15 s of waiting. Responses carry `cache-control: no-store` and
`x-accel-buffering: no` so nginx does not buffer the stream. A feed that fails is pushed as
its own `status="error"` envelope, so one dead feed never stops the others from updating. The
stream does not bypass the cache: `CachedFeed` still enforces each feed's TTL, and polling
faster than a TTL just re-serves the cached snapshot.

### Polling fallback

The stream is an optimisation, not a requirement. Every event name is also a
`GET /api/<feed>` endpoint returning the identical envelope, so a client that cannot hold an
`EventSource` open (no SSE support, a buffering proxy, a stream error) polls those endpoints
on the same cadence instead. `static/js/data-sync.js` does exactly that: it loads with one GET
per feed for fast first data, then opens the stream; on the first `EventSource` error it closes
the source and switches to `setInterval` polling every 15 s. The connection pill in the header
says which mode is active: "connecting", "live (SSE)" or "polling every 15s", with the reason
for the fallback in its tooltip.

## Per-layer degradation

Degradation is per layer and is driven only by `envelope.status` (`status-panel.js::setPill`
and `map-layers.js::renderLayers`):

* **fresh**: the pill says `fresh` and the detail line shows the record count and the fetch
  time.
* **stale**: the pill says `stale`, the last good records stay on the map, and the detail line
  shows `last good HH:MM:SS`, the count, and `envelope.error.message` (why the refresh
  failed).
* **error**: the pill says `error`, the layer is not drawn at all, and the detail line is
  `envelope.error.message`.

A layer with no data is absent, never a placeholder shape or a filled-in value, and one feed
failing changes nothing about any other layer. If the dashboard API itself is unreachable,
`fetchFeed` synthesises an `error` envelope saying "dashboard API unreachable" for that layer
rather than inventing records. Layers are toggleable; `subway_arrivals`, `density`, `nyc_311`
and `citibike` are on by default, and `dot_cameras`, `dohmh_inspections` and `mta_bus` are off
(opt-in: they are either dense data or, for buses, off unless the user searches for a route --
see "Search" below). The static subway route-shape backdrop (`mta_subway_shapes`) is not one
of these toggleable layers at all; it always draws once fetched.

The weather badge follows the same rule: `error` shows "weather unavailable" with the message
in the tooltip, and `stale` appends "last good HH:MM:SS". `density` is `error` with
`kind="not_configured"` until nyc-vision has written `density_samples` rows, so the heatmap is
simply hidden and the panel says why. `tests/dash/test_degradation.py` covers the per-feed
behaviour server-side and `tests/dash/test_frontend.py` asserts the strings in `app.js`.

## Search

The sidebar search box (`static/js/search.js`) finds a subway station (by stop name), a Citi
Bike dock (by name), a DOT camera (by name), or a bus route (by number) among data the page
has already polled; there is no dedicated search endpoint. Matches are grouped by kind and,
within a kind, sorted nearest-first from the map's current center (`currentSearchOrigin`,
Haversine distance); bus routes have no single point to measure from and keep an alphabetical
order instead. Selecting a subway, Citi Bike, or camera result flies the map to it (zoom 16)
and opens that record's real detail panel through the same lookup table (`DETAIL_BUILDERS`) a
map click uses. Selecting a bus route does not fly anywhere -- a route is many live vehicles,
not one point -- instead it sets the shared `highlightedBusRoute` value so `busLayer`
(`map-layers.js`) brings that route's vehicles to full opacity and a larger radius while
dimming every other bus, and switches the `mta_bus` layer on if it was off.

## Borough filter

The sidebar's borough chips (`#borough-filter` in `index.html`, wired by `initBoroughFilter`
in `map-layers.js`) narrow the three layers whose records carry a clean borough field: DOT
cameras (`Camera.area`), 311 requests (`ServiceRequest.borough`), and restaurant inspections
(`RestaurantInspection.boro`). The comparison is case-insensitive because those three fields
do not agree on case. Subway, camera density, Citi Bike, and buses have no comparable field
and are unaffected. Selecting a borough also updates the three affected layers' sidebar counts
to `"<drawn> of <total> <noun> · <borough>"` so the count next to each checkbox never
disagrees with what is actually drawn on the map.

## Service alerts and route highlighting

A collapsible sidebar section (`static/js/alerts-banner.js`) lists active MTA subway service
alerts, fetched from `/api/mta_subway_alerts?limit=200` and polled independently every 60 s
(alerts change far less often than train positions, so this does not share the map layers'
15 s cadence). Each alert shows route chips colored to match the same palette `subwayLayer`
and the route-shapes backdrop use, its header text, and when it started; the count badge uses
the envelope's `total_before_filter`, not the page size, so a truncated response is never
presented as the full total. Clicking a route chip sets the shared `highlightedRoute` value,
which brings that route's static GTFS shapes (see below) to full opacity and a thicker stroke
on the map while dimming every other route's shapes; clicking the same chip again clears it.

## Static subway route shapes

`mta_subway_shapes` (24 hour TTL) is fetched once at boot rather than on the map layers'
poll/SSE cycle, since a day-old static bundle has no reason to be re-polled every 15 s. It
draws as a dim PathLayer backdrop under every marker layer and is not one of the toggleable
sidebar layers; the only way to change how it looks is the alert-chip highlight above.

## Weather alerts

`weather_now`'s `alerts` field surfaces on the header's weather badge (`status-panel.js`):
when any configured station (Central Park, LaGuardia, JFK) has an active NWS alert, the badge
takes a severity-based color -- Extreme/Severe read as the same red as a failed feed, plus a
pulse; Moderate/Minor read as the same amber as a stale one -- and its tooltip names the worst
active alert. Alerts are aggregated across every station in the envelope, not just the one
driving the badge's temperature text, since a coastal station can have an active alert (a rip
current statement, say) while the station chosen for the headline temperature shows clear
skies. Clicking the badge opens a popover listing every active alert (event, severity,
headline, area, station) above the existing short forecast list; an empty `alerts` list is the
normal case and adds nothing to the badge or the popover.

## Shareable map URLs

The map's center and zoom are kept in the URL hash as `#zoom/lat/lon` (`static/js/app.js`,
e.g. `#12.40/40.73570/-73.99110`), written with `history.replaceState` (never `pushState`, so
panning does not spam browser back-button history) about 200 ms after each `moveend`. Loading
a URL with a hash restores that view instead of the default city-wide one; a malformed or
absent hash falls back to the default view rather than erroring. A copy-link map control
(top-left, below the zoom and recenter controls) copies `location.href` to the clipboard and
shows a checkmark or a warning glyph depending on whether the clipboard write succeeded.

## Detail panel

One persistent panel (`static/js/detail-panel.js`) is reused by every clickable layer
(subway, 311, Citi Bike, DOT cameras, restaurant inspections, buses) rather than a bespoke UI
per layer. It carries `role="dialog"` and `aria-modal="true"`, traps Tab/Shift+Tab focus to
its own focusable elements while open, and restores focus to whatever triggered it (a clicked
marker, a search result, or a documented fallback) when it closes. An open panel stays live:
it is re-rendered from every subsequent feed refresh (poll or SSE) so a train's ETA or a bus's
next stop keeps counting down instead of freezing at the moment it was clicked; if the tracked
record drops out of the feed entirely, the panel shows an explicit "no longer tracked" message
instead of leaving stale data on screen.

## Mobile layout

Below 900px width, `chrome.css` stacks the map above the sidebar panel instead of placing them
side by side. Below 480px the panel becomes a docked bottom sheet: the map fills the full
viewport underneath and the panel overlays its bottom edge with its own independent scroll and
a drag-handle affordance, so the map never moves while the sheet's own contents (search,
borough filter, alerts, layers) scroll independently of it. Map controls grow to a 44px touch
target at this width.

## Color palette

Marker colors (`static/js/map-layers.js`) follow a documented categorical/sequential/status
palette rather than being picked ad hoc: subway and bus route colors reuse the real MTA line
colors (kept recognizable to riders who already know them), restaurant grades reuse the same
colors as the fresh/stale/error status pills, and the Citi Bike fill ramp is a sequential blue
scale. Comments in `map-layers.js` record which color pairs were checked against
colorblind-simulated contrast floors, and which known limits (for example, that ten subway
line colors cannot all be pairwise distinct under every color-vision deficiency simultaneously)
were accepted rather than solved by drifting away from the real MTA colors.

## Frontend assets

One `index.html`, no build step, and a set of plain `<script defer>` files loaded in
dependency order (`static/js/`): `utils.js`, `icons.js`, `state.js` (shared mutable state:
the map instance, the per-feed `state` map, and cross-file globals like `highlightedRoute`
and `selectedBorough`), `map-layers.js` (marker colors, the deck.gl layer builders, the `FEEDS`
table), `alerts-banner.js`, `status-panel.js` (the sidebar layer list and header badges),
`detail-panel.js`, `search.js`, `data-sync.js` (fetch/poll/SSE wiring), and `app.js` (boot,
loaded last). Stylesheets (`static/css/`): `tokens.css`, `chrome.css`, `layers-panel.css`,
`search.css`, `detail-panel.css`, `alerts-banner.css`. The map libraries are loaded from
pinned CDN URLs:

* `https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css` and `.../maplibre-gl.js`
* `https://unpkg.com/deck.gl@9.0.0/dist.min.js`

The basemap style is OpenFreeMap's keyless `liberty` style at
`https://tiles.openfreemap.org/styles/liberty`. No API key is used anywhere in the page or
the server. If the CDN scripts fail to load, `initMap` shows a banner saying so and the status
pills keep working without a map; if the style or tiles fail, a banner says the basemap is
unavailable and the data layers keep updating.

Toggleable layers (`FEEDS` in `map-layers.js`): subway trains drawn at their next stop's
coordinates (a ScatterplotLayer coloured by route; the coordinates come from the static stops
feed, nothing is interpolated between stations), camera density (a HeatmapLayer weighted by
person mean plus vehicle mean), 311 requests, Citi Bike stations coloured by how full they are
(zoomed-out stations decluttered by shrinking and fading near-empty ones), DOT camera
locations, restaurant inspections coloured by grade, and MTA buses coloured by the subway line
palette their route falls back to. Underneath all of these, static GTFS subway route shapes
(`mta_subway_shapes`) draw as a dim, always-on PathLayer backdrop; they are not one of the
toggleable `FEEDS` entries. The weather badge lives in the header, not on the map.

## Measured numbers

Server-side timings measured in the build sandbox during the Phase 4 gate run. They are
response times from the app only: no upstream fetch and no browser rendering is included.

| Path | Measured |
|------|----------|
| the static documents (`index.html` plus `js`/`css` assets, measured pre-refactor as three files) | 7.5 ms total |
| per-feed `/api/<feed>` endpoints | 1.3 to 1.5 ms median |
| `/api/density` (through DuckDB) | 4.6 ms |
| `/api/health` | 1.3 ms |
| `/api/stream` to the `ready` event | 6.8 ms |
| `/api/stream` full cycle (every feed plus `health`) | 17.2 ms |

**First paint under 2 s has not been measured.** The gate needs a real browser loading the
page, and `unpkg.com` and `tiles.openfreemap.org` are both answered with HTTP 403 by the build
sandbox egress proxy, so the map cannot paint there and the number would be meaningless. It is
not guessed anywhere.

### Running the first-paint gate

`tests/dash/test_first_paint.py` is the browser half of the Phase 4 gate. It needs two things
the build sandbox did not have:

1. The `playwright` Python package. It is not in `pyproject.toml` yet; add `playwright>=1.56`
   to the dev group. Pin below 1.62: the Chromium already installed at `/opt/pw-browsers` is
   revision 1194, which the 1.56.x line expects and 1.62 does not, so a newer playwright would
   try to download a browser.
2. Reachable `unpkg.com` and `tiles.openfreemap.org`.

Do not run `playwright install`; point the existing browser at it instead:

```bash
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers uv run pytest tests/dash/test_first_paint.py -s
```

The measured number is printed as `first-contentful-paint (warm cache): NNN ms` and the test
fails above 2000 ms. Both conditions are checked at runtime and the tests skip with a reason
naming whatever is missing, so they never pass silently. The file holds four tests: first
paint on a warm cache, updates arriving over SSE without a page refresh, honest degradation
when the map libraries are blocked, and the polling fallback when `/api/stream` is blocked.
The last two need only playwright, since they block the CDN on purpose.

## Tests

`tests/dash/` drives the app through `TestClient` and `httpx.ASGITransport` with fake adapters
behind the real `CachedFeed`, so no network and no on-disk DuckDB is touched: `test_api.py`
(routing, geo filtering, argument validation), `test_stream.py` (SSE framing and arguments),
`test_degradation.py` (per-feed fresh, stale and error behaviour), `test_frontend.py` (reads
every file under `static/js/` and `static/css/` and asserts on the degradation strings, the
script load order, and the search/borough/alerts/detail-panel behaviour described above),
`test_cli.py`, and `test_first_paint.py` (the browser gate above, marked `slow`).
