# nyc-live

Live NYC civic data, three ways:

1. **nyc-mcp** — an MCP server exposing subway, cameras, Citi Bike, 311, restaurant inspections, and weather as typed tools for Claude Code and Claude Desktop.
2. **nyc-vision** — a pedestrian and vehicle density pipeline running YOLO over NYC DOT traffic camera frames, writing counts to DuckDB.
3. **nyc-dash** — a single-page live map: subway positions, camera density heatmap, 311 stream, weather, Citi Bike levels.

Layers 2 and 3 consume layer 1 through the shared service layer in `src/nyc_live`.

Status: **Phase 0 (scaffold) complete.** Feed adapters, the MCP server, the vision pipeline, and the dashboard land in Phases 1 to 4. See `CLAUDE.md` for the phase plan and `docs/architecture.md` for the design.

## Quick start

```bash
uv sync --all-packages --group dev   # or: just sync
cp .env.example .env                 # optional keys; nothing is required
just check                           # lint + types + tests
just smoke                           # one line per live feed (Phase 1+)
```

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and [just](https://just.systems/).

## Layout

```
src/nyc_live/          core library: contracts (frozen), feeds/, cache, store, services/
packages/nyc-mcp/      MCP server (FastMCP)
packages/nyc-vision/   detection pipeline (ultralytics YOLO, CPU-first)
packages/nyc-dash/     FastAPI + MapLibre GL + Deck.gl dashboard
tests/                 offline tests run by default; `live`-marked tests need NYC_LIVE_TESTS=1
docs/                  architecture, privacy, MCP install instructions
.claude/agents/        subagent definitions used to build this repo
```

## Data sources

| Feed | Upstream | Auth |
|------|----------|------|
| DOT traffic cameras | `webcams.nyctmc.org/api/cameras/` | none |
| MTA subway GTFS-realtime | `api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct/` | none |
| Citi Bike GBFS | discovered from `gbfs.json` | none |
| 311 and DOHMH inspections | `data.cityofnewyork.us` (Socrata) | optional app token |
| Weather | `api.weather.gov` | none, descriptive User-Agent required |
| MTA Bus Time, 511NY cameras | deferred | key required, skipped when absent |

No data is ever fabricated. When a feed is down, every tool and endpoint says so.

## Privacy

Camera frames are processed in memory and discarded. Only counts and bounding-box statistics are stored. See `docs/privacy.md`.
