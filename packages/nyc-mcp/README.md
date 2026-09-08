# nyc-mcp

MCP server that exposes NYC live civic data to Claude Code, Claude Desktop, and any
other MCP client. It is a thin FastMCP layer over `nyc_live.services`; the same
service functions back the `nyc-dash` dashboard.

## Install and register

From a checkout with `uv` installed:

```sh
uv sync --all-packages --group dev          # or: just sync
uv run nyc-mcp --help
```

Register with Claude Code (stdio transport, the default):

```sh
claude mcp add nyc-live -- uv run --directory /path/to/nyc-live nyc-mcp
claude mcp list        # should show: nyc-live: uv run --directory ... nyc-mcp - ✓ Connected
claude mcp get nyc-live
```

Claude Desktop (`claude_desktop_config.json`):

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

Streamable HTTP instead of stdio:

```sh
uv run nyc-mcp --http --host 127.0.0.1 --port 8765     # endpoint: http://127.0.0.1:8765/mcp
claude mcp add --transport http nyc-live http://127.0.0.1:8765/mcp
```

Settings come from the environment or `.env` (see `.env.example` at the repo root).
Nothing is required for the public feeds; `MTA_BUS_TIME_API_KEY` and `NY511_API_KEY`
unlock the key-gated ones. Without a key those feeds report `configured: false` in
`feed_health`, with `status: "never_fetched"` until something tries to read them (no
tool does today); a read attempt turns that into `status: "error"` with
`last_error.kind = "not_configured"`. Note that `feed_health` returns `FeedHealth`
rows rather than an Envelope, so the field is `last_error`, not `error`.

### DuckDB store

`query_warehouse`, `density_now`, and `density_history` read the DuckDB file at
`NYC_LIVE_DUCKDB_PATH` (default `./data/nyc_live.duckdb`). The server is a reader:

* If the file exists it is opened **read-only**, so it can coexist with other readers.
  Feed telemetry (`feed_fetches`, `camera_frame_fetches`) is then not recorded by this
  process.
* If the file does not exist yet (fresh checkout, `nyc-vision` never ran) it is created
  **writable** once so the frozen schema exists and the warehouse has tables to describe.
  In that case telemetry is recorded.
* If the file cannot be opened at all (another process holds the write lock, corrupt file)
  the server still starts; the store-backed tools return `status="error"`,
  `error.kind="internal"` with the reason, and `feed_health.store.open` is `false`.

## Tools

Every data tool returns an `Envelope` (`nyc_live.contracts.Envelope`) serialised as JSON:
`feed`, `status` (`fresh` | `stale` | `error`), `fetched_at`, `stale_after`, `records`,
`error`, `query`, `total_before_filter`, `truncated`. `stale` means the last good snapshot
is being served and `error` explains why it could not be refreshed; `error` means no usable
data (`records == []`, `error` set). Feed failures are never MCP `isError` results; that is
reserved for invalid arguments (for example `lat` without `lon`).

| Tool | Returns | Filters | Freshness |
|------|---------|---------|-----------|
| `list_cameras` | `Envelope[Camera]` | `lat`, `lon`, `radius_m`, `limit`, `online_only` | 10 min |
| `nearby_cameras` | `Envelope[Camera]` nearest first | `lat`, `lon` (required), `radius_m`, `limit` | 10 min |
| `get_camera_frame` | MCP image block + `Envelope[CameraFrame]` | `camera_id`, or `lat`/`lon`/`radius_m` for the nearest online camera | 2 s per camera; frames are memory-only |
| `subway_arrivals` | `Envelope[SubwayArrival]` soonest first | `stop_id` (station or platform), `lat`, `lon`, `radius_m`, `horizon_s`, `limit` | 30 s |
| `subway_alerts` | `Envelope[SubwayAlert]` | `route_id`, `lat`, `lon`, `radius_m` (via stops served nearby), `limit` | 60 s |
| `citibike_status` | `Envelope[BikeStation]` | `lat`, `lon`, `radius_m`, `limit` | 60 s |
| `nearby_311` | `Envelope[ServiceRequest]` | `lat`, `lon`, `radius_m`, `complaint_type`, `limit` | 5 min |
| `weather_now` | `Envelope[WeatherReport]` | `lat`, `lon`, `radius_m` (default 50 km), `limit` | 5 min |
| `query_warehouse` | `Envelope[WarehouseResult]` | `sql` (single SELECT/WITH over the read-only tables), `max_rows` | computed per call |
| `feed_health` | per-feed `FeedHealth` list + store status | none | instant, no fetch |
| `density_now` | `Envelope[CameraDensity]` per camera | `lat`, `lon`, `radius_m`, `camera_id`, `window_s`, `limit` | 60 s |
| `density_history` | `Envelope[CameraDensity]` per camera and bucket | `camera_id`, `lat`, `lon`, `radius_m`, `hours`, `bucket_s`, `limit` | 60 s |

`density_now` / `density_history` return `status="error"`, `error.kind="not_configured"`
("nyc-vision has not produced density samples yet") until Phase 3's detector has written
`density_samples` rows. That is the honest answer, not a fault.

## Layout

```
packages/nyc-mcp/src/nyc_mcp/
  server.py     create_server(services=None) -> FastMCP; all tool definitions
  __main__.py   `nyc-mcp` console script: --http, --host, --port, --path, --log-level
src/nyc_live/services/
  registry.py   build_services / open_services: one httpx client, FeedRegistry, frame source, store
  nearby.py     nearby(envelope, GeoQuery, limit), geo_query(lat, lon, radius_m)
  subway.py     subway_arrivals(trips, stops, query|stop_id, horizon, limit), alerts_near(...)
  cameras.py    camera_frame(frames, camera_id, store) -> Envelope[CameraFrame], nearest_camera
  density.py    density_now(store, query, window), density_history(store, query, window, bucket)
  warehouse.py  warehouse(store, sql) -> Envelope[WarehouseResult]
```

Tests: `tests/services/` (pure functions over hand-built envelopes and an in-memory
DuckDB) and `tests/mcp/` (in-process `fastmcp.Client` against `create_server` with fake
adapters; no network).
