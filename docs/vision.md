# nyc-vision: the density pipeline

`nyc-vision` (`packages/nyc-vision/`) samples a stratified set of NYC DOT traffic cameras on
an interval, runs an `ultralytics` YOLO model over each frame, and writes counts and box
statistics into DuckDB. Frames are never written to disk. This page covers the pipeline, its
environment variables, device selection, the privacy posture, the Phase 3 gate commands, and
how to read the report. `packages/nyc-vision/README.md` is the package's own reference and
this page does not contradict it.

## What a tick does

One tick is defined in `packages/nyc-vision/src/nyc_vision/pipeline.py::DensityPipeline.tick`:

1. Refresh the DOT camera list through `CameraListAdapter` (which honours its own 10 minute
   TTL, so this is usually a no-op) and stratify a selection with `sampling.select_cameras`.
   If the list fetch fails, the previous selection is kept and the error is reported on the
   tick line; with no previous selection the tick does nothing.
2. Upsert the selected cameras into the `cameras` table so `services/density.py` can attach a
   name and a coordinate to every aggregate. The SQL lives in `pipeline.py`
   (`UPSERT_CAMERA_SQL`); `first_seen` is never updated after the first write.
3. For each camera, up to `NYC_VISION_CONCURRENCY` in parallel, call
   `FrameSource.get_frame()` and then run the detector in a worker thread. Detection is
   serialised behind a lock because one model instance is shared. The pipeline never builds a
   DOT image URL itself, so the 2 second per-camera cadence
   (`contracts.CAMERA_FRAME_MIN_INTERVAL`) and the in-memory buffer stay owned by
   `CameraFrameSource` in `src/nyc_live/feeds/cameras.py`.
4. Write one `DensitySample` per camera per `DetectionClass`, including zero counts, so "no
   people at 03:00" is a recorded observation rather than a hole. On a zero row
   `confidence_mean` and `bbox_area_frac_mean` stay `NULL` instead of being reported as 0
   (`pipeline.build_samples`).
5. Drain the frame source's `CameraFrameFetch` telemetry into `camera_frame_fetches`.
6. Evict expired frames from the in-memory buffer.

A camera that fails at any step is logged and counted; the tick continues and never raises for
a per-camera problem. `run_forever` waits `interval_s` measured from the start of the tick, so
an overrunning tick does not compound; if a tick takes longer than the interval it logs a
warning and the next one starts immediately.

### Camera selection

`sampling.py` is deterministic and uses no RNG. Online, in-bbox cameras are dropped into a
`grid` x `grid` mesh over `contracts.NYC_BBOX` (default 5x5). Cells are sorted by (row, col),
cameras inside a cell by id, and the selection round-robins one camera per cell per pass. A
dense area cannot crowd out a sparse one until the sparse cells are exhausted, and reordering
the DOT list upstream does not change the selection. When fewer cameras are eligible than were
requested, the shortage is logged through `SelectionStats.describe()` and the selection is
never padded.

### What is written

Through the frozen `nyc_live.store.Store` API:

| Table | Rows | Written by |
|-------|------|-----------|
| `density_samples` | one row per camera per tick per `DetectionClass`, zero counts included | `Store.insert_density_samples` |
| `camera_frame_fetches` | one row per frame-fetch attempt, ok or failed | `Store.record_frame_fetches` |
| `cameras` | camera_id, source, name, lat, lon, is_online, first_seen, last_seen | `UPSERT_CAMERA_SQL` in `pipeline.py` |

`run` archives yesterday's `density_samples` and `camera_frame_fetches` to
`$NYC_LIVE_DATA_DIR/archive/<table>/<day>.parquet` once per day via `Store.archive_day`,
skipping days with no rows. Set `NYC_VISION_ARCHIVE=false` to turn that off.

`density_now` and `density_history` are not reimplemented in this package.
`nyc_vision/service.py` re-exports `nyc_live.services.density`, so the MCP tools, the
dashboard and this package share exactly one aggregation. See `docs/mcp.md` for the served
shape.

## Configuration

Every knob is environment-only and optional (`packages/nyc-vision/src/nyc_vision/config.py`).
The core `NYC_LIVE_*` settings (`NYC_LIVE_DUCKDB_PATH`, `NYC_LIVE_DATA_DIR`, HTTP timeouts,
upstream base URLs) come from `nyc_live.config` and are unchanged.

