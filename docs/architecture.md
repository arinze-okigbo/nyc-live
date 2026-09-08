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
             FastMCP tools (Phase 2)     YOLO over frames      FastAPI + MapLibre/Deck.gl
             Envelope[T] out             (Phase 3, scaffold)   (Phase 4, scaffold)
```

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

### `feeds/bus.py` (MTA Bus Time, deferred)

`BusPositionsAdapter` (`mta_bus`, 30 s) is a registry-visible stub. `is_configured()` is False without `MTA_BUS_TIME_API_KEY` and `fetch()` raises `FeedNotConfigured`. With a key set it still raises `FeedUnavailable(kind=INTERNAL, "not implemented")` rather than returning anything. The key is never logged.

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

### Density stubs

Phase 3 has not run, so `density_samples` is empty. Both density functions return `status="error"`, `error.kind="not_configured"` with a message naming the empty window; they never return an empty success. A missing store is `error.kind="internal"`. The MCP tools pass the cached DOT camera list as the location fallback (`camera_fallback` in `server.py`).

### `packages/nyc-mcp`

`server.py::create_server(services=None, settings=None)` builds the FastMCP server; its lifespan builds `Services` on the first session unless a pre-built one is injected (tests). Tools are registered in four groups (cameras, subway, civic, store-backed) and each returns `Envelope.model_dump(mode="json")`, except `get_camera_frame` (image block plus envelope) and `feed_health` (a plain status dict). Feed failures are never MCP `isError`; that is reserved for invalid arguments. `__main__.py` is the `nyc-mcp` console script: stdio by default, `--http` for Streamable HTTP, logging to stderr. Install instructions and the per-tool reference are in `docs/mcp.md`.
