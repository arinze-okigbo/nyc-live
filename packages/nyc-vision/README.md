# nyc-vision

YOLO density over NYC DOT traffic-camera frames. Samples a stratified set of cameras on
an interval, runs `ultralytics` YOLO on each frame, and writes **counts and box
statistics only** into DuckDB. Raw frames never touch disk: they live in the
`CameraFrameSource` in-memory buffer (one per camera, evicted after
`FRAME_BUFFER_MAX_AGE`) and are dropped. See `docs/privacy.md`.

What it writes, through the frozen `nyc_live.store.Store` API:

| table | rows | written by |
|-------|------|-----------|
| `density_samples` | one row per camera per tick per `DetectionClass`, zero counts included | `Store.insert_density_samples` |
| `camera_frame_fetches` | one row per frame-fetch attempt, ok/failed | `Store.record_frame_fetches` |
| `cameras` | camera_id, source, name, lat, lon, is_online, first_seen, last_seen | upsert SQL in `pipeline.py` |

`density_now` / `density_history` are **not** reimplemented here. The served path is
`nyc_live.services.density`; `nyc_vision.service` re-exports it so there is exactly one
aggregation and the MCP tool, the dashboard and this package cannot disagree.

## Dependencies the orchestrator must add

`ultralytics` and `torch` are imported lazily inside `YoloDetector`, so this package
imports and its tests pass without them. Running a real detection needs them. Exact
lines (this is the only change nyc-vision needs to `pyproject.toml`; **cv-engineer does
not edit it**):

`packages/nyc-vision/pyproject.toml`:

```toml
dependencies = [
    "nyc-live",
    "pillow>=10.0",
    "numpy>=1.26",
    "ultralytics>=8.3",
    "torch>=2.4",
    "torchvision>=0.19",
]
```

root `pyproject.toml`:

```toml
[tool.uv.sources]
nyc-live = { workspace = true }
torch = { index = "pytorch-cpu", marker = "sys_platform == 'linux'" }
torchvision = { index = "pytorch-cpu", marker = "sys_platform == 'linux'" }

[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true
```

Then `just sync`. On macOS the default PyPI torch wheels are used (they include MPS);
the `pytorch-cpu` index only applies on Linux.

## Configuration

All environment, all optional. Core `NYC_LIVE_*` settings (`NYC_LIVE_DUCKDB_PATH`,
`NYC_LIVE_DATA_DIR`, HTTP timeouts) come from `nyc_live.config`.

| env var | default | meaning |
|---------|---------|---------|
| `NYC_VISION_CAMERAS` | `60` | cameras sampled per tick (the gate needs >= 50) |
| `NYC_VISION_INTERVAL_S` | `60` | seconds between ticks |
| `NYC_VISION_MODEL` | `yolo11n.pt` | ultralytics weights name or path |
| `NYC_VISION_DEVICE` | `cpu` | torch device: `cpu`, `mps`, `cuda:0` |
| `NYC_VISION_CONFIDENCE` | `0.25` | detection confidence threshold |
| `NYC_VISION_IMGSZ` | `640` | inference image size |
| `NYC_VISION_CONCURRENCY` | `4` | cameras fetched in parallel (detection is always serialised) |
| `NYC_VISION_GRID` | `5` | stratification grid resolution over the NYC bbox |
| `NYC_VISION_ARCHIVE` | `true` | daily Parquet archive of yesterday's rows |
| `NYC_VISION_TZ` | `America/New_York` | local timezone for the rush report and chart |

### Device selection

* **Linux / CI — `NYC_VISION_DEVICE=cpu` (default).** CPU wheels from the
  `pytorch-cpu` index. Measured here: first `yolo11n` inference ~19 s (weights load +
  warm-up), subsequent inferences ~100 ms per 640x480 frame. 60 cameras at 100 ms is
  ~6 s of detection per tick, comfortably inside a 60 s interval.
* **Apple Silicon — `NYC_VISION_DEVICE=mps`.** The default macOS torch wheels ship
  Metal support; nothing extra to install. `YoloDetector` calls `model.to("mps")` and
  passes `device="mps"` to `predict`, and raises `DetectionError` naming the device if
  the move fails, rather than silently falling back to CPU.
* **NVIDIA — `NYC_VISION_DEVICE=cuda:0`** is passed straight through, untested here.

The first run downloads the weights to the ultralytics cache. To avoid that, set
`NYC_VISION_MODEL=/path/to/yolo11n.pt`.