| Variable | Default | Meaning |
|----------|---------|---------|
| `NYC_VISION_CAMERAS` | `60` | cameras sampled per tick (the gate needs at least 50) |
| `NYC_VISION_INTERVAL_S` | `60` | seconds between ticks |
| `NYC_VISION_MODEL` | `yolo11n.pt` | ultralytics weights name or path |
| `NYC_VISION_DEVICE` | `cpu` | torch device: `cpu`, `mps`, `cuda:0` |
| `NYC_VISION_CONFIDENCE` | `0.25` | detection confidence threshold |
| `NYC_VISION_IMGSZ` | `640` | inference image size |
| `NYC_VISION_CONCURRENCY` | `4` | cameras fetched in parallel; detection is always serialised |
| `NYC_VISION_GRID` | `5` | stratification grid resolution over the NYC bbox |
| `NYC_VISION_ARCHIVE` | `true` | daily Parquet archive of yesterday's rows |
| `NYC_VISION_TZ` | `America/New_York` | local timezone for the rush report and chart; storage stays UTC |

`--cameras`, `--interval`, `--db`, `--tz` and `--day` on the CLI override the matching
variable for that run.

## Device selection

Lifted from `packages/nyc-vision/README.md`:

* **Linux and CI, `NYC_VISION_DEVICE=cpu` (default).** CPU wheels from the `pytorch-cpu`
  index. The package README reports, measured on the build machine: first `yolo11n` inference
  about 19 s (weights load plus warm-up), subsequent inferences about 100 ms per 640x480
  frame. 60 cameras at 100 ms is about 6 s of detection per tick, comfortably inside a 60 s
  interval.
* **Apple Silicon, `NYC_VISION_DEVICE=mps`.** The default macOS torch wheels ship Metal
  support; nothing extra to install. `YoloDetector` calls `model.to("mps")` and passes
  `device="mps"` to `predict`, and raises `DetectionError` naming the device if the move
  fails, rather than silently falling back to CPU.
* **NVIDIA, `NYC_VISION_DEVICE=cuda:0`** is passed straight through, untested here.

The first run downloads the weights into the ultralytics cache. To avoid that, set
`NYC_VISION_MODEL=/path/to/yolo11n.pt`.

Only the six `contracts.DetectionClass` members are kept. Every other COCO class ultralytics
reports (traffic light, dog, handbag, and so on) is dropped and counted nowhere
(`COCO_CLASS_MAP` in `detector.py`; COCO ids 4 and 6, aeroplane and train, are deliberately
absent).

## Dependencies you must add before running

`ultralytics` and `torch` are imported lazily inside `YoloDetector._load()`, so the package
imports and its tests pass without them and `just check` stays green on a machine with no
torch wheels. Running a real detection needs them, and `pyproject.toml` is the orchestrator's
file, not this package's. The exact lines, copied from `packages/nyc-vision/README.md`:

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

Then `just sync`. On macOS the default PyPI torch wheels are used (they include MPS); the
`pytorch-cpu` index only applies on Linux. Without these lines `nyc-vision run` fails on the
first detection with `DetectionError: ultralytics is not installed`.

## Running

```bash
just sync                                   # after the dependency lines above are in place
just vision once                            # one tick: camera list, frames, model, DuckDB
just vision run                             # tick until Ctrl-C
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

`run` and `once` open DuckDB read-write; `report` and `chart` open it read-only. DuckDB allows
one writer process, so stop `run` before reporting on the same file, or point `--db` at a
copy. `run` installs SIGINT and SIGTERM handlers and stops cleanly mid-interval.

Each tick prints one line (`TickResult.describe`):

```
tick 2026-09-08T14:03:00Z cameras=59/60 ok samples=354 telemetry=60 failed=1 in 7.2s
```

`once` additionally prints up to ten per-camera failures and exits 1 if no camera succeeded.

## The Phase 3 gate

The gate is 24 h continuous over at least 50 cameras, under 2 % frame-fetch failure, and a
chart showing AM and PM rush. On a machine that can reach `webcams.nyctmc.org`:

```bash
# 1. start the pipeline and leave it for a full local day (plus a little)
NYC_VISION_CAMERAS=60 NYC_VISION_INTERVAL_S=60 uv run nyc-vision run

# 2. after at least 24 h, Ctrl-C, then measure
uv run nyc-vision report --since 24h

