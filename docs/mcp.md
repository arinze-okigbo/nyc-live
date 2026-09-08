# nyc-mcp: install and tool reference

`nyc-mcp` (`packages/nyc-mcp/src/nyc_mcp/server.py`) is a FastMCP server over the service layer in `src/nyc_live/services/`. This page covers installation, the response shape every tool shares, each tool's parameters and failure behaviour, the DuckDB store policy, and the HTTP transport. The tool docstrings in `server.py` are the source of truth; this page restates them.

## Install

From a checkout with `uv` installed:

```sh
uv sync --all-packages --group dev          # or: just sync
uv run nyc-mcp --help
```

Settings come from the environment or `.env` (`src/nyc_live/config.py`; see `.env.example`). Nothing is required for the public feeds. `MTA_BUS_TIME_API_KEY` and `NY511_API_KEY` unlock the key-gated feeds; without them `feed_health` reports those feeds with `configured: false`.

### Claude Code

Stdio transport, the default:

```sh
claude mcp add nyc-live -- uv run --directory /path/to/nyc-live nyc-mcp
claude mcp list        # should show: nyc-live: uv run --directory ... nyc-mcp - ✓ Connected
claude mcp get nyc-live
```

`/path/to/nyc-live` must be the absolute path of the checkout; `uv run --directory` resolves the workspace and virtualenv from there.

### Claude Desktop

Add this to `claude_desktop_config.json` and restart Claude Desktop:

```json
{
  "mcpServers": {
    "nyc-live": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/nyc-live", "nyc-mcp"]
    }
  }
}
```

If `uv` is not on the PATH that Claude Desktop inherits, use its absolute path as `command`.

### Streamable HTTP (`--http`)

`packages/nyc-mcp/src/nyc_mcp/__main__.py` accepts `--http`, `--host` (default `127.0.0.1`), `--port` (default `8765`), `--path` (default `/mcp`), and `--log-level`. Logging always goes to stderr because stdout is the MCP wire on stdio.

```sh
uv run nyc-mcp --http --host 127.0.0.1 --port 8765     # endpoint: http://127.0.0.1:8765/mcp
claude mcp add --transport http nyc-live http://127.0.0.1:8765/mcp
```

`just mcp` forwards its arguments to `uv run nyc-mcp`.

## The Envelope every tool returns

Every data tool returns `nyc_live.contracts.Envelope` serialised with `model_dump(mode="json")`:

```json
{
  "feed": "citibike",
  "status": "fresh",
  "fetched_at": "2026-09-08T14:02:11.412Z",
  "stale_after": "2026-09-08T14:03:11.412Z",
  "records": [],
  "error": null,
  "query": {"lat": 40.7484, "lon": -73.9857, "radius_m": 500.0},
  "total_before_filter": 2103,
  "truncated": false
}
```

| Field | Meaning |
|-------|---------|
| `feed` | `FeedName` the records came from. |
| `status` | `fresh`: snapshot younger than the feed's TTL. `stale`: TTL expired and the refresh failed; `records` is the last good snapshot and `error` explains why it could not be refreshed. `error`: no usable data; `records` is empty and `error` says why. |
| `fetched_at` | When the served snapshot was fetched. `null` on `error`. |
| `stale_after` | `fetched_at` plus the feed's TTL (`contracts.DEFAULT_TTL`), or the upstream's larger published TTL for GBFS. `null` on `error`. |
| `records` | The typed records. Always `[]` when `status` is `error`. |
| `error` | `FeedError`: `kind` (`not_configured`, `upstream_http`, `upstream_timeout`, `upstream_parse`, `rate_limited`, `not_found`, `internal`), `message`, `feed`, `url`, `upstream_status`, `occurred_at`, `retry_after_s`. `null` when `fresh`. |
| `query` | The `GeoQuery` that was applied (`lat`, `lon`, `radius_m`), or `null` if none was. |
| `total_before_filter` | Size of the whole snapshot before geo and limit filtering. |
| `truncated` | `true` when `limit` cut the result. |

How a client should read it, in order:

