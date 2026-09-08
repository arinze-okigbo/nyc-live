"""Phase 3 gate, end to end: frame telemetry, the DuckDB store, and the density services.

Two boundaries matter here and neither is mocked:

* `nyc_live.feeds.cameras.CameraFrameSource` -> `nyc_live.services.persist_frame_telemetry`
  / `nyc_vision.pipeline.DensityPipeline.drain_telemetry` -> `camera_frame_fetches` ->
  `nyc_vision.report.frame_failure_rate`. With the upstreams unreachable every fetch
  really fails, and the point of the test is that the failure is RECORDED, not dropped.
* `nyc_vision.pipeline` writes `density_samples` + `cameras`; `nyc_live.services.density`
  reads them; `nyc_vision.service` re-exports that reader. All three must agree.

The `DensitySample` rows written here carry `model="fake:integration-tester"` and camera
ids starting with `int-cam-`. They are inputs for the aggregation path. Nothing in this
file observed the world, and no detector ran.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    DensitySample,
    DetectionClass,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    GeoQuery,
    now_utc,
)
from nyc_live.feeds.cameras import CameraFrameSource, CameraListAdapter
from nyc_live.http import make_client
from nyc_live.services import camera_frame, density_history, density_now
from nyc_live.store import Store
from nyc_vision import service as vision_service
from nyc_vision.detector import FakeDetector
from nyc_vision.pipeline import DensityPipeline
from nyc_vision.report import cameras_covered, frame_failure_rate
from tests.integration.conftest import (
    CAM_A,
    CAM_B,
    SYNTHETIC_MODEL,
    TIMES_SQ,
    seed_density,
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "vision.duckdb") as store:
        yield store


@pytest.fixture
def pipeline(
    db: Store, http_client: httpx.AsyncClient, integration_settings: Settings
) -> DensityPipeline:
    """The real pipeline over the real frame source and the real camera list adapter."""
    return DensityPipeline(
        store=db,
        frames=CameraFrameSource(client=http_client, settings=integration_settings),
        detector=FakeDetector(name="integration-tester"),
        camera_feed=CameraListAdapter(client=http_client, settings=integration_settings),
        camera_count=4,
        archive=False,
    )


# --------------------------------------------------------------------------- telemetry


async def test_frame_fetch_failures_are_recorded_not_dropped(
    db: Store, pipeline: DensityPipeline, http_client: httpx.AsyncClient
) -> None:
    """Every failed frame fetch reaches `camera_frame_fetches`, through both drain paths."""
    frames = pipeline.frames
    assert isinstance(frames, CameraFrameSource)
    started = now_utc()

    # the service-layer path (what nyc-mcp's get_camera_frame uses)
    for camera_id in (CAM_A, CAM_B):
        env = await camera_frame(frames, camera_id, store=db)
        assert env.status == "error" and env.records == []
        assert env.error is not None and env.error.kind is not ErrorKind.NOT_CONFIGURED
        assert env.error.url and env.error.url.endswith(f"/{camera_id}/image")

    # the pipeline path (what nyc-vision run uses)
    with pytest.raises(FeedUnavailable):
        await frames.get_frame("int-cam-c")
    assert pipeline.drain_telemetry() == 1
    assert frames.pending_telemetry == 0

    rows = db.execute(
        "SELECT camera_id, ok, error FROM camera_frame_fetches ORDER BY camera_id",
    )
    assert [r[0] for r in rows] == [CAM_A, CAM_B, "int-cam-c"]
    assert all(ok is False for _, ok, _ in rows)
    assert all(err for *_, err in rows), "a failed fetch must record why it failed"

    rate = frame_failure_rate(db, started - timedelta(minutes=1))
    assert rate.attempts == 3 and rate.failures == 3
    assert rate.rate == 1.0 and rate.rate_pct == 100.0
    assert rate.passes is False, "a 100% failure rate must not pass the <2% gate"
    assert sum(count for _, count in rate.top_errors) == 3
    assert all(message.startswith("upstream_http") for message, _ in rate.top_errors)
    assert "FAIL" in rate.describe()


async def test_top_errors_groups_by_failure_mode(db: Store, pipeline: DensityPipeline) -> None:
    """Three identical transport failures on three cameras are one failure mode, not three.

    Regression test for the defect this suite found: `camera_frame_fetches.error` used to
    hold "{kind}: {message}" with the per-camera URL inside it, so the gate report's
    `GROUP BY error` could never aggregate and "top errors" was five arbitrary singletons.
    """
    frames = pipeline.frames
    assert isinstance(frames, CameraFrameSource)
    started = now_utc()
    for camera_id in (CAM_A, CAM_B, "int-cam-c"):
        with pytest.raises(FeedUnavailable):
            await frames.get_frame(camera_id)
    pipeline.drain_telemetry()

    rate = frame_failure_rate(db, started - timedelta(minutes=1))
    assert rate.failures == 3
    assert len(rate.top_errors) == 1, rate.top_errors
    mode, count = rate.top_errors[0]
    assert count == 3
    assert mode == "upstream_http: ConnectError", mode

    # the column must stay low-cardinality whatever the upstream says: no URL, no camera
    # id, no attempt count, no free text. camera_id and status_code are separate columns.
    for (stored,) in db.execute("SELECT DISTINCT error FROM camera_frame_fetches"):
        assert isinstance(stored, str)
        assert len(stored) <= 64, stored
        assert "http" not in stored.removeprefix("upstream_http"), stored
        assert "/" not in stored and "int-cam-" not in stored, stored
        kind, _, reason = stored.partition(": ")
        assert kind in {k.value for k in ErrorKind}, stored
        assert reason == "" or re.fullmatch(r"[A-Za-z0-9_.\-]{1,32}", reason), stored


async def test_tick_with_an_unreachable_camera_list_writes_nothing(
    db: Store, pipeline: DensityPipeline
) -> None:
    """A dead camera list produces an honest empty tick, never invented zero-count rows."""
    result = await pipeline.tick()
    assert result.camera_list_error is not None, "the camera list outage must be reported"
    assert "webcams.nyctmc.org" in result.camera_list_error or result.camera_list_error
    assert result.cameras_selected == 0
    assert result.samples_written == 0
    assert result.cameras_ok == 0
    assert db.execute("SELECT count(*) FROM density_samples")[0][0] == 0
    assert db.execute("SELECT count(*) FROM cameras")[0][0] == 0

    env = density_now(db)
    assert env.status == "error" and env.records == []
    assert env.error is not None and env.error.kind is ErrorKind.NOT_CONFIGURED
    assert "nyc-vision" in env.error.message


# --------------------------------------------------------------------------- density reads


def test_density_now_reads_back_what_the_store_holds(db: Store) -> None:
    """The `cameras` upsert + `density_samples` contract between nyc-vision and services."""
    t = seed_density(db)
    env = density_now(db, window=timedelta(minutes=10), now=t)
    assert env.status == "fresh"
    assert env.stale_after is not None and env.fetched_at is not None
    assert env.stale_after - env.fetched_at == DEFAULT_TTL[FeedName.DENSITY]
    assert env.feed is FeedName.DENSITY
    by_camera = {r.camera_id: r for r in env.records}
    assert set(by_camera) == {CAM_A, CAM_B}
    a = by_camera[CAM_A]
    assert (a.sample_count, a.person_mean, a.person_max) == (2, 5.0, 6)
    assert (a.vehicle_mean, a.vehicle_max) == (3.5, 5)
    assert (a.lat, a.lon) == TIMES_SQ
    assert a.name == "Integration cam A"
    b = by_camera[CAM_B]
    assert (b.sample_count, b.person_mean, b.vehicle_mean) == (1, 0.0, 10.0)


def test_density_history_buckets_the_same_rows(db: Store) -> None:
    t = seed_density(db)
    env = density_history(db, window=timedelta(hours=1), bucket=timedelta(seconds=60), now=t)
    assert env.status == "fresh"
    assert len(env.records) == 3, [r.camera_id for r in env.records]
    assert [r.camera_id for r in env.records] == [CAM_A, CAM_A, CAM_B]
    for record in env.records:
        assert record.window_end - record.window_start <= timedelta(seconds=60)
        assert record.sample_count == 1


def test_density_samples_grow_over_a_short_window(db: Store) -> None:
    """The brief's Phase 3 assertion: the table grows and the service sees the growth."""
    t = seed_density(db)
    before = db.execute("SELECT count(*) FROM density_samples")[0][0]
    first = density_now(db, window=timedelta(minutes=10), now=t)
    first_counts = {r.camera_id: r.sample_count for r in first.records}

    db.insert_density_samples(
        [
            DensitySample(
                camera_id=CAM_A,
                ts=t,
                cls=cls,
                count=9 if cls is DetectionClass.PERSON else 0,
                model=SYNTHETIC_MODEL,
            )
            for cls in DetectionClass
        ]
    )
    after = db.execute("SELECT count(*) FROM density_samples")[0][0]
    assert after > before, "density_samples did not grow"
    assert after - before == len(DetectionClass), "one row per contract class, zeros included"

    second = density_now(db, window=timedelta(minutes=10), now=t + timedelta(seconds=1))
    second_counts = {r.camera_id: r.sample_count for r in second.records}
    assert second_counts[CAM_A] == first_counts[CAM_A] + 1
    assert second.records[0].person_max == 9

    coverage = cameras_covered(db, t - timedelta(hours=1))
    assert coverage.cameras == 2
    assert coverage.frames == 4
    assert coverage.samples == after


