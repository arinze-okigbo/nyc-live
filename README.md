# nyc-live

Live NYC civic data, three ways:

1. **nyc-mcp**: an MCP server exposing subway, cameras, Citi Bike, 311, restaurant inspections, and weather as typed tools for Claude Code and Claude Desktop.
2. **nyc-vision**: a pedestrian and vehicle density pipeline running YOLO over NYC DOT traffic camera frames, writing counts to DuckDB.
3. **nyc-dash**: a single-page live map: subway positions, camera density heatmap, 311 stream, weather, Citi Bike levels.

Layers 2 and 3 consume layer 1 through the shared service layer in `src/nyc_live/services`.

## Status

**Phases 0 to 2 complete.** The frozen contracts, the feed adapters (`src/nyc_live/feeds/`), the service layer (`src/nyc_live/services/`), and the MCP server (`packages/nyc-mcp/`) are in the tree and `just check` passes. The vision pipeline (Phase 3) and the dashboard (Phase 4) are package scaffolds only; `density_now` and `density_history` answer `status="error"`, `error.kind="not_configured"` until Phase 3 writes rows.

Live verification is partial. From the build sandbox only the MTA static GTFS zip on S3 (`https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip`) was reachable, and that is the only upstream the adapters have been run against for real. The DOT camera list and frames, the MTA GTFS-realtime feeds, Citi Bike GBFS, Socrata, and weather.gov adapters are written against the documented public shapes and covered by offline tests with hand-built payloads, but they have not yet been run against their real upstreams. Live verification of those feeds, and recording their replay fixtures, is pending on a machine with upstream access. See `CLAUDE.md` for the phase plan and `docs/architecture.md` for the design.

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

## Tools

Every data tool returns an `Envelope` (`src/nyc_live/contracts.py`) with `status` set to `fresh`, `stale`, or `error`. The freshness column is `contracts.DEFAULT_TTL` for the tool's feed.

| Tool | Returns | Freshness |
|------|---------|-----------|
| `list_cameras` | `Envelope[Camera]`, the DOT camera list | 10 min |
| `nearby_cameras` | `Envelope[Camera]`, nearest first, `lat`/`lon` required | 10 min |
| `get_camera_frame` | MCP image block plus `Envelope[CameraFrame]` | 2 s per camera |
| `subway_arrivals` | `Envelope[SubwayArrival]`, soonest first | 30 s |
| `subway_alerts` | `Envelope[SubwayAlert]` | 60 s |
| `citibike_status` | `Envelope[BikeStation]` | 60 s |
| `nearby_311` | `Envelope[ServiceRequest]` | 5 min |
| `weather_now` | `Envelope[WeatherReport]`, one per NWS station | 5 min |
| `query_warehouse` | `Envelope[WarehouseResult]`, read-only SQL over DuckDB | computed per call |
| `feed_health` | per-feed `FeedHealth` list plus store status, no fetch | instant |
| `density_now` | `Envelope[CameraDensity]` per camera | 60 s |
| `density_history` | `Envelope[CameraDensity]` per camera and time bucket | 60 s |

## Layout

```
src/nyc_live/          core library: contracts (frozen), feeds/, cache, store, services/
packages/nyc-mcp/      MCP server (FastMCP)
packages/nyc-vision/   detection pipeline (ultralytics YOLO, CPU-first); Phase 3, scaffold only
packages/nyc-dash/     FastAPI + MapLibre GL + Deck.gl dashboard; Phase 4, scaffold only
tests/                 offline tests run by default; `live`-marked tests need NYC_LIVE_TESTS=1
docs/                  architecture, privacy, MCP install and tool reference
.claude/agents/        subagent definitions used to build this repo
```

## Data sources

| Feed | Upstream | Auth |
|------|----------|------|
| DOT traffic cameras | `https://webcams.nyctmc.org/api/cameras/` (list), `.../{id}/image` (JPEG) | none |
| MTA subway GTFS-realtime | `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct/{slug}` (trips), `.../camsys%2Fsubway-alerts` (alerts) | none |
| MTA subway static GTFS (stops) | `https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip` | none |
| Citi Bike GBFS | discovered from `https://gbfs.citibikenyc.com/gbfs/gbfs.json` | none |
| 311 (`erm2-nwe9`) and DOHMH inspections (`43nn-pn8j`) | `https://data.cityofnewyork.us` (Socrata) | optional `SOCRATA_APP_TOKEN` |
| Weather | `https://api.weather.gov` | none; descriptive User-Agent required |
| MTA Bus Time | `https://bustime.mta.info/api/siri/vehicle-monitoring.json` | `MTA_BUS_TIME_API_KEY`; deferred stub, raises even with a key |
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

Camera frames are processed in memory and discarded. Only counts and bounding-box statistics are stored. See `docs/privacy.md`.
