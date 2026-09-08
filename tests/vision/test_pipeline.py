from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nyc_live.contracts import (
    DetectionClass,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    now_utc,
)
from nyc_live.services.density import density_now
from nyc_live.store import Store
from nyc_vision.detector import ClassStats, DetectionResult, FakeDetector
from nyc_vision.pipeline import DensityPipeline, build_samples, run_forever
from tests.vision.conftest import (
    FakeCameraFeed,
    FakeFrameSource,
    borough_cameras,
    synthetic_jpeg,
)


def make_pipeline(
    store: Store,
    *,
    cameras: int = 10,
    fail_ids: tuple[str, ...] = (),
    detector: FakeDetector | None = None,
    archive_dir: Path | None = None,
    camera_error: FeedUnavailable | None = None,
) -> tuple[DensityPipeline, FakeFrameSource, FakeCameraFeed]:
    frames = FakeFrameSource(fail_ids=fail_ids)
    feed = FakeCameraFeed(borough_cameras(per_borough=4), error=camera_error)
    pipeline = DensityPipeline(
        store=store,
        frames=frames,
        detector=detector or FakeDetector(),
        camera_feed=feed,
        camera_count=cameras,
        concurrency=3,
        archive_dir=archive_dir,
        archive=archive_dir is not None,
    )
    return pipeline, frames, feed


# -- build_samples ---------------------------------------------------------


def test_build_samples_writes_a_row_for_every_class_including_zeros() -> None:
    result = DetectionResult(
        model="yolo11n.pt",
        inference_ms=42.0,
        width=640,
        height=480,
        classes={DetectionClass.PERSON: ClassStats(3, 0.8, 0.01)},
    )
    ts = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    rows = build_samples("cam-1", ts, result)

    assert len(rows) == len(DetectionClass)
    assert {r.cls for r in rows} == set(DetectionClass)
    person = next(r for r in rows if r.cls is DetectionClass.PERSON)
    assert person.count == 3
    assert person.confidence_mean == 0.8
    assert person.bbox_area_frac_mean == 0.01
    assert person.frame_w == 640 and person.frame_h == 480
    assert person.model == "yolo11n.pt"
    zeros = [r for r in rows if r.cls is not DetectionClass.PERSON]
    assert all(r.count == 0 for r in zeros)
    # absence is recorded as a zero count, never as a confidence of 0
    assert all(r.confidence_mean is None and r.bbox_area_frac_mean is None for r in zeros)


# -- a tick ----------------------------------------------------------------


async def test_tick_writes_samples_telemetry_and_cameras(store: Store) -> None:
    pipeline, frames, _ = make_pipeline(store, cameras=10)
    result = await pipeline.tick()

    assert result.camera_list_error is None
    assert result.cameras_selected == 10
    assert result.cameras_ok == 10
    assert result.cameras_failed == 0
    assert result.samples_written == 10 * len(DetectionClass)

    rows = store.execute("SELECT count(*), count(DISTINCT camera_id) FROM density_samples")
    assert rows[0] == (10 * len(DetectionClass), 10)

    persons = store.execute("SELECT DISTINCT count FROM density_samples WHERE class = 'person'")
    assert persons == [(2,)]  # FakeDetector's fixed count

    telemetry = store.execute(
        "SELECT count(*), count(*) FILTER (WHERE ok) FROM camera_frame_fetches"
    )
    assert telemetry[0] == (10, 10)
    assert result.telemetry_written == 10
    assert frames.telemetry == []  # drained

    cams = store.execute(
        "SELECT count(*), count(*) FILTER (WHERE lat IS NOT NULL AND name IS NOT NULL) FROM cameras"
    )
    assert cams[0] == (10, 10)
    sources = store.execute("SELECT DISTINCT source FROM cameras")
    assert sources == [("nyc_dot",)]


async def test_tick_never_writes_frame_bytes(store: Store, tmp_path: Path) -> None:
    pipeline, _, _ = make_pipeline(store, cameras=5)
    await pipeline.tick()
    columns = {
        r[0]
        for r in store.execute(
            "SELECT column_name FROM duckdb_columns() WHERE table_name = 'density_samples'"
        )
    }
    assert "data" not in columns and "image" not in columns
    assert list(tmp_path.rglob("*.jpg")) == []


async def test_one_failing_camera_does_not_abort_the_tick(store: Store) -> None:
    # pick a real id out of the selection the pipeline will make, then fail exactly that one
    probe, _, _ = make_pipeline(store, cameras=10)
    await probe.refresh_selection()
    chosen_first = probe.selection[0].id

    pipeline, _, feed = make_pipeline(store, cameras=10, fail_ids=(chosen_first,))
    result = await pipeline.tick()

    assert result.cameras_ok == 9
    assert result.cameras_failed == 1
    assert result.errors[0][0] == chosen_first
    assert "not_found" in result.errors[0][1]
    assert result.samples_written == 9 * len(DetectionClass)

    written = store.execute(
        "SELECT count(*) FROM density_samples WHERE camera_id = ?", [chosen_first]
    )
    assert written[0][0] == 0
    failed = store.execute(
        "SELECT ok, status_code FROM camera_frame_fetches WHERE camera_id = ?", [chosen_first]
    )
    assert failed == [(False, 404)]
    assert feed.calls >= 1


async def test_detection_failure_is_counted_but_not_a_frame_fetch_failure(store: Store) -> None:
    """The <2 % gate is measured from camera_frame_fetches; a model failure must not pollute it."""
    detector = FakeDetector(fails_on=lambda _: True)
    pipeline, _, _ = make_pipeline(store, cameras=6, detector=detector)
    result = await pipeline.tick()

    assert result.cameras_ok == 0
    assert result.cameras_failed == 6
    assert all(err.startswith("detect:") for _, err in result.errors)
    assert store.execute("SELECT count(*) FROM density_samples")[0][0] == 0
    assert store.execute("SELECT count(*) FILTER (WHERE NOT ok) FROM camera_frame_fetches")[0] == (
        0,
    )


