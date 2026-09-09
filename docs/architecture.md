# Architecture

```
                 +-----------------------------------------------------------+
  upstreams      |  src/nyc_live (core library)                              |
                 |                                                           |
  DOT cameras -->|  feeds/cameras.py      \                                  |
  511NY       -->|  feeds/ny511.py         |                                 |
  MTA GTFS-RT -->|  feeds/transit.py       |  FeedAdapter.fetch()            |
  MTA static  -->|  feeds/transit.py       |  -> Snapshot[T] | FeedUnavailable
  Bus Time    -->|  feeds/bus.py           |                                 |
  Citi Bike   -->|  feeds/micromobility.py |                                 |
  Socrata     -->|  feeds/socrata.py       |  (registered via feeds/civic.py)|
  weather.gov -->|  feeds/weather.py      /                                  |
                 |                                                           |
                 |  feeds/__init__.py  ADAPTER_SPECS registry, load_adapters |
                 |  http.py    make_client, RateLimiter, get_with_retry      |
                 |  cache.py   CachedFeed / FeedRegistry: TTL, single-flight,|
                 |             stale-with-error                              |
                 |  store.py   DuckDB (frozen DDL) + Parquet archive         |
                 |  services/  registry, nearby, subway, cameras, density,   |
                 |             warehouse                                     |
                 +-----+---------------------------+-----------------+-------+
                       |                           |                 |
             packages/nyc-mcp            packages/nyc-vision   packages/nyc-dash
             server.py: FastMCP tools    detector.py YOLO      app.py FastAPI + api.py
             __main__.py stdio/--http    sampling.py           routes + stream.py SSE
             Envelope[T] out             pipeline.py tick      static/ MapLibre + deck.gl
                                         report.py, chart.py   Envelope[T] per layer
                                         writes density_samples,
                                         camera_frame_fetches, cameras
                                                 |                     ^
                                                 +---------------------+
                                                  density_samples read back
                                                  through services/density.py
```

All three packages are built. What has and has not been verified against real upstreams is in
`README.md` under Status; the two long-running gates (Phase 3's 24 h run, Phase 4's first
paint) are documented in `docs/vision.md` and `docs/dashboard.md` and have not been run.

## Principles

* **Contracts are frozen.** `contracts.py` is the single source of truth for record shapes, the adapter protocol, and the DuckDB schema. See `CLAUDE.md`.
* **Adapters are parameterless.** Each `fetch()` returns the whole feed. Geo filtering happens in the service layer over cached snapshots, so one upstream call serves every consumer.
* **Every response is an Envelope.** `status` is `fresh`, `stale`, or `error`; `stale_after` and `error` are always present. Consumers render the status, they never assume success.
* **Nothing is fabricated.** A failing upstream produces an error envelope or a stale envelope with the error attached, never synthetic rows.
* **Upstream politeness is layered.** Adapters own per-key cadence (`RateLimiter`), the cache owns TTL, and both are configured from `DEFAULT_TTL`.

## Data flow for a tool call

1. Tool receives optional `lat`, `lon`, `radius_m`.
2. Service asks `FeedRegistry[feed].get()`; the cache returns fresh, stale, or error.
3. Service applies `geo.filter_nearby` and any derived view (e.g. per-stop arrivals).
4. Tool returns `Envelope[T]` with `query` echoed and `total_before_filter` set.

## Storage

DuckDB file at `NYC_LIVE_DUCKDB_PATH`. `nyc-vision` is the only writer of `density_samples` and `camera_frame_fetches`; the cache writes `feed_fetches`. Append-only tables are archived daily to Parquet under `NYC_LIVE_DATA_DIR/archive/<table>/<day>.parquet`. `query_warehouse` is SELECT-only over an allowlist of tables.

The sections above are the Phase 0 baseline.

## Phase 1: adapters

Adapters are discovered by `src/nyc_live/feeds/__init__.py::ADAPTER_SPECS`, a tuple of (module, class, `FeedName`, owner). `load_adapters` imports each module, instantiates `Adapter(client=..., settings=...)`, and checks that `adapter.name` matches the spec. A module that is absent is skipped with a warning (`strict=True` makes it fatal); a module that exists but fails to import is always an error. `FRAME_SOURCE_SPEC` names the one `FrameSource` implementation. Every adapter uses `nyc_live.http.get_with_retry` for GETs and drops out-of-city coordinates with `geo.in_nyc_bbox` (the bbox is padded by 0.05 degrees), logging how many were dropped.

### `feeds/transit.py` (MTA subway)

