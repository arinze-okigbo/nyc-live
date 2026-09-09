# nyc-live

Live NYC civic data, three ways:

1. **nyc-mcp**: an MCP server exposing subway (arrivals, alerts, static route shapes), cameras, MTA bus positions, Citi Bike, 311, restaurant inspections, and weather as typed tools for Claude Code and Claude Desktop.
2. **nyc-vision**: a pedestrian and vehicle density pipeline running YOLO over NYC DOT traffic camera frames, writing counts to DuckDB.
3. **nyc-dash**: a single-page live map: subway positions and route shapes, camera density heatmap, 311 stream, weather (with a severe-alert indicator), Citi Bike levels, and an opt-in bus layer, plus a station/dock/camera/route search box, a borough filter, and a shareable map URL.

Layers 2 and 3 consume layer 1 through the shared service layer in `src/nyc_live/services`.

## Status

**All four phases are built.** The frozen contracts, the feed adapters (`src/nyc_live/feeds/`), the service layer (`src/nyc_live/services/`), the MCP server (`packages/nyc-mcp/`), the vision pipeline (`packages/nyc-vision/`) and the dashboard (`packages/nyc-dash/`) are all in the tree, and `just check` passes.

Three things remain, and all three need a machine with network access:

1. **Live upstream verification.** From the build sandbox only the MTA static GTFS zip on S3 (`https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip`) and the ultralytics weights host were reachable. The DOT camera list and frames, the MTA GTFS-realtime feeds, Citi Bike GBFS, Socrata, and weather.gov adapters are written against the documented public shapes and covered by offline tests, but they have not been run against their real upstreams and their replay fixtures have not been recorded.
2. **The Phase 3 gate** (24 h over 50 or more cameras, under 2 % frame-fetch failure, a chart showing AM and PM rush) has not been run. No `density_samples` row exists, so `density_now` and `density_history` answer `status="error"`, `error.kind="not_configured"`, and `docs/vision-rush.png` does not exist. Commands in `docs/vision.md`.
3. **The Phase 4 first-paint gate** (under 2 s on a warm cache) has not been measured, because `unpkg.com` and `tiles.openfreemap.org` are blocked from the build sandbox and the map cannot paint without them. Server-side response times were measured and are in `docs/dashboard.md`; the browser number is not guessed anywhere.

Two setup steps are yours to make before running the last two: add the torch dependency lines (`docs/vision.md`, copied from `packages/nyc-vision/README.md`) and add `playwright>=1.56` to the dev group (`docs/dashboard.md`). See `CLAUDE.md` for the phase plan and `docs/architecture.md` for the design.

## Quick start