1. Check `status`. Do not assume success because the call did not fail.
2. On `stale`, use `records` but show `fetched_at` as the data's age and surface `error.message`. A snapshot stays servable as stale for TTL plus 30 minutes (`DEFAULT_MAX_STALE` in `src/nyc_live/cache.py`), after which the feed reports `error`.
3. On `error`, there is nothing to render. `error.kind` tells you whether to retry (`upstream_timeout`, `rate_limited` with `retry_after_s`), refresh an id (`not_found`), or stop (`not_configured`).
4. When `query` is set, every record carries `distance_m` and results are nearest first (`src/nyc_live/geo.py::filter_nearby`).

Feed failures are never MCP `isError` results. `isError` is reserved for invalid arguments, for example `lat` given without `lon`, or `get_camera_frame` called with neither `camera_id` nor a point.

## Tools

### `list_cameras`

Parameters: `lat`, `lon` (optional, together), `radius_m` (default 1000), `limit` (default 200), `online_only` (default false).

Returns `Envelope[Camera]` from `FeedName.DOT_CAMERAS` (10 minute TTL): `id`, `source`, `name`, `lat`, `lon`, `is_online`, `image_url`, `area`, `roadway`, `direction`. DOT camera ids are UUIDs that rotate, so do not cache them across days. With a point, only cameras within `radius_m`, nearest first; otherwise the first `limit` cameras. `online_only` filters after the geo filter and keeps `total_before_filter` at the snapshot size.

When the feed is down: `stale` serves the last known list; `error` means no list is available and `error.message` says why (implemented by `CameraListAdapter` in `src/nyc_live/feeds/cameras.py`).

### `nearby_cameras`

Parameters: `lat`, `lon` (required), `radius_m` (default 1000), `limit` (default 20).

Same data and freshness as `list_cameras`; `distance_m` is set on every record. Same failure behaviour.

### `get_camera_frame`

Parameters: `camera_id`, or `lat` and `lon` with `radius_m` (default 1000) to pick the nearest online camera. One of the two is required.

Returns an MCP image content block (JPEG) followed by a text block holding an `Envelope[CameraFrame]` from `FeedName.DOT_CAMERA_FRAMES`: `camera_id`, `fetched_at`, `stale_after` (about 2 s later; `CAMERA_FRAME_MIN_INTERVAL`), `content_type`, `byte_size`, `width`, `height`. The bytes are excluded from the JSON. `CameraFrameSource` in `src/nyc_live/feeds/cameras.py` serves a buffered frame if it is younger than 2 s, otherwise waits on the per-camera rate limiter and fetches. Frames live in memory only; see `docs/privacy.md`.

When something is down, no image block is returned and the envelope explains:

* `error.kind="not_found"`: the id is unknown or has rotated (upstream 404); re-run `list_cameras`. Also used when no online camera is within `radius_m` of the point.
* Other kinds (`upstream_http`, `upstream_timeout`, `upstream_parse` for a non-JPEG body): the frame fetch failed.
* If a point was given and the camera list itself is in `error`, the camera list's error envelope is returned as-is, so `feed` will read `dot_cameras` rather than `dot_camera_frames`.

### `subway_arrivals`

Parameters: `stop_id` (a GTFS stop such as `127` or a platform such as `127N`), `lat`, `lon`, `radius_m` (default 500), `horizon_s` (default 1800), `limit` (default 50). `stop_id` and the point may be combined.

Returns `Envelope[SubwayArrival]` under `FeedName.MTA_SUBWAY`, soonest first: `stop_id`, `stop_name`, `route_id`, `trip_id`, `direction` (`N`/`S`), `arrival` (UTC), `eta_s`, and the platform's `lat`/`lon`/`distance_m`. Built by `src/nyc_live/services/subway.py::subway_arrivals` from the realtime trips feed (30 s TTL) joined to the static stop list (24 h TTL). Parent station ids match every platform beneath them. Only arrivals inside `horizon_s` are returned.

When a feed is down: if either the trips or the stops envelope is `error`, the result is an `error` envelope carrying that feed's error. If either is `stale`, the result is `stale` with the error attached and ETAs come from the older snapshot; treat them as approximate. `fetched_at` and `stale_after` are those of the trips feed.