Three adapters. `SubwayTripsAdapter` (`mta_subway`, 30 s) fetches the eight NYCT GTFS-realtime feeds (`gtfs`, `gtfs-ace`, `gtfs-bdfm`, `gtfs-g`, `gtfs-jz`, `gtfs-nqrw`, `gtfs-l`, `gtfs-si`) concurrently from `settings.mta_gtfs_base/nyct/{slug}` and merges them; a 404 on any slug is `ErrorKind.NOT_FOUND` naming the slug, and eight feeds that decode to zero trips is treated as an upstream fault, never an empty snapshot. `SubwayAlertsAdapter` (`mta_subway_alerts`, 60 s) reads `camsys%2Fsubway-alerts`, sending the `%2F` verbatim. `SubwayStopsAdapter` (`mta_subway_stops`, 24 h) downloads the static GTFS zip from `https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip`, parses `stops.txt` in a worker thread, and derives each stop's `routes` from `trips.txt` joined to `stop_times.txt`, unioned up to the parent station (about 0.5 s on the 2026-08-27 zip). The legacy `web.mta.info` zip returns 403 and is not used; there is no bundled fallback copy. All three share one module-level `RawBytesCache` keyed by URL: within a URL's TTL the cached bytes are served without touching upstream, concurrent callers are single-flighted per URL, and a per-URL `RateLimiter` with interval equal to the TTL backs the cache. Trip direction is derived from the NYCT `trip_id` suffix, falling back to the `N`/`S` suffix of the stop id; NYCT vehicle positions carry no coordinates, so trains are placed by joining `current_stop_id` to the stops feed. `SubwayAlert.updated_at` is always `None` because it lives in a protobuf extension the project does not compile.

### `feeds/bus.py` (MTA Bus Time)

`BusPositionsAdapter` (`mta_bus`, 30 s) is key-gated: `is_configured()` is False without `MTA_BUS_TIME_API_KEY` and `fetch()` raises `FeedNotConfigured`. With a key set it fetches SIRI VehicleMonitoring from `settings.mta_bus_time_base` and maps every active bus (about 1,380 observed live) into `BusVehicle`, including the optional `MonitoredCall`/`Occupancy` fields (next stop, ETA, distance, stops away, occupancy) present on roughly 99.9% of activities. An upstream `ErrorCondition` (e.g. a rejected key, returned as HTTP 200) is checked explicitly and raised as `FeedUnavailable` rather than parsed into zero records. The key is sent as a query parameter and never appears in `source_url`, logs, or error messages.

### `feeds/cameras.py` (NYC DOT)

`CameraListAdapter` (`dot_cameras`, 10 min) reads the JSON list at `settings.dot_cameras_base/` and maps rows into `Camera`, accepting both camelCase and snake_case key variants because the endpoint could not be probed from the build sandbox. A row missing `id`, `name`, `lat`, or `lon` is skipped and counted; if every row is missing them the fetch raises `UPSTREAM_PARSE` naming the keys it looked for, and if no row carries an online flag the cameras are reported offline rather than guessed online. The adapter also honours its own TTL as a minimum upstream interval. `CameraFrameSource` implements `FrameSource`: `get_frame(camera_id)` returns the buffered frame if it is younger than `CAMERA_FRAME_MIN_INTERVAL` (2 s), otherwise waits on a per-camera `RateLimiter` and fetches `{base}/{id}/image`. Frames are validated by JPEG magic bytes (a non-JPEG body is `UPSTREAM_PARSE`); a 404 is `NOT_FOUND` and evicts the buffer entry, since DOT ids rotate. The buffer holds at most `FRAME_BUFFER_MAX_FRAMES_PER_CAMERA` (1) frame per camera in memory and evicts anything older than `FRAME_BUFFER_MAX_AGE` (10 minutes); nothing is written to disk. Every upstream attempt appends a `CameraFrameFetch` row to an in-memory deque (capped at 10,000) that the caller drains with `drain_telemetry()`; the module never opens DuckDB.

### `feeds/ny511.py` (511NY cameras, deferred)

`Ny511CamerasAdapter` (`ny511_cameras`, 10 min) is key-gated on `NY511_API_KEY`. When configured it calls `https://511ny.org/api/getcameras?key=...&format=json`, maps the statewide list into `Camera` records using `cameras.py`'s parsing helpers, and keeps only rows inside the NYC bbox. The field mapping is unverified live. The key is sent as a query parameter and never appears in error messages or logged URLs.

### `feeds/micromobility.py` (Citi Bike GBFS)

