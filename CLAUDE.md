# nyc-live: rules for every agent working in this repo

## Locked stack (do not relitigate)

Python 3.12 · `uv` for deps · `ruff` + `pyright` for checks · `pytest` · `FastMCP` for the MCP server · DuckDB for the time-series store, Parquet on disk for archives · `httpx` async for all fetching · `ultralytics` YOLO for detection, CPU-first with a documented path to Apple Silicon MPS · FastAPI + static MapLibre GL / Deck.gl frontend · `just` as the task runner.

`just check` (ruff format, ruff check, pyright, pytest) must pass before any commit. Run `just fmt` first.

## The contract freeze

`src/nyc_live/contracts.py` is frozen. It holds the `FeedAdapter` and `FrameSource` protocols, every Pydantic record type, `Snapshot` and `Envelope`, `FeedUnavailable`, the DuckDB DDL, TTLs, and the privacy constants.

* No agent other than the orchestrator edits `contracts.py`, ever.
* If you need a change, STOP and report the exact change to the orchestrator. Do not work around it with `extra` fields, dicts, or side channels.
* Every parallel agent codes against `contracts.py` as-is.

## Ownership map (never touch a file you do not own)

| Owner | Owns |
|-------|------|
| orchestrator only | `contracts.py`, `pyproject.toml`, `justfile`, `CLAUDE.md`, `.claude/**`, `src/nyc_live/{__init__,config,geo,http,cache,store,smoke}.py`, `src/nyc_live/feeds/__init__.py`, `tests/conftest.py`, `tests/test_contracts.py` |
| feed-transit | `src/nyc_live/feeds/transit.py`, `src/nyc_live/feeds/bus.py`, `tests/feeds/test_transit*.py`, `tests/feeds/test_bus*.py`, `tests/fixtures/transit/` |
| feed-cameras | `src/nyc_live/feeds/cameras.py`, `src/nyc_live/feeds/ny511.py`, `tests/feeds/test_cameras*.py`, `tests/feeds/test_ny511*.py`, `tests/fixtures/cameras/` |
| feed-micromobility | `src/nyc_live/feeds/micromobility.py`, `tests/feeds/test_micromobility*.py`, `tests/fixtures/micromobility/` |
| feed-civic | `src/nyc_live/feeds/civic.py`, `src/nyc_live/feeds/socrata.py`, `src/nyc_live/feeds/weather.py`, `tests/feeds/test_civic*.py`, `tests/fixtures/civic/` |
| mcp-architect | `src/nyc_live/services/**`, `packages/nyc-mcp/**`, `tests/services/**`, `tests/mcp/**` |
| cv-engineer | `packages/nyc-vision/**`, `tests/vision/**` |
| dash-engineer | `packages/nyc-dash/**`, `tests/dash/**` |
| integration-tester | `tests/integration/**` |
| docs-writer | `README.md`, `docs/**` |

Need a new dependency? Report it; the orchestrator adds it to `pyproject.toml`.

## Adapter rules (Phase 1)

* One adapter class per `FeedName`, registered by name in `src/nyc_live/feeds/__init__.py::ADAPTER_SPECS`. Create the module and class named there; do not edit the registry.
* Constructor: `Adapter(client: httpx.AsyncClient, settings: Settings)`. Use `nyc_live.http.get_with_retry` for every GET and `nyc_live.http.RateLimiter` for per-key cadence.
* `fetch()` returns a full `Snapshot[T]` or raises `FeedUnavailable`. Never return an empty snapshot because the upstream failed. A 404 on a feed URL is fatal and loud (`ErrorKind.NOT_FOUND`).
* Map upstream fields explicitly into the frozen record types (`extra="forbid"` will reject anything else). Drop records with out-of-NYC coordinates via `geo.in_nyc_bbox` and log how many you dropped.
* Key-gated feeds (`MTA_BUS`, `NY511_CAMERAS`): `is_configured()` returns False when the env var is empty; `fetch()` raises `FeedNotConfigured`. The registry skips them cleanly.
* Set `stale_after = fetched_at + DEFAULT_TTL[name]` unless the upstream publishes its own TTL (GBFS `ttl`), in which case use the larger of the two.
* Tests: one `@pytest.mark.live` test that hits the real upstream and asserts on real data shape, and one offline test that replays a recorded fixture under `tests/fixtures/<area>/` using `respx`. Record the fixture from a real response, trimmed, never invented.

## Hard constraints (all phases)

* Never fabricate data. Down feed = `Envelope(status="error")` with the error attached, surfaced in tools and UI.
* Cache and rate-limit every upstream. Never fetch the same DOT camera faster than every 2 s (`CAMERA_FRAME_MIN_INTERVAL`).
* No secrets in the repo. Keys come from env; `.env.example` is the only checked-in reference.
* No raw camera frames on disk. In-memory rolling buffer only (`FRAME_BUFFER_*` constants), counts and box statistics persisted. See `docs/privacy.md`.
* Commit at every gate with a message naming the gate.

## Phases

0. Scaffold, contracts, justfile, agent definitions. Gate: `just check` passes.
1. Four `feed-*` agents in parallel. Gate: every adapter returns real data live; `just smoke` prints one line per feed.
2. `mcp-architect`: service layer + MCP server. Gate: tools callable from Claude Code; `claude mcp add` instructions verified.
3. `cv-engineer`: detection pipeline, `density_now` / `density_history`. Gate: 24 h over 50+ cameras, <2 % frame-fetch failure, visible AM/PM rush.
4. `dash-engineer`: dashboard on the service layer. Gate: <2 s load, live updates, graceful per-feed degradation.

## Torch / ultralytics (Phase 3)

Install CPU wheels on Linux via the `pytorch-cpu` index (`[[tool.uv.index]] name = "pytorch-cpu"`, `url = "https://download.pytorch.org/whl/cpu"`, with `torch = { index = "pytorch-cpu", marker = "sys_platform == 'linux'" }` in `[tool.uv.sources]`). On macOS, default wheels include MPS; select the device with `NYC_VISION_DEVICE=mps`.