### `subway_alerts`

Parameters: `lat`, `lon`, `radius_m` (default 800), `route_id` (for example `A` or `7`, case-insensitive), `limit` (default 100).

Returns `Envelope[SubwayAlert]` from `FeedName.MTA_SUBWAY_ALERTS` (60 s TTL): `id`, `header`, `description`, `active_from`, `active_until`, `routes`, `stop_ids`, `effect`. `updated_at` is always `null` (the MTA publishes it only in a protobuf extension the project does not compile; see the `src/nyc_live/feeds/transit.py` docstring). With a point, an alert is kept when it names a route or stop served within `radius_m`; the join goes through the static stop list (`services/subway.py::alerts_near`).

When a feed is down: `stale` serves the last alerts; `error` means the alerts feed is unavailable. If the stop list is in `error` while a point was given, the alerts come back unfiltered and `query` is left `null` to signal that no geo filter was applied.

### `citibike_status`

Parameters: `lat`, `lon`, `radius_m` (default 1000), `limit` (default 50).

Returns `Envelope[BikeStation]` from `FeedName.CITIBIKE` (60 s TTL, or the GBFS-published `ttl` if larger): `station_id`, `name`, `capacity`, `bikes_available`, `ebikes_available` (may be `null` when the feed does not publish it), `docks_available`, `is_renting`, `is_returning`, `is_installed`, `last_reported`, `lat`, `lon`. Without a point, the first `limit` of roughly 2,000 stations.

When the feed is down: `stale` serves the last counts; `error` means the GBFS root or one of its child feeds was unreachable or did not parse (`CitiBikeAdapter` in `src/nyc_live/feeds/micromobility.py`).

### `nearby_311`

Parameters: `lat`, `lon`, `radius_m` (default 1000), `limit` (default 50), `complaint_type` (case-insensitive substring, for example `noise`).

Returns `Envelope[ServiceRequest]` from `FeedName.NYC_311` (5 minute TTL): every request created in the last 24 hours city-wide (`Nyc311Adapter` in `src/nyc_live/feeds/socrata.py`), with `created_at`, `closed_at`, `agency`, `complaint_type`, `descriptor`, `status`, `borough`, `incident_zip`, `incident_address`, `location_type`, and `lat`/`lon` when the city geocoded the request. Ungeocoded requests are kept in the snapshot but dropped whenever a point is given.

When the feed is down: `stale` serves the last page set; `error` means `data.cityofnewyork.us` was unreachable or the rows did not parse.

### `weather_now`

Parameters: `lat`, `lon`, `radius_m` (default 50 000), `limit` (default 10).

Returns `Envelope[WeatherReport]` from `FeedName.WEATHER` (5 minute TTL): one record per NWS observation station resolved from the Central Park, LaGuardia, and JFK lookup points (`WeatherAdapter` in `src/nyc_live/feeds/weather.py`), each with `station_id`, `station_name`, `lat`, `lon`, an `observation` (`observed_at`, `text`, `temperature_c`, `dewpoint_c`, `humidity_pct`, `wind_speed_kmh`, `wind_gust_kmh`, `wind_direction_deg`, `pressure_pa`, `visibility_m`, `precip_last_hour_mm`; `null` when weather.gov reports null) and a short `forecast` list. The default 50 km radius means the nearest station is always included.

When the feed is down: a single failing lookup point is skipped and logged; the snapshot still succeeds with the others. `error` means no station could be fetched at all, and `error.message` lists the reason per lookup point.

### `query_warehouse`

Parameters: `sql` (required), `max_rows` (default 500, clamped to 1 to 5000).

Returns `Envelope[WarehouseResult]` from `FeedName.WAREHOUSE` with exactly one record: `sql`, `columns`, `rows`, `row_count`, `truncated`, `elapsed_ms`. `stale_after == fetched_at`, so re-run rather than cache. Only a single `SELECT` or `WITH` statement over `cameras`, `camera_frame_fetches`, `density_samples`, `feed_fetches`, `bike_station_status`, `weather_observations`, `service_requests_311`, and `subway_positions` is accepted (`WAREHOUSE_READ_ONLY_TABLES` in `contracts.py`, enforced by `Store.query_readonly` in `src/nyc_live/store.py`). Timestamps come back as ISO-8601 UTC strings.