`CitiBikeAdapter` (`citibike`, 60 s) fetches the GBFS root at `settings.citibike_gbfs_root`, discovers the `station_information` and `station_status` URLs from it (GBFS 2.x `data.<lang>.feeds` and 3.x `data.feeds` are both handled; child URLs are never hardcoded), fetches both concurrently, and joins them on `station_id`. Station ids present on only one side are dropped and logged; if more than `MAX_UNMATCHED_FRACTION` (5 %) of ids are unmatched the fetch raises `UPSTREAM_PARSE`, on the grounds that the two files were not from the same moment. `ebikes_available` comes from `num_ebikes_available`, else from `vehicle_types_available` classified through the `vehicle_types` feed when the root lists one, else `None`. `stale_after` is `fetched_at` plus the larger of `DEFAULT_TTL[CITIBIKE]` and the published GBFS `ttl`; `upstream_generated_at` is `station_status.last_updated`.

### `feeds/civic.py`, `feeds/socrata.py` (311 and DOHMH inspections)

`civic.py` only re-exports the three civic adapter classes so the registry has one import path. `socrata.py` holds a shared SoDA 2.1 base adapter that pages with `$limit=20000` and `$offset` while a page is exactly full, capped at 10 pages (200k rows, logged when hit). `X-App-Token` is sent only when `SOCRATA_APP_TOKEN` is set. Socrata floating timestamps carry no zone and NYC datasets publish them in America/New_York, so `parse_floating_timestamp` localises there and converts to UTC, and `format_floating_timestamp` renders `$where` literals in NYC local time so the window compares correctly. `Nyc311Adapter` (`nyc_311`, 5 min) selects every request created in the last 24 hours, ordered by `created_date DESC, unique_key DESC` so paging is stable on ties; rows without coordinates are kept (`ServiceRequest` is `MaybeLocated`) and rows outside the bbox are dropped. `InspectionsAdapter` (`dohmh_inspections`, 6 h) selects one row per violation for inspections in the last 90 days with non-null coordinates, which also excludes the dataset's `1900-01-01` never-inspected placeholder; the `0,0` coordinate placeholder is dropped.

### `feeds/weather.py` (weather.gov)

`WeatherAdapter` (`weather`, 5 min) starts from three lookup points (Central Park, LaGuardia, JFK), not station ids. For each, it calls `/points/{lat},{lon}` to obtain the `observationStations` and `forecast` URLs, takes the first feature of the stations list as the nearest station, checks it is inside the NYC bbox, then fetches `/stations/{id}/observations/latest` and the forecast concurrently. Two lookup points resolving to the same station are de-duplicated. A single failing point is skipped and logged; the fetch raises only when no station could be produced, with a per-point reason in the message. Units are converted to the contract's C, km/h, Pa, m, and mm; weather.gov nulls stay `None`. The `User-Agent` set by `http.make_client` is mandatory for this upstream.

## Phase 2: services and MCP

### `services/registry.py`

`build_registry(adapters, store=None, max_stale=DEFAULT_MAX_STALE)` wraps every adapter in a `CachedFeed` and returns a `FeedRegistry`; telemetry goes to the store only when it is writable. `open_store_for_reader(settings)` implements the reader policy described in `docs/mcp.md`: open read-only if the file exists, create it writable once if it does not, and return `None` (with a warning) if it cannot be opened at all. `build_services` combines one `httpx.AsyncClient` (`http.make_client`), the registry, the `CameraFrameSource`, and the store into a `Services` dataclass; `open_services` is the async context-manager form and `Services.aclose()` closes what it owns. Key-gated adapters are registered too, so consumers can show "not configured" rather than "missing".

`CachedFeed` (`src/nyc_live/cache.py`) is where an adapter exception becomes an honest Envelope: `fresh` while the snapshot is younger than the TTL, `stale` when a refresh failed but a snapshot no older than TTL plus `DEFAULT_MAX_STALE` (30 minutes) exists, `error` otherwise. Refreshes are single-flighted per feed, and each attempt is recorded to `feed_fetches` when a writable store is attached.

### Pure service functions

Each takes envelopes or a store and returns an envelope; none fetches an upstream.