# 3. render the chart for a complete local day
uv run nyc-vision chart --day <YYYY-MM-DD> --out docs/vision-rush.png
```

### Reading the report

`report` prints four lines and a verdict, and exits 0 only when both gates pass (1 when either
fails, 2 when the DuckDB file does not exist or cannot be opened read-only):

```
window: ... -> ... (1 day, 0:00:00)
frame-fetch failure rate: 0.NNN% (F/A attempts over C cameras) [gate: <2% -> PASS|FAIL]
coverage: C cameras, N frames, M samples spanning H h (... -> ...) [gate: >=50 cameras and >=24 h -> PASS|FAIL]
rush profile: 24 hours with data; AM peak HH:00 (person P, vehicle V); PM peak HH:00 (...) [<day>, <tz>]
gate: PASS|FAIL
```

* **window** is the UTC interval `--since` resolved to.
* **frame-fetch failure rate** is failures over attempts in `camera_frame_fetches` and nothing
  else (`report.frame_failure_rate`). Up to five most common error strings are printed under
  it. A window with zero attempts prints a "no rows" line and fails the gate rather than
  reporting 0 %. A detector failure is counted separately as `TickResult.cameras_failed` and is
  deliberately never written into `camera_frame_fetches`, so a model problem cannot be
  laundered into or out of this metric.
* **coverage** counts distinct cameras, frames (one frame is one `(camera_id, ts)` pair) and
  rows in `density_samples`, and the span between the first and last sample
  (`report.cameras_covered`). It passes at 50 or more cameras and 24 h or more of span.
* **rush profile** names the AM (05:00 to 11:59) and PM (14:00 to 20:59) peak hours by
  person mean plus vehicle mean for one local day (`report.hourly_rush`, `report.rush_summary`).
  Only hours that actually have frames are counted, so a gap stays a visible gap. It is
  reported for information; the PASS/FAIL verdict is the failure rate and coverage.

### Reading the chart

`chart` draws the per-hour person and vehicle means as two polylines over a 24 h local-time
axis with the AM (07-10) and PM (16-19) windows shaded, using Pillow (matplotlib is not a
dependency). Hours with no frames are left as gaps, never interpolated or zeroed. It refuses
to write a PNG when the day has no rows: it prints the reason to stderr and exits 1. There is
no empty or placeholder chart. On success it prints the output path, how many of the 24 hours
had data, and the rush summary.

### Gate status

**The gate has not been run.** `webcams.nyctmc.org` was blocked from the build sandbox, so no
frame was ever fetched, no `density_samples` row exists, and `docs/vision-rush.png` does not
exist. Only the ultralytics weights host and the MTA static GTFS zip on S3 were reachable from
that sandbox. Nothing in this repository reports a density number measured from a real camera.
Running the three commands above on a machine with access to the DOT camera host is the
remaining work for Phase 3.

## Privacy

The pipeline persists counts and box statistics only. Per camera per sample:
timestamp, detection class, count, mean confidence, mean box area as a fraction of the frame,
model name, inference time and frame dimensions. That is the whole `DensitySample` row in
`contracts.py`. No box coordinates, no crops, no embeddings, no tracks across frames. Per
frame-fetch attempt: camera id, timestamp, success flag, HTTP status, latency, byte size and
an error string (`CameraFrameFetch`), which is what the failure gate is measured from and
holds no image data.

Frames exist only as bytes in the `CameraFrameSource` in-memory buffer, one per camera
(`FRAME_BUFFER_MAX_FRAMES_PER_CAMERA`), evicted after `FRAME_BUFFER_MAX_AGE` (10 minutes).
`detector.detect()` takes those bytes, decodes them with Pillow in memory and returns counts;
it writes nothing. No frame is written to disk, to Parquet or to DuckDB at any point. Full
policy in `docs/privacy.md`.

## Tests

`tests/vision/` runs entirely offline and without torch: a `FakeFrameSource` and a
`FakeDetector` exercise the tick, the store writes and the reports. `FakeDetector.model_name`
is deliberately `fake:<name>` and `is_synthetic` is `True`, so any row it produced is obvious
on inspection; it must never be wired into `nyc-vision run` against a real database. The
real-model test is marked `slow` and skips itself with a reason when `ultralytics` is not
importable. The COCO class mapping is tested against a stand-in ultralytics result object, so
no weights are needed to prove it.