When it cannot answer: DML, DDL, multiple statements, or an unknown table produce `status="error"`, `error.kind="internal"`, with the reason in `error.message`. The same kind is used when the DuckDB file could not be opened. Tables may be empty on a fresh install; that is an empty `rows` list with `status="fresh"`, since an empty query result is a real answer.

### `feed_health`

No parameters. This is the one tool that does not return an `Envelope`. It returns:

```json
{
  "checked_at": "...",
  "store": {"path": "data/nyc_live.duckdb", "open": true, "read_only": true},
  "feeds": [ {"feed": "...", "configured": true, "status": "fresh", "ttl_s": 60.0,
              "last_ok_at": "...", "last_error": null, "consecutive_failures": 0,
              "record_count": 2103}, ... ]
}
```

Each entry is a `FeedHealth` from `FeedRegistry.health()` (`src/nyc_live/cache.py`). `status` is `never_fetched` until some tool has read that feed in this process; after that it is `fresh`, `stale`, or `error`. Key-gated feeds (`mta_bus`, `ny511_cameras`) show `configured: false`; no data tool reads them, so they normally stay `never_fetched`. Nothing is fetched by this call. Call it first when other tools return `error` to see whether one feed or all of them are affected.

### `density_now`

Parameters: `lat`, `lon`, `radius_m` (default 1000), `camera_id`, `window_s` (default 300), `limit` (default 100).

Returns `Envelope[CameraDensity]` from `FeedName.DENSITY` (60 s TTL): one record per camera with samples in the trailing `window_s`, carrying `person_mean`, `vehicle_mean`, `person_max`, `vehicle_max` (counts per frame), `sample_count`, `latest_ts`, `name`, `lat`, `lon`. Computed by `src/nyc_live/services/density.py` from `density_samples` in DuckDB; aggregates only, no imagery.

When it cannot answer: until nyc-vision has written rows, `status="error"`, `error.kind="not_configured"` with a message saying no samples exist in the window. That is the expected answer today, not a fault: the pipeline is built but has never been run against real cameras (`docs/vision.md`), so the table is empty. `error.kind="internal"` means the DuckDB file could not be opened, the query failed, or samples exist but no camera in the window has a location in the `cameras` table.

### `density_history`

Parameters: `camera_id`, `lat`, `lon`, `radius_m` (default 1000), `hours` (default 24), `bucket_s` (default 900), `limit` (default 500).

Returns `Envelope[CameraDensity]`, one record per (camera, bucket) with `window_start`/`window_end` set to the bucket bounds, ordered by camera then time. `limit` caps the number of rows and sets `truncated`. Failure behaviour is the same as `density_now`.

## DuckDB store policy

`query_warehouse`, `density_now`, and `density_history` read the DuckDB file at `settings.duckdb_path` (`NYC_LIVE_DUCKDB_PATH`, default `./data/nyc_live.duckdb`). The server is a reader; the policy is `open_store_for_reader` in `src/nyc_live/services/registry.py`:

* If the file exists it is opened read-only, so it can coexist with other readers. Feed telemetry (`feed_fetches`, `camera_frame_fetches`) is not recorded by this process.
* If the file does not exist yet (fresh checkout, nyc-vision never ran) it is created writable once so the frozen schema (`contracts.DUCKDB_SCHEMA`) exists and the warehouse has tables to describe. In that case telemetry is recorded.
* If the file cannot be opened at all (another process holds the write lock, corrupt file) the server still starts with `store=None`; the store-backed tools return `status="error"`, `error.kind="internal"` with the reason, and `feed_health.store.open` is `false`.

`Services` (`registry.py`) holds one shared `httpx.AsyncClient`, the `FeedRegistry`, the `CameraFrameSource`, and the store. The server's lifespan builds it on the first session and closes it on shutdown.

## Tests

`tests/mcp/test_server.py` drives `create_server` in-process with `fastmcp.Client` and fake adapters; `tests/services/` covers the pure functions and the density SQL over an in-memory DuckDB. No network is used by either.