* `services/nearby.py`: `nearby(envelope, query, limit)` filters to `radius_m` nearest first via `geo.filter_nearby`, sets `query`, `total_before_filter`, `truncated`, and `distance_m`, drops records without coordinates when a query is applied, and passes error envelopes through untouched. `geo_query(lat, lon, radius_m)` builds a `GeoQuery` and raises `ValueError` when only one of `lat`/`lon` is given.
* `services/subway.py`: `subway_arrivals(trips, stops, query, stop_id, horizon, limit)` joins GTFS-RT `stop_time_updates` to the static stops, keeps platforms distinct (`127N` and `127S`), resolves a parent station id to all its platforms, sorts soonest first, and returns the weaker of the two input statuses. `alerts_near(alerts, stops, query)` keeps alerts naming a route or stop served within the radius and returns the alerts unfiltered with `query` unset when the stops feed is down.
* `services/cameras.py`: `camera_frame(frames, camera_id, store)` wraps `FrameSource.get_frame` in an `Envelope[CameraFrame]` and flushes the source's telemetry through `persist_frame_telemetry`, which writes to a writable store or just drains the buffer otherwise. `nearest_camera(env, query)` returns the closest online camera within the radius.
* `services/warehouse.py`: `warehouse(store, sql, max_rows)` runs `Store.query_readonly` (single `SELECT`/`WITH`, allowlisted tables, forbidden-keyword scan) and returns an `Envelope[WarehouseResult]` with `stale_after == fetched_at`. Cells are coerced to JSON-safe values.
* `services/density.py`: `density_now` and `density_history` aggregate `density_samples` per frame (sum of person counts and of `VEHICLE_CLASSES` counts per `(camera_id, ts)`), then per camera or per `time_bucket`. Camera name and coordinates come from the `cameras` table, with an optional in-memory `Camera` list as a fallback so the tools work before nyc-vision has upserted metadata.

### Density with no samples

`density_samples` is empty until nyc-vision has run against real cameras, which has not happened yet. Both density functions then return `status="error"`, `error.kind="not_configured"` with a message naming the empty window; they never return an empty success. A missing store is `error.kind="internal"`. The MCP tools pass the cached DOT camera list as the location fallback (`camera_fallback` in `server.py`), and the dashboard passes the same list from `api.py::_density`.

### `packages/nyc-mcp`

`server.py::create_server(services=None, settings=None)` builds the FastMCP server; its lifespan builds `Services` on the first session unless a pre-built one is injected (tests). Tools are registered in four groups (cameras, subway, civic, store-backed) and each returns `Envelope.model_dump(mode="json")`, except `get_camera_frame` (image block plus envelope) and `feed_health` (a plain status dict). Feed failures are never MCP `isError`; that is reserved for invalid arguments. `__main__.py` is the `nyc-mcp` console script: stdio by default, `--http` for Streamable HTTP, logging to stderr. Install instructions and the per-tool reference are in `docs/mcp.md`.

## Phase 3: vision

`packages/nyc-vision` is the only writer of `density_samples` and `camera_frame_fetches`, and it maintains the `cameras` table so aggregates can be located. It is a writer only; the read path is `services/density.py`, which `nyc_vision/service.py` re-exports rather than reimplementing, so the MCP tools, the dashboard and the package cannot disagree about what density means. Full reference in `docs/vision.md`.

### `detector.py`

`Detector` is a protocol over "JPEG bytes in, per-class counts out". `YoloDetector` imports `ultralytics` and `torch` lazily inside `_load()`, so the package and its tests import on a machine with neither and `just check` stays green; only a real detection needs the wheels (the exact `pyproject.toml` lines the orchestrator must add are in `packages/nyc-vision/README.md` and repeated in `docs/vision.md`). The model is loaded once and `detect()` holds a lock, so one instance can be handed to `asyncio.to_thread` from several tasks. `COCO_CLASS_MAP` keeps only the six `contracts.DetectionClass` members; every other COCO class is dropped. `summarize_boxes` is a pure function over the class ids, confidences and boxes, clamping box-area fraction into [0, 1] so a box overhanging the frame cannot produce a value the frozen `DensitySample` bounds would reject. `FakeDetector` (`model_name` prefixed `fake:`, `is_synthetic = True`) exists so the pipeline and the store writes can be tested without torch, and is never wired into a real run.

### `sampling.py`

`select_cameras` is deterministic with no RNG and no borough lookup table. Online, in-bbox cameras are bucketed into a `grid` x `grid` mesh over `contracts.NYC_BBOX` (default 5x5), cells sorted by (row, col) and cameras inside a cell by id, then one camera per cell per pass is taken round-robin. A dense area cannot crowd out a sparse one until the sparse cells are exhausted, and reordering the DOT list upstream does not change the selection. A shortage of eligible cameras is logged through `SelectionStats`, never padded.

### `pipeline.py`