## Running

```bash
just sync                       # after the dependency lines above are in place
just vision once                # one tick; proves camera list + frames + model + DuckDB
just vision run                 # tick forever; Ctrl-C stops cleanly mid-interval
just vision report -- --since 24h
just vision chart -- --day 2026-09-08 --out docs/vision-rush.png
```

or directly:

```bash
uv run nyc-vision once
uv run nyc-vision run --cameras 60 --interval 60
uv run nyc-vision report --since 24h
uv run nyc-vision chart --day 2026-09-08 --out docs/vision-rush.png
uv run nyc-vision --db ./data/nyc_live.duckdb report --since 24h   # any database
```

`run` and `once` open DuckDB read-write; `report` and `chart` open it **read-only**.
DuckDB allows one writer process, so stop `run` before reporting on the same file (or
point `--db` at a copy).

## The Phase 3 gate

24 h continuous over >= 50 cameras, < 2 % frame-fetch failure, and a chart showing AM
and PM rush. On a machine that can reach `webcams.nyctmc.org`:

```bash
# 1. start the pipeline and leave it for a full local day (plus a little)
NYC_VISION_CAMERAS=60 NYC_VISION_INTERVAL_S=60 uv run nyc-vision run

# 2. after >= 24 h, Ctrl-C, then measure
uv run nyc-vision report --since 24h

# 3. render the chart for a complete local day
uv run nyc-vision chart --day <YYYY-MM-DD> --out docs/vision-rush.png
```

`report` prints, and exits 0 only if both gates pass:

```
window: ... -> ... (1 day, 0:00:00)
frame-fetch failure rate: 0.NNN% (F/A attempts over C cameras) [gate: <2% -> PASS|FAIL]
coverage: C cameras, N frames, M samples spanning H h (... -> ...) [gate: >=50 cameras and >=24 h -> PASS|FAIL]
rush profile: 24 hours with data; AM peak HH:00 (person P, vehicle V); PM peak HH:00 (...)
gate: PASS|FAIL
```

The failure rate is measured from `camera_frame_fetches` and nothing else. A detector
failure is counted separately (`TickResult.cameras_failed`) and is deliberately **not**
written into that table, so a model problem cannot be laundered into or out of the
frame-fetch metric.

`chart` refuses to write a PNG when the day has no rows: it prints the reason to stderr
and exits 1. There is no empty or placeholder chart.

## Design notes

**Camera selection** (`sampling.py`) is deterministic and has no RNG. Online, in-bbox
cameras are dropped into a `grid` x `grid` mesh over `contracts.NYC_BBOX` (default 5x5,
cells of roughly 10x12 km, which separates Staten Island / Brooklyn / Queens /
Manhattan / the Bronx without a borough polygon file). Cells are sorted by (row, col),
cameras inside a cell by id, and the selection round-robins one camera per cell per
pass. A dense area cannot crowd out a sparse one, and reordering the DOT list does not
change the selection.

**Cadence.** Frames are only ever obtained through `FrameSource.get_frame()`, which
owns the `CAMERA_FRAME_MIN_INTERVAL` (2 s) per-camera rate limit and the buffer. This
package never builds a DOT image URL.

**Zero counts are data.** Every tick writes all six `DetectionClass` rows per camera, so
"no people at 03:00" is recorded. `confidence_mean` and `bbox_area_frac_mean` stay
`NULL` on a zero row rather than being reported as 0.

**Archive.** `run` archives yesterday's `density_samples` and `camera_frame_fetches` to
`$NYC_LIVE_DATA_DIR/archive/<table>/<day>.parquet` via `Store.archive_day`, once per
day, skipping days with no rows.

**Timestamps.** DuckDB needs `pytz` to return `TIMESTAMPTZ` cells to Python and it is
not a dependency, so every query here selects `epoch_ms(ts)` and converts in Python.
Local-hour bucketing for the rush profile is done with `zoneinfo`, which also keeps the
SQL free of the ICU extension.

## Tests

`tests/vision/` runs entirely offline and without torch: a `FakeFrameSource` and a
`FakeDetector` (model name `fake:*`, `is_synthetic = True`) exercise the tick, the store
writes and the reports. The real-model test is marked `slow` and skips itself with a
reason when `ultralytics` is not importable. The class-mapping path is tested against a
stand-in ultralytics result object, so no weights are needed to prove the COCO mapping.