```bash
uv sync --all-packages --group dev   # or: just sync
cp .env.example .env                 # optional keys; nothing is required
just check                           # ruff format, ruff check, pyright, pytest
just smoke                           # one line per feed against the real upstreams
```

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and [just](https://just.systems/).

### Register the MCP server

Claude Code (stdio transport):

```bash
claude mcp add nyc-live -- uv run --directory /path/to/nyc-live nyc-mcp
```

Claude Desktop, in `claude_desktop_config.json`:

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

Replace `/path/to/nyc-live` with the absolute path of your checkout. Streamable HTTP (`uv run nyc-mcp --http`), the DuckDB store policy, and every tool's parameters are in `docs/mcp.md`.

### Run the density pipeline

```bash
just vision once                 # one tick: camera list, frames, model, DuckDB
just vision run                  # tick every NYC_VISION_INTERVAL_S until Ctrl-C
just vision report -- --since 24h
```

`run` and `once` need `ultralytics`, `torch` and `torchvision`, which are not installed yet; the exact `pyproject.toml` lines to add are in `docs/vision.md`. Counts and box statistics go to DuckDB, frames never touch disk. Configuration (`NYC_VISION_*`), device selection for cpu, mps and cuda, and the Phase 3 gate are in `docs/vision.md`.

### Run the dashboard

```bash
just dash                        # http://127.0.0.1:8080, API docs at /docs
uv run nyc-dash --host 0.0.0.0 --port 9000
```

The page reads the service layer through `/api/<feed>` and `/api/stream`, and shows a status pill per layer. Endpoints, the SSE format and its polling fallback, and the per-layer degradation rules are in `docs/dashboard.md`.

## Tools

Every data tool returns an `Envelope` (`src/nyc_live/contracts.py`) with `status` set to `fresh`, `stale`, or `error`. The freshness column is `contracts.DEFAULT_TTL` for the tool's feed.

| Tool | Returns | Freshness |
|------|---------|-----------|
| `list_cameras` | `Envelope[Camera]`, the DOT camera list | 10 min |
| `nearby_cameras` | `Envelope[Camera]`, nearest first, `lat`/`lon` required | 10 min |
| `get_camera_frame` | MCP image block plus `Envelope[CameraFrame]` | 2 s per camera |
| `subway_arrivals` | `Envelope[SubwayArrival]`, soonest first | 30 s |
| `subway_alerts` | `Envelope[SubwayAlert]` | 60 s |
| `subway_route_shapes` | `Envelope[SubwayRouteShape]`, static GTFS route polylines | 24 h |
| `bus_positions` | `Envelope[BusVehicle]`, key-gated on `MTA_BUS_TIME_API_KEY` | 30 s |
| `citibike_status` | `Envelope[BikeStation]` | 60 s |
| `nearby_311` | `Envelope[ServiceRequest]` | 5 min |
| `weather_now` | `Envelope[WeatherReport]`, one per NWS station, includes active alerts | 5 min |
| `query_warehouse` | `Envelope[WarehouseResult]`, read-only SQL over DuckDB | computed per call |
| `feed_health` | per-feed `FeedHealth` list plus store status, no fetch | instant |
| `density_now` | `Envelope[CameraDensity]` per camera | 60 s |
| `density_history` | `Envelope[CameraDensity]` per camera and time bucket | 60 s |
| `camera_density_history` | `Envelope[CameraDensity]`, one camera's trend bucketed for a chart | 60 s |

## Layout

```
src/nyc_live/          core library: contracts (frozen), feeds/, cache, store, services/
packages/nyc-mcp/      MCP server (FastMCP)
packages/nyc-vision/   detection pipeline (ultralytics YOLO, CPU-first); writes density_samples
packages/nyc-dash/     FastAPI + MapLibre GL + deck.gl dashboard over the service layer
tests/                 offline tests run by default; `live`-marked tests need NYC_LIVE_TESTS=1
docs/                  architecture, privacy, MCP tools, vision pipeline, dashboard
.claude/agents/        subagent definitions used to build this repo
```

* `docs/architecture.md`: module map and what each phase added.
* `docs/mcp.md`: install, the Envelope, every tool's parameters and failure behaviour.
* `docs/vision.md`: the detection pipeline, `NYC_VISION_*`, device selection, the Phase 3 gate.
* `docs/dashboard.md`: endpoints, SSE and polling, per-layer degradation, measured numbers.
* `docs/privacy.md`: what is stored, what is not, and why the frame buffer exists.

## Data sources

| Feed | Upstream | Auth |
|------|----------|------|
| DOT traffic cameras | `https://webcams.nyctmc.org/api/cameras/` (list), `.../{id}/image` (JPEG) | none |
| MTA subway GTFS-realtime | `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct/{slug}` (trips), `.../camsys%2Fsubway-alerts` (alerts) | none |
| MTA subway static GTFS (stops) | `https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip` | none |
| Citi Bike GBFS | discovered from `https://gbfs.citibikenyc.com/gbfs/gbfs.json` | none |
| 311 (`erm2-nwe9`) and DOHMH inspections (`43nn-pn8j`) | `https://data.cityofnewyork.us` (Socrata) | optional `SOCRATA_APP_TOKEN` |
| Weather | `https://api.weather.gov` | none; descriptive User-Agent required |
| MTA Bus Time | `https://bustime.mta.info/api/siri/vehicle-monitoring.json` | `MTA_BUS_TIME_API_KEY`; implemented and verified live (`src/nyc_live/feeds/bus.py`) |
| 511NY cameras | `https://511ny.org/api/getcameras` | `NY511_API_KEY`; unverified live |

Base URLs are settings (`src/nyc_live/config.py`) and can be overridden with `NYC_LIVE_*_BASE` variables for tests. The legacy `web.mta.info/.../google_transit.zip` returns 403 and is not used.

No data is ever fabricated. When a feed is down, every tool says so in the envelope (`src/nyc_live/cache.py`).

## Recording test fixtures

Offline replay tests use recorded upstream responses under `tests/fixtures/<area>/`. Fixtures must be trimmed copies of real responses, never hand-written. Because most upstreams were unreachable from the build sandbox, only one fixture is checked in today: `tests/fixtures/transit/gtfs_subway_trimmed.zip`. Every other replay test calls `pytest.skip("fixture not recorded yet: <path>")` until its files exist.

To record them, run the commands in each area's `RECORD.md` on a machine with network access:

* `tests/fixtures/transit/RECORD.md`: GTFS-realtime protobufs for the eight NYCT slugs and the alerts feed, trimmed with `gtfs-realtime-bindings`.
* `tests/fixtures/cameras/RECORD.md`: `cameras.json` (20 entries) and one `frame.jpg`. See `docs/privacy.md` on the frame fixture.
* `tests/fixtures/micromobility/RECORD.md`: `gbfs.json`, `station_information.json`, `station_status.json` (20 matching stations), optional `vehicle_types.json`.
* `tests/fixtures/civic/RECORD.md`: one 311 page, one inspections page, and the four-step weather.gov chain for Central Park.

Tests marked `live` hit the real upstreams and are skipped unless `NYC_LIVE_TESTS=1` is set (`tests/conftest.py`). `just test-live` sets it for you.

## Privacy

Camera frames are processed in memory and discarded. Only counts and bounding-box statistics are stored: `density_samples` rows plus `camera_frame_fetches` telemetry, both written by nyc-vision. The dashboard never receives a frame, only aggregates through the service layer. See `docs/privacy.md`.