def test_geo_filtering_reaches_the_density_service(db: Store) -> None:
    t = seed_density(db)
    env = density_now(
        db,
        GeoQuery(lat=TIMES_SQ[0], lon=TIMES_SQ[1], radius_m=1000),
        window=timedelta(minutes=10),
        now=t,
    )
    assert env.status == "fresh"
    assert [r.camera_id for r in env.records] == [CAM_A]
    assert env.total_before_filter == 2
    assert env.records[0].distance_m is not None and env.records[0].distance_m < 1.0
    assert env.query is not None and env.query.radius_m == 1000


def test_nyc_vision_service_is_the_service_layer_not_a_second_implementation(db: Store) -> None:
    """One aggregation path: `nyc_vision.service` re-exports `nyc_live.services.density`."""
    assert vision_service.density_now is density_now
    assert vision_service.density_history is density_history
    t = seed_density(db)
    assert vision_service.density_now(db, now=t) == density_now(db, now=t)
    assert timedelta(minutes=5) == vision_service.DEFAULT_NOW_WINDOW


# --------------------------------------------------------------------------- live


@pytest.mark.live
@pytest.mark.slow
async def test_real_frame_failure_rate_is_under_the_gate_live(
    integration_settings: Settings, tmp_path: Path
) -> None:
    """Fetch real frames from real DOT cameras and measure the real failure rate."""
    sample_size = 25
    async with make_client(integration_settings) as client:
        store = Store(tmp_path / "live.duckdb")
        cameras = await CameraListAdapter(client=client, settings=integration_settings).fetch()
        online = [c for c in cameras.records if c.is_online][:sample_size]
        assert len(online) >= 10, f"only {len(online)} online cameras in the DOT list"
        frames = CameraFrameSource(client=client, settings=integration_settings)
        started = now_utc()
        for cam in online:
            try:
                await frames.get_frame(cam.id)
            except Exception:  # recorded as a failed row by the frame source
                continue
        store.record_frame_fetches(frames.drain_telemetry())
        rate = frame_failure_rate(store, started - timedelta(minutes=1))
        store.close()
    assert rate.attempts == len(online), rate.describe()
    assert rate.passes, rate.describe()