async def test_camera_list_failure_keeps_the_previous_selection(store: Store) -> None:
    pipeline, _, feed = make_pipeline(store, cameras=5)
    await pipeline.tick()
    assert len(pipeline.selection) == 5

    feed.error = FeedUnavailable(FeedName.DOT_CAMERAS, "upstream 503", kind=ErrorKind.UPSTREAM_HTTP)
    result = await pipeline.tick()
    assert result.camera_list_error is not None
    assert "upstream 503" in result.camera_list_error
    assert result.cameras_ok == 5  # kept sampling the last known selection


async def test_camera_list_failure_with_no_selection_does_nothing(store: Store) -> None:
    pipeline, frames, _ = make_pipeline(
        store,
        cameras=5,
        camera_error=FeedUnavailable(FeedName.DOT_CAMERAS, "boom", kind=ErrorKind.UPSTREAM_HTTP),
    )
    result = await pipeline.tick()
    assert result.cameras_selected == 0
    assert result.cameras_ok == 0
    assert result.camera_list_error is not None
    assert frames.requested == []
    assert store.execute("SELECT count(*) FROM density_samples")[0][0] == 0


async def test_upsert_keeps_first_seen_and_updates_last_seen(store: Store) -> None:
    pipeline, _, _ = make_pipeline(store, cameras=3)
    await pipeline.tick()
    first = store.execute(
        "SELECT camera_id, epoch_ms(first_seen), epoch_ms(last_seen) FROM cameras ORDER BY 1"
    )
    await asyncio.sleep(0.01)
    await pipeline.tick()
    second = store.execute(
        "SELECT camera_id, epoch_ms(first_seen), epoch_ms(last_seen) FROM cameras ORDER BY 1"
    )
    assert len(second) == 3
    for (cid_a, first_a, last_a), (cid_b, first_b, last_b) in zip(first, second, strict=True):
        assert cid_a == cid_b
        assert first_a == first_b
        assert last_b >= last_a


async def test_pipeline_output_is_readable_by_the_density_service(store: Store) -> None:
    """The whole point: what the pipeline writes is what services/density.py serves."""
    pipeline, _, _ = make_pipeline(store, cameras=8)
    await pipeline.tick()

    env = density_now(store, window=timedelta(minutes=5))
    assert env.status == "fresh"
    assert len(env.records) == 8
    record = env.records[0]
    assert record.person_mean == 2.0
    assert record.vehicle_mean == 3.0
    assert record.name is not None
    assert record.lat != 0.0


async def test_pipeline_refuses_a_read_only_store(tmp_path: Path) -> None:
    path = tmp_path / "ro.duckdb"
    Store(path).close()
    with Store(path, read_only=True) as ro, pytest.raises(ValueError, match="writable Store"):
        DensityPipeline(
            store=ro,
            frames=FakeFrameSource(),
            detector=FakeDetector(),
            camera_feed=FakeCameraFeed(borough_cameras(2)),
        )


# -- runner ----------------------------------------------------------------


async def test_run_forever_honours_max_ticks_and_stop(store: Store) -> None:
    pipeline, _, _ = make_pipeline(store, cameras=3)
    ticks = await run_forever(pipeline, interval_s=0.01, max_ticks=3)
    assert ticks == 3
    assert pipeline.ticks_completed == 3

    stop = asyncio.Event()
    stop.set()
    assert await run_forever(pipeline, interval_s=0.01, stop=stop) == 0


async def test_run_forever_reports_each_tick(store: Store) -> None:
    pipeline, _, _ = make_pipeline(store, cameras=2)
    seen: list[str] = []
    await run_forever(
        pipeline, interval_s=0.01, max_ticks=2, on_tick=lambda r: seen.append(r.describe())
    )
    assert len(seen) == 2
    assert "cameras=2/2 ok" in seen[0]


# -- archive ---------------------------------------------------------------


async def test_daily_archive_writes_parquet_for_yesterday(store: Store, tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    pipeline, _, _ = make_pipeline(store, cameras=4, archive_dir=archive_dir)
    yesterday = (now_utc() - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)

    frames = FakeFrameSource(clock=yesterday)
    pipeline.frames = frames
    await pipeline.tick()

    written = pipeline.maybe_archive()
    day = yesterday.date()
    assert len(written) == 2
    assert {p.name for p in written} == {f"{day.isoformat()}.parquet"}
    assert (archive_dir / "density_samples" / f"{day.isoformat()}.parquet").exists()
    assert (archive_dir / "camera_frame_fetches" / f"{day.isoformat()}.parquet").exists()

    # idempotent per process-day
    assert pipeline.maybe_archive() == []

    rows = store.execute(
        "SELECT count(*) FROM read_parquet(?)",
        [str(archive_dir / "density_samples" / f"{day.isoformat()}.parquet")],
    )
    assert rows[0][0] == 4 * len(DetectionClass)


async def test_archive_skips_a_day_with_no_rows(store: Store, tmp_path: Path) -> None:
    pipeline, _, _ = make_pipeline(store, cameras=2, archive_dir=tmp_path / "archive")
    assert pipeline.maybe_archive() == []


async def test_archive_disabled_writes_nothing(store: Store, tmp_path: Path) -> None:
    pipeline, _, _ = make_pipeline(store, cameras=2)
    await pipeline.tick()
    assert pipeline.archive is False
    assert pipeline.maybe_archive() == []


def test_synthetic_jpeg_is_a_real_jpeg() -> None:
    assert synthetic_jpeg().startswith(b"\xff\xd8\xff")