One tick: refresh and stratify the camera list, upsert the selection into `cameras`, fetch frames through `FrameSource.get_frame()` up to `NYC_VISION_CONCURRENCY` in parallel and run the detector in a worker thread behind a lock, write one `DensitySample` per camera per class including zero counts, drain the frame source's `CameraFrameFetch` telemetry into `camera_frame_fetches`, and evict expired frames. The package never builds a DOT image URL, so the 2 s per-camera cadence and the in-memory buffer stay owned by `CameraFrameSource`. A per-camera failure is counted and logged and never raises out of the tick; a detector failure is counted as `TickResult.cameras_failed` and deliberately not written into `camera_frame_fetches`, so a model problem cannot move the frame-fetch gate metric in either direction. `run_forever` measures the interval from the start of the tick so an overrun does not compound, and `maybe_archive` writes yesterday's rows to Parquet once per day through `Store.archive_day`.

### `report.py`, `chart.py`, `__main__.py`

`report.py` measures the gate off the tables and nothing else: `frame_failure_rate` over `camera_frame_fetches`, `cameras_covered` over `density_samples`, and `hourly_rush` for the local-hour profile. An empty window returns zero counts and `None` timestamps and fails the gate rather than reporting 0 %. Every query selects `epoch_ms(ts)` and converts in Python, because DuckDB needs `pytz` to hand back a `TIMESTAMPTZ` and that is not a dependency; local-hour bucketing uses `zoneinfo`, which also keeps the SQL free of the ICU extension. `chart.py` draws the two polylines with Pillow (matplotlib is not a dependency), leaves hours with no frames as gaps, and raises `NoChartData` rather than writing an empty chart. `__main__.py` is the `nyc-vision` console script with `run`, `once`, `report` and `chart`; `run` and `once` open DuckDB read-write, `report` and `chart` open it read-only.

## Phase 4: dashboard

`packages/nyc-dash` is a pure consumer of `nyc_live.services`. It never imports `nyc_live.feeds`, never touches an upstream, and never fabricates a record. Full reference in `docs/dashboard.md`.

### `api.py`

A table of `FeedRoute` records (key, `FeedName`, label, handler, geo support, default limit and radius, aliases, description) is the single source of the dashboard's endpoints, the `/api` index and the SSE event names. Handlers call the same service functions the MCP tools call (`nearby`, `subway_arrivals`, `alerts_near`, `density_now`) and return the `Envelope` untouched; the HTTP layer only serialises it. `not_registered` and `crashed` produce honest error envelopes for a missing adapter module or an unexpected handler exception, so a hole in the registry is visible rather than blank. `health_payload` returns `FeedRegistry.health()` plus the store state in the same shape as the MCP `feed_health` tool.

### `app.py`

`create_app(services=None)` builds the `Services` in the lifespan unless one is injected (tests pass a pre-built one, so no network and no on-disk DuckDB is touched). Routes: `/api` (the route table), `/api/health`, `/api/stream`, `/api/{feed}`, and the static page mounted at `/`. A down feed is HTTP 200 with `status="error"` because the browser needs the envelope to render the layer's pill; 4xx is reserved for bad requests (unknown key, `lat` without `lon`, a geo filter on a feed whose records have no coordinates, out-of-range arguments).

### `stream.py`

`/api/stream` yields a `ready` event, then one event per feed per cycle named by the route key, then a `health` event, then waits `interval_s` in 0.25 s slices with a keepalive comment about every 15 s, checking for client disconnect throughout. A failing feed is pushed as its own error envelope so one dead feed never stops the others. The stream calls the same handlers as `/api/<feed>`, so `CachedFeed` still owns every TTL decision; it is an optimisation over polling those endpoints, and `static/app.js` falls back to `setInterval` polling on the first `EventSource` error.

### `static/`

One `index.html`, no build step, and a set of plain deferred scripts loaded in dependency order (`static/js/`: `utils.js`, `icons.js`, `state.js`, `map-layers.js`, `alerts-banner.js`, `status-panel.js`, `detail-panel.js`, `search.js`, `data-sync.js`, `app.js`) plus several stylesheets (`static/css/`: `tokens.css`, `chrome.css`, `layers-panel.css`, `search.css`, `detail-panel.css`, `alerts-banner.css`). MapLibre GL 4.7.1 and deck.gl 9.0.0 come from pinned unpkg URLs and the basemap is OpenFreeMap's keyless `liberty` style, so no API key exists anywhere. Each layer's pill is driven by `envelope.status` and nothing else: `fresh` shows the count and time, `stale` keeps the last good records and shows when they were good plus the refresh error, `error` hides the layer and shows the message. Nothing is ever drawn from placeholder data. If the CDN is unreachable a banner says so and the pills keep working without a map. Full reference to the interactive features (search, borough filter, service alerts, shareable URLs, the detail panel, mobile layout) is in `docs/dashboard.md`.
