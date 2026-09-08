from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from nyc_live.contracts import CameraFrameFetch, DensitySample, DetectionClass
from nyc_live.store import Store
from nyc_vision.report import (
    GATE_MAX_FAILURE_RATE,
    cameras_covered,
    day_bounds_utc,
    frame_failure_rate,
    hourly_rush,
    rush_summary,
)

NY = ZoneInfo("America/New_York")


def insert_fetches(store: Store, *, ok: int, failed: int, cameras: int, base: datetime) -> None:
    rows: list[CameraFrameFetch] = []
    for i in range(ok):
        rows.append(
            CameraFrameFetch(
                camera_id=f"cam-{i % cameras}",
                ts=base + timedelta(seconds=i),
                ok=True,
                status_code=200,
                latency_ms=30.0,
                byte_size=12345,
            )
        )
    for i in range(failed):
        rows.append(
            CameraFrameFetch(
                camera_id=f"cam-{i % cameras}",
                ts=base + timedelta(seconds=1000 + i),
                ok=False,
                status_code=502,
                latency_ms=15.0,
                error="upstream_http: 502 from webcams.nyctmc.org",
            )
        )
    store.record_frame_fetches(rows)


def frame_rows(camera_id: str, ts: datetime, *, persons: int, cars: int) -> list[DensitySample]:
    """The six rows one detection produces, exactly as the pipeline writes them."""
    counts = {DetectionClass.PERSON: persons, DetectionClass.CAR: cars}
    return [
        DensitySample(
            camera_id=camera_id,
            ts=ts,
            cls=cls,
            count=counts.get(cls, 0),
            confidence_mean=0.8 if counts.get(cls, 0) else None,
            model="test-model",
            frame_w=640,
            frame_h=480,
        )
        for cls in DetectionClass
    ]


def insert_frame(store: Store, camera_id: str, ts: datetime, *, persons: int, cars: int) -> None:
    store.insert_density_samples(frame_rows(camera_id, ts, persons=persons, cars=cars))


# -- frame_failure_rate ---------------------------------------------------


def test_failure_rate_over_recorded_attempts(store: Store) -> None:
    base = datetime.now(UTC) - timedelta(hours=2)
    insert_fetches(store, ok=198, failed=2, cameras=50, base=base)

    rate = frame_failure_rate(store, base - timedelta(minutes=1))
    assert rate.attempts == 200
    assert rate.failures == 2
    assert rate.cameras == 50
    assert rate.rate == 0.01
    assert rate.rate_pct == 1.0
    assert rate.passes is True
    assert "1.000%" in rate.describe()
    assert "PASS" in rate.describe()
    assert rate.top_errors[0][1] == 2
    assert "502" in rate.top_errors[0][0]


def test_failure_rate_fails_the_gate_above_two_percent(store: Store) -> None:
    base = datetime.now(UTC) - timedelta(hours=1)
    insert_fetches(store, ok=95, failed=5, cameras=10, base=base)
    rate = frame_failure_rate(store, base - timedelta(minutes=1))
    assert rate.rate == 0.05
    assert rate.rate > GATE_MAX_FAILURE_RATE
    assert rate.passes is False
    assert "FAIL" in rate.describe()


def test_failure_rate_with_no_rows_does_not_pass_and_says_so(store: Store) -> None:
    rate = frame_failure_rate(store, datetime.now(UTC) - timedelta(hours=24))
    assert rate.attempts == 0
    assert rate.failures == 0
    assert rate.rate == 0.0
    assert rate.passes is False
    assert "no camera_frame_fetches rows" in rate.describe()


def test_failure_rate_respects_the_window(store: Store) -> None:
    old = datetime.now(UTC) - timedelta(days=3)
    insert_fetches(store, ok=10, failed=10, cameras=2, base=old)
    recent = datetime.now(UTC) - timedelta(minutes=30)
    insert_fetches(store, ok=100, failed=1, cameras=2, base=recent)

    rate = frame_failure_rate(store, datetime.now(UTC) - timedelta(hours=1))
    assert rate.attempts == 101
    assert rate.failures == 1


# -- cameras_covered ------------------------------------------------------


