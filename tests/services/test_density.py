"""services.density over an in-memory Store with synthetic density_samples rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nyc_live.contracts import (
    DEFAULT_TTL,
    Camera,
    CameraSource,
    DensitySample,
    DetectionClass,
    ErrorKind,
    FeedName,
    GeoQuery,
)
from nyc_live.services.density import camera_density_history, density_history, density_now
from nyc_live.store import Store

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=1)


def seed(store: Store) -> None:
    rows: list[DensitySample] = []
    for i in range(8):  # one frame a minute for 8 minutes
        ts = T0 - timedelta(minutes=i)
        rows += [
            DensitySample(camera_id="cam1", ts=ts, cls=DetectionClass.PERSON, count=i, model="m"),
            DensitySample(camera_id="cam1", ts=ts, cls=DetectionClass.CAR, count=2, model="m"),
            DensitySample(camera_id="cam1", ts=ts, cls=DetectionClass.TRUCK, count=1, model="m"),
            DensitySample(camera_id="cam2", ts=ts, cls=DetectionClass.BUS, count=1, model="m"),
            DensitySample(camera_id="cam3", ts=ts, cls=DetectionClass.PERSON, count=9, model="m"),
        ]
    store.insert_density_samples(rows)
    store.execute(
        "INSERT INTO cameras VALUES ('cam1', 'nyc_dot', 'Cam One', 40.75, -73.98, true, ?, ?)",
        [T0, T0],
    )


CAM2 = Camera(
    id="cam2",
    source=CameraSource.NYC_DOT,
    name="Cam Two",
    is_online=True,
    image_url="https://cams.test/2",
    lat=40.70,
    lon=-74.00,
)


def test_no_rows_is_not_configured_error(store: Store) -> None:
    for fn in (density_now, density_history):
        env = fn(store, now=NOW)
        assert env.status == "error" and env.records == []
        assert env.error is not None
        assert env.error.kind == ErrorKind.NOT_CONFIGURED
        assert env.error.feed == FeedName.DENSITY
        assert "nyc-vision has not produced density samples yet" in env.error.message


def test_missing_store_is_internal_error() -> None:
    env = density_now(None)
    assert env.status == "error" and env.error is not None
    assert env.error.kind == ErrorKind.INTERNAL


def test_density_now_aggregates_per_camera(store: Store) -> None:
    seed(store)
    env = density_now(store, now=NOW, cameras=[CAM2])
    assert env.status == "fresh"
    assert env.fetched_at == NOW and env.stale_after == NOW + DEFAULT_TTL[FeedName.DENSITY]
    by_id = {r.camera_id: r for r in env.records}
    assert set(by_id) == {"cam1", "cam2"}  # cam3 has samples but no location anywhere
    c1 = by_id["cam1"]
    assert c1.name == "Cam One" and (c1.lat, c1.lon) == (40.75, -73.98)
    assert c1.sample_count == 5  # minutes 0..4 fall inside the 5 min window
    assert c1.person_mean == pytest.approx((0 + 1 + 2 + 3 + 4) / 5)
    assert c1.person_max == 4
    assert c1.vehicle_mean == 3.0 and c1.vehicle_max == 3
    assert c1.latest_ts == T0
    assert c1.window_start == NOW - timedelta(minutes=5) and c1.window_end == NOW
    c2 = by_id["cam2"]
    assert c2.name == "Cam Two" and c2.lat == 40.70  # from the in-memory fallback
    assert c2.person_mean == 0.0 and c2.vehicle_mean == 1.0
    assert env.total_before_filter == 3


def test_density_now_geo_filter_and_camera_id(store: Store) -> None:
    seed(store)
    env = density_now(store, GeoQuery(lat=40.75, lon=-73.98, radius_m=200), now=NOW, cameras=[CAM2])
    assert [r.camera_id for r in env.records] == ["cam1"]
    assert env.records[0].distance_m == 0.0 and env.query is not None
    env = density_now(store, camera_id="cam2", now=NOW, cameras=[CAM2])
    assert [r.camera_id for r in env.records] == ["cam2"]
    env = density_now(store, camera_id="nope", now=NOW)
    assert env.status == "error" and env.error is not None
    assert env.error.kind == ErrorKind.NOT_CONFIGURED


def test_density_now_only_unlocated_cameras_is_internal_error(store: Store) -> None:
    seed(store)
    env = density_now(store, camera_id="cam3", now=NOW)
    assert env.status == "error" and env.error is not None
    assert env.error.kind == ErrorKind.INTERNAL
    assert "cameras" in env.error.message


def test_density_history_buckets(store: Store) -> None:
    seed(store)
    env = density_history(
        store, camera_id="cam1", window=timedelta(minutes=10), bucket=timedelta(minutes=5), now=NOW
    )
    assert env.status == "fresh"
    starts = [(r.window_start, r.window_end, r.sample_count) for r in env.records]
    assert starts == [
        (T0 - timedelta(minutes=10), T0 - timedelta(minutes=5), 2),  # minutes 6, 7
        (T0 - timedelta(minutes=5), T0, 5),  # minutes 1..5
        (T0, NOW, 1),  # minute 0, bucket clipped to window end
    ]
    assert env.records[1].person_mean == pytest.approx((1 + 2 + 3 + 4 + 5) / 5)
    assert env.records[0].person_max == 7


def test_density_history_limit_and_geo(store: Store) -> None:
    seed(store)
    env = density_history(
        store,
        window=timedelta(hours=1),
        bucket=timedelta(minutes=5),
        limit=2,
        cameras=[CAM2],
        now=NOW,
    )
    assert len(env.records) == 2 and env.truncated is True
    env = density_history(
        store,
        GeoQuery(lat=40.70, lon=-74.00, radius_m=100),
        window=timedelta(hours=1),
        bucket=timedelta(minutes=30),
        cameras=[CAM2],
        now=NOW,
    )
    assert {r.camera_id for r in env.records} == {"cam2"}


def test_density_history_rejects_non_positive_bucket(store: Store) -> None:
    env = density_history(store, bucket=timedelta(0))
    assert env.status == "error" and env.error is not None
    assert env.error.kind == ErrorKind.INTERNAL


# --------------------------------------------------------------------------- camera_density_history


def test_camera_density_history_requires_camera_id(store: Store) -> None:
    env = camera_density_history(store, "", now=NOW)
    assert env.status == "error" and env.error is not None
    assert env.error.kind == ErrorKind.INTERNAL
    assert "camera_id" in env.error.message


def test_camera_density_history_rejects_non_positive_since_s(store: Store) -> None:
    env = camera_density_history(store, "cam1", since_s=0, now=NOW)
    assert env.status == "error" and env.error is not None
    assert env.error.kind == ErrorKind.INTERNAL
    assert "since_s" in env.error.message


def test_camera_density_history_defaults_to_adaptive_bucket(store: Store) -> None:
    seed(store)
    env = camera_density_history(store, "cam1", since_s=600, now=NOW)
    assert env.status == "fresh"
    assert all(r.camera_id == "cam1" for r in env.records)
    # since_s=600 / 120 target points == 5 s, floored to the 30 s minimum bucket
    assert env.records[0].window_end - env.records[0].window_start <= timedelta(seconds=30)


def test_camera_density_history_matches_density_history_for_one_camera(store: Store) -> None:
    seed(store)
    explicit = density_history(
        store, camera_id="cam1", window=timedelta(minutes=10), bucket=timedelta(minutes=5), now=NOW
    )
    via_wrapper = camera_density_history(store, "cam1", since_s=600, bucket_s=300, now=NOW)
    assert via_wrapper.records == explicit.records


def test_camera_density_history_no_samples_is_not_configured_error(store: Store) -> None:
    env = camera_density_history(store, "cam1", now=NOW)
    assert env.status == "error" and env.error is not None
    assert env.error.kind == ErrorKind.NOT_CONFIGURED
