"""Phase 0 gate tests: the frozen contracts, schema, cache policy, and helpers work."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from nyc_live.cache import CachedFeed, FeedRegistry
from nyc_live.contracts import (
    DEFAULT_TTL,
    DUCKDB_SCHEMA,
    SCHEMA_VERSION,
    Camera,
    CameraSource,
    DensitySample,
    DetectionClass,
    ErrorKind,
    FeedAdapter,
    FeedName,
    FeedUnavailable,
    GeoQuery,
    Snapshot,
    now_utc,
)
from nyc_live.geo import filter_nearby, haversine_m
from nyc_live.store import Store


def test_every_feed_has_a_ttl() -> None:
    assert set(DEFAULT_TTL) == set(FeedName)


def test_schema_applies_and_is_versioned(store: Store) -> None:
    tables = store.tables()
    for stmt in DUCKDB_SCHEMA:
        name = stmt.split("IF NOT EXISTS")[1].split("(")[0].strip()
        assert name in tables
    assert store.execute("SELECT value FROM schema_meta WHERE key='schema_version'") == [
        (str(SCHEMA_VERSION),)
    ]


def test_density_sample_roundtrip(store: Store) -> None:
    ts = now_utc()
    row = DensitySample(
        camera_id="cam-1",
        ts=ts,
        cls=DetectionClass.PERSON,
        count=3,
        confidence_mean=0.71,
        bbox_area_frac_mean=0.02,
        model="yolo11n",
        inference_ms=41.0,
        frame_w=352,
        frame_h=240,
    )
    assert store.insert_density_samples([row]) == 1
    assert store.insert_density_samples([row]) == 1  # idempotent on PK
    res = store.query_readonly("SELECT camera_id, class, count FROM density_samples")
    assert res.rows == [["cam-1", "person", 3]]
    assert res.truncated is False


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE density_samples",
        "SELECT 1; DROP TABLE density_samples",
        "INSERT INTO density_samples VALUES (1)",
        "SELECT * FROM schema_meta",
        "COPY density_samples TO '/tmp/x.parquet'",
    ],
)
def test_warehouse_rejects_writes_and_hidden_tables(store: Store, sql: str) -> None:
    with pytest.raises(ValueError):
        store.query_readonly(sql)


def test_haversine_known_distance() -> None:
    # Times Square -> Grand Central is roughly 0.9 km
    d = haversine_m(40.7580, -73.9855, 40.7527, -73.9772)
    assert 850 < d < 1000


def test_filter_nearby_sets_distance_and_sorts() -> None:
    def cam(i: str, lat: float, lon: float) -> Camera:
        return Camera(
            id=i,
            source=CameraSource.NYC_DOT,
            name=i,
            is_online=True,
            image_url="x",
            lat=lat,
            lon=lon,
        )

    far = cam("far", 40.85, -73.90)
    near = cam("near", 40.7585, -73.9850)
    mid = cam("mid", 40.7527, -73.9772)
    out = filter_nearby([far, mid, near], GeoQuery(lat=40.7580, lon=-73.9855, radius_m=2000))
    assert [c.id for c in out] == ["near", "mid"]
    assert out[0].distance_m is not None and out[0].distance_m < out[1].distance_m  # type: ignore[operator]


class _FakeAdapter:
    name = FeedName.CITIBIKE
    ttl = timedelta(seconds=60)

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def is_configured(self) -> bool:
        return True

    async def fetch(self) -> Snapshot[Camera]:
        self.calls += 1
        if self.fail:
            raise FeedUnavailable(self.name, "boom", upstream_status=503)
        now = now_utc()
        return Snapshot[Camera](
            feed=self.name,
            fetched_at=now,
            stale_after=now + self.ttl,
            source_url="https://example.invalid",
            records=[],
        )


async def test_cache_fresh_then_stale_then_error(store: Store) -> None:
    adapter = _FakeAdapter()
    assert isinstance(adapter, FeedAdapter)
    feed: CachedFeed[Camera] = CachedFeed(adapter, store=store, max_stale=timedelta(seconds=0))

    env = await feed.get()
    assert env.status == "fresh" and env.error is None and adapter.calls == 1
    env = await feed.get()
    assert env.status == "fresh" and adapter.calls == 1  # served from cache

    adapter.fail = True
    env = await feed.get(force=True)
    # max_stale=0 but the snapshot is younger than ttl+max_stale so it is served stale
    assert env.status == "stale"
    assert env.error is not None and env.error.kind is ErrorKind.UPSTREAM_HTTP
    assert env.error.upstream_status == 503

    rows = store.execute("SELECT ok, error_kind FROM feed_fetches ORDER BY ts")
    assert rows == [(True, None), (False, "upstream_http")]

    reg = FeedRegistry([feed])
    health = reg.health()[0]
    assert health.consecutive_failures == 1 and health.last_ok_at is not None


async def test_cache_never_fetched_failure_is_error() -> None:
    adapter = _FakeAdapter()
    adapter.fail = True
    feed: CachedFeed[Camera] = CachedFeed(adapter)
    env = await feed.get()
    assert env.status == "error" and env.records == [] and env.error is not None
    assert feed.health().status == "error"


def test_archive_day_writes_parquet(store: Store, tmp_path: Path) -> None:
    ts = now_utc()
    store.insert_density_samples(
        [DensitySample(camera_id="c", ts=ts, cls=DetectionClass.CAR, count=1, model="m")]
    )
    out = store.archive_day("density_samples", ts.date(), tmp_path / "archive")
    assert out.exists() and out.suffix == ".parquet"
    assert store.execute(f"SELECT count(*) FROM read_parquet('{out}')") == [(1,)]
    with pytest.raises(ValueError):
        store.archive_day("schema_meta", ts.date(), tmp_path / "archive")