def test_cameras_covered_counts_frames_not_rows(store: Store) -> None:
    start = datetime.now(UTC) - timedelta(hours=25)
    rows: list[DensitySample] = []
    for cam in range(52):
        for hour in (0, 12, 24):
            rows += frame_rows(f"cam-{cam}", start + timedelta(hours=hour), persons=1, cars=2)
    store.insert_density_samples(rows)

    coverage = cameras_covered(store, start - timedelta(minutes=1))
    assert coverage.cameras == 52
    assert coverage.frames == 52 * 3
    assert coverage.samples == 52 * 3 * len(DetectionClass)
    assert coverage.span_hours == 24.0
    assert coverage.passes is True
    assert "52 cameras" in coverage.describe()
    assert "PASS" in coverage.describe()


def test_cameras_covered_fails_below_fifty_cameras(store: Store) -> None:
    start = datetime.now(UTC) - timedelta(hours=25)
    rows: list[DensitySample] = []
    for cam in range(10):
        for hour in (0, 24):
            rows += frame_rows(f"cam-{cam}", start + timedelta(hours=hour), persons=1, cars=0)
    store.insert_density_samples(rows)
    coverage = cameras_covered(store, start - timedelta(minutes=1))
    assert coverage.cameras == 10
    assert coverage.passes is False
    assert "FAIL" in coverage.describe()


def test_cameras_covered_with_no_rows(store: Store) -> None:
    coverage = cameras_covered(store, datetime.now(UTC) - timedelta(hours=24))
    assert coverage.cameras == 0
    assert coverage.frames == 0
    assert coverage.span_hours == 0.0
    assert coverage.first_ts is None
    assert coverage.passes is False
    assert "no density_samples rows" in coverage.describe()


# -- hourly_rush ----------------------------------------------------------


def test_day_bounds_cover_one_local_day() -> None:
    start, end = day_bounds_utc(date(2026, 9, 8), "America/New_York")
    assert (end - start) == timedelta(hours=24)
    assert start.astimezone(NY).hour == 0
    assert start.tzinfo is UTC


def test_hourly_rush_averages_per_frame_across_cameras(store: Store) -> None:
    day = date(2026, 9, 8)
    # 08:00 local: two cameras, 10 and 20 people -> mean 15
    for cam, persons in (("a", 10), ("b", 20)):
        insert_frame(
            store,
            cam,
            datetime(2026, 9, 8, 8, 0, tzinfo=NY).astimezone(UTC),
            persons=persons,
            cars=4,
        )
    # 03:00 local: one camera, 1 person
    insert_frame(
        store, "a", datetime(2026, 9, 8, 3, 0, tzinfo=NY).astimezone(UTC), persons=1, cars=0
    )

    rows = hourly_rush(store, day, tz="America/New_York")
    by_hour = {r.hour: r for r in rows}
    assert sorted(by_hour) == [3, 8]
    assert by_hour[8].person_mean == 15.0
    assert by_hour[8].person_max == 20
    assert by_hour[8].vehicle_mean == 4.0
    assert by_hour[8].frames == 2
    assert by_hour[8].cameras == 2
    assert by_hour[3].person_mean == 1.0
    # hours with no frames are absent, not zero-filled
    assert 12 not in by_hour


def test_hourly_rush_uses_local_time_not_utc(store: Store) -> None:
    """18:00 UTC in September is 14:00 in New York; the bucket must be the local hour."""
    insert_frame(store, "a", datetime(2026, 9, 8, 18, 0, tzinfo=UTC), persons=5, cars=1)
    rows = hourly_rush(store, date(2026, 9, 8), tz="America/New_York")
    assert [r.hour for r in rows] == [14]


def test_hourly_rush_excludes_other_days(store: Store) -> None:
    insert_frame(
        store, "a", datetime(2026, 9, 7, 12, 0, tzinfo=NY).astimezone(UTC), persons=9, cars=9
    )
    assert hourly_rush(store, date(2026, 9, 8), tz="America/New_York") == []


def test_rush_summary_names_both_peaks(store: Store) -> None:
    for hour, persons in ((6, 1), (8, 30), (12, 5), (18, 20), (22, 2)):
        insert_frame(
            store,
            "a",
            datetime(2026, 9, 8, hour, 0, tzinfo=NY).astimezone(UTC),
            persons=persons,
            cars=1,
        )
    text = rush_summary(hourly_rush(store, date(2026, 9, 8), tz="America/New_York"))
    assert "AM peak 08:00" in text
    assert "PM peak 18:00" in text
    assert "5 hours with data" in text


def test_rush_summary_with_no_rows() -> None:
    assert "no density_samples rows" in rush_summary([])
