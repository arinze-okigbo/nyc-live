"""One tick: pick cameras, pull frames, detect, write counts. Plus the loop runner.

A tick is:

1. Refresh the DOT camera list through the adapter (it honours its own 10 min TTL,
   so this is nearly always a no-op) and stratify a selection with `sampling`.
2. Upsert the selected cameras into the `cameras` table so `services/density.py`
   can put a name and a lat/lon on every aggregate. `nyc_live.store.Store` has no
   helper for that table, so the SQL lives here.
3. For each camera, in parallel up to `concurrency`: `FrameSource.get_frame()`
   (never the DOT image URL directly; the frame source owns the 2 s per-camera
   cadence and the in-memory buffer) then run the detector in a worker thread.
   Detection is serialised by a lock because one model instance is shared.
4. Write one `DensitySample` per camera per contract class, INCLUDING zero counts,
   so "no people at 03:00" is a recorded observation and not a hole in the data.
5. Drain the frame source's `CameraFrameFetch` telemetry into `camera_frame_fetches`.
   That table, and only that table, is what the <2 % failure gate is measured from,
   so detector failures are counted separately and never written into it.
6. Evict expired frames from the in-memory buffer.

A camera that fails at any step is logged and counted; the tick continues. A tick
never raises for a per-camera problem. Frames are never written to disk.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from nyc_live.contracts import (
    Camera,
    CameraFrameFetch,
    DensitySample,
    DetectionClass,
    FeedAdapter,
    FeedUnavailable,
    FrameSource,
    now_utc,
)
from nyc_live.store import Store
from nyc_vision.detector import DetectionResult, Detector
from nyc_vision.sampling import DEFAULT_GRID, SelectionStats, select_cameras

log = logging.getLogger(__name__)

ARCHIVE_TABLES: tuple[str, ...] = ("density_samples", "camera_frame_fetches")
"""Archived to Parquet daily. Both are in `contracts.PARQUET_ARCHIVE_TABLES`."""

UPSERT_CAMERA_SQL = """
INSERT INTO cameras (camera_id, source, name, lat, lon, is_online, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (camera_id) DO UPDATE SET
    source    = excluded.source,
    name      = excluded.name,
    lat       = excluded.lat,
    lon       = excluded.lon,
    is_online = excluded.is_online,
    last_seen = excluded.last_seen
"""
"""`first_seen` is intentionally not updated: it is when nyc-vision first saw the camera."""


@dataclass(slots=True)
class TickResult:
    started_at: datetime
    finished_at: datetime
    cameras_selected: int = 0
    cameras_ok: int = 0
    cameras_failed: int = 0
    samples_written: int = 0
    telemetry_written: int = 0
    frames_evicted: int = 0
    camera_list_error: str | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def describe(self) -> str:
        head = (
            f"tick {self.started_at:%Y-%m-%dT%H:%M:%SZ} "
            f"cameras={self.cameras_ok}/{self.cameras_selected} ok "
            f"samples={self.samples_written} telemetry={self.telemetry_written} "
            f"failed={self.cameras_failed} in {self.duration_s:.1f}s"
        )
        if self.camera_list_error:
            head += f" [camera list: {self.camera_list_error}]"
        return head


def build_samples(camera_id: str, ts: datetime, result: DetectionResult) -> list[DensitySample]:
    """One row per contract class, zero counts included. Counts and box stats only."""
    rows: list[DensitySample] = []
    for cls in DetectionClass:
        stats = result.stats(cls)
        rows.append(
            DensitySample(
                camera_id=camera_id,
                ts=ts,
                cls=cls,
                count=stats.count,
                confidence_mean=stats.confidence_mean if stats.count else None,
                bbox_area_frac_mean=stats.bbox_area_frac_mean if stats.count else None,
                model=result.model,
                inference_ms=result.inference_ms,
                frame_w=result.width,
                frame_h=result.height,
            )
        )
    return rows


class DensityPipeline:
    """Owns the tick. Construct once per process; `Store` must be writable."""

    def __init__(
        self,
        *,
        store: Store,
        frames: FrameSource,
        detector: Detector,
        camera_feed: FeedAdapter[Camera],
        camera_count: int = 60,
        concurrency: int = 4,
        grid: int = DEFAULT_GRID,
        archive_dir: Path | None = None,
        archive: bool = True,
    ) -> None:
        if store.read_only:
            raise ValueError("nyc-vision needs a writable Store; got read_only=True")
        self.store = store
        self.frames = frames
        self.detector = detector
        self.camera_feed = camera_feed
        self.camera_count = camera_count
        self.concurrency = max(1, concurrency)
        self.grid = grid
        self.archive_dir = archive_dir
        self.archive = archive
        self.ticks_completed = 0
        self.last_tick: TickResult | None = None
        self._selection: list[Camera] = []
        self._selection_stats: SelectionStats | None = None
        self._first_seen: dict[str, datetime] = {}
        self._archived_days: set[date] = set()
        self._detect_lock = asyncio.Lock()

    # -- camera selection --------------------------------------------------

    @property
    def selection(self) -> list[Camera]:
        return list(self._selection)

    async def refresh_selection(self) -> str | None:
        """Refresh the camera list and re-stratify. Returns an error string on failure.

        On failure the previous selection is kept (the DOT list changes slowly and a
        transient list outage must not stop detection); with no previous selection the
        caller gets the error and the tick does nothing.
        """
        try:
            snapshot = await self.camera_feed.fetch()
        except FeedUnavailable as exc:
            return f"{exc.kind.value}: {exc.message}"
        except Exception as exc:
            log.exception("camera list refresh crashed")
            return f"{type(exc).__name__}: {exc}"
        chosen, stats = select_cameras(snapshot.records, self.camera_count, grid=self.grid)
        previous = {c.id for c in self._selection}
        self._selection = chosen
        self._selection_stats = stats
        if {c.id for c in chosen} != previous:
            log.info("camera selection changed: %s", stats.describe())
        return None

    # -- writes ------------------------------------------------------------

    def upsert_cameras(self, cameras: Sequence[Camera], *, seen_at: datetime) -> int:
        """Keep the `cameras` table current so density aggregates can be located."""
        rows = []
        for cam in cameras:
            first = self._first_seen.setdefault(cam.id, seen_at)
            rows.append(
                (
                    cam.id,
                    cam.source.value,
                    cam.name,
                    cam.lat,
                    cam.lon,
                    cam.is_online,
                    first,
                    seen_at,
                )
            )
        return self.store.executemany(UPSERT_CAMERA_SQL, rows)

    def drain_telemetry(self) -> int:
        """Move the frame source's CameraFrameFetch rows into DuckDB."""
        drain = getattr(self.frames, "drain_telemetry", None)
        if not callable(drain):
            return 0
        rows: list[CameraFrameFetch] = list(drain())
        if not rows:
            return 0
        return self.store.record_frame_fetches(rows)

    def _evict(self) -> int:
        evict = getattr(self.frames, "evict_expired", None)
        if not callable(evict):
            return 0
        try:
            return int(evict())
        except Exception:
            log.exception("frame buffer eviction failed")
            return 0

    # -- the tick ----------------------------------------------------------

    async def tick(self) -> TickResult:
        started = now_utc()
        result = TickResult(started_at=started, finished_at=started)
        result.camera_list_error = await self.refresh_selection()
        cameras = self._selection
        result.cameras_selected = len(cameras)
        if not cameras:
            result.telemetry_written = self.drain_telemetry()
            result.finished_at = now_utc()
            self.last_tick = result
            return result

        self.upsert_cameras(cameras, seen_at=started)

        semaphore = asyncio.Semaphore(self.concurrency)

        async def run_one(cam: Camera) -> tuple[str, list[DensitySample] | None, str | None]:
            async with semaphore:
                return await self._process_camera(cam)

        outcomes = await asyncio.gather(*(run_one(cam) for cam in cameras), return_exceptions=True)

        samples: list[DensitySample] = []
        for cam, outcome in zip(cameras, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                result.cameras_failed += 1
                result.errors.append((cam.id, f"{type(outcome).__name__}: {outcome}"))
                log.warning("camera %s crashed the worker: %s", cam.id, outcome)
                continue
            _, rows, error = outcome
            if error is not None or rows is None:
                result.cameras_failed += 1
                result.errors.append((cam.id, error or "no samples"))
                continue
            result.cameras_ok += 1
            samples.extend(rows)

        if samples:
            try:
                result.samples_written = self.store.insert_density_samples(samples)
            except Exception:
                log.exception("insert_density_samples failed for %d rows", len(samples))
        result.telemetry_written = self.drain_telemetry()
        result.frames_evicted = self._evict()
        result.finished_at = now_utc()
        self.ticks_completed += 1
        self.last_tick = result
        return result

    async def _process_camera(
        self, cam: Camera
    ) -> tuple[str, list[DensitySample] | None, str | None]:
        try:
            frame = await self.frames.get_frame(cam.id)
        except FeedUnavailable as exc:
            log.warning("frame fetch failed for %s: %s", cam.id, exc.message)
            return cam.id, None, f"frame: {exc.kind.value}: {exc.message}"
        except Exception as exc:
            log.warning("frame fetch crashed for %s: %s", cam.id, exc)
            return cam.id, None, f"frame: {type(exc).__name__}: {exc}"
        try:
            async with self._detect_lock:
                detection = await asyncio.to_thread(self.detector.detect, frame.data)
        except Exception as exc:
            log.warning("detection failed for %s: %s", cam.id, exc)
            return cam.id, None, f"detect: {type(exc).__name__}: {exc}"
        return cam.id, build_samples(cam.id, frame.fetched_at, detection), None

    # -- daily parquet archive --------------------------------------------

    def maybe_archive(self, *, now: datetime | None = None) -> list[Path]:
        """Archive yesterday's rows to Parquet, once per day per process."""
        if not self.archive or self.archive_dir is None:
            return []
        day = (now or now_utc()).date() - timedelta(days=1)
        if day in self._archived_days:
            return []
        self._archived_days.add(day)
        written: list[Path] = []
        for table in ARCHIVE_TABLES:
            try:
                rows = self.store.execute(
                    f"SELECT count(*) FROM {table} WHERE CAST(ts AS DATE) = ?", [day]
                )
                if not rows or not rows[0][0]:
                    log.info("archive: no %s rows for %s, skipping", table, day)
                    continue
                out = self.store.archive_day(table, day, self.archive_dir)
            except Exception:
                log.exception("archiving %s for %s failed", table, day)
                continue
            log.info("archived %s rows of %s for %s to %s", rows[0][0], table, day, out)
            written.append(out)
        return written


async def run_forever(
    pipeline: DensityPipeline,
    *,
    interval_s: float = 60.0,
    stop: asyncio.Event | None = None,
    max_ticks: int | None = None,
    on_tick: Callable[[TickResult], None] | None = None,
) -> int:
    """Tick every `interval_s`. Returns the number of completed ticks.

    An overrunning tick does not compound: the next one starts as soon as the
    previous finished. `stop` is set by the SIGINT/SIGTERM handler in `__main__`.
    """
    stop = stop or asyncio.Event()
    ticks = 0
    while not stop.is_set():
        began = time.monotonic()
        pipeline.maybe_archive()
        result = await pipeline.tick()
        ticks += 1
        log.info("%s", result.describe())
        if on_tick is not None:
            on_tick(result)
        if max_ticks is not None and ticks >= max_ticks:
            break
        delay = interval_s - (time.monotonic() - began)
        if delay <= 0:
            log.warning(
                "tick took %.1fs, longer than the %.1fs interval; running back to back",
                time.monotonic() - began,
                interval_s,
            )
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            continue
        else:
            break
    return ticks
