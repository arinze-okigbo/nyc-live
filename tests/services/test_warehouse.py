"""services.warehouse: SELECT-only SQL wrapped in an Envelope."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nyc_live.contracts import (
    WAREHOUSE_READ_ONLY_TABLES,
    DensitySample,
    DetectionClass,
    ErrorKind,
    FeedName,
)
from nyc_live.services.warehouse import warehouse
from nyc_live.store import Store


def test_select_returns_fresh_envelope(store: Store) -> None:
    env = warehouse(store, "SELECT 1 AS one, 'a' AS s;")
    assert env.status == "fresh" and env.feed == FeedName.WAREHOUSE
    assert env.fetched_at == env.stale_after  # computed at call time, TTL 0
    (res,) = env.records
    assert res.columns == ["one", "s"] and res.rows == [[1, "a"]]
    assert res.sql == "SELECT 1 AS one, 'a' AS s"
    assert env.truncated is False and env.total_before_filter == 1


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM feed_fetches",
        "UPDATE cameras SET name = 'x'",
        "DROP TABLE density_samples",
        "CREATE TABLE t (x INT)",
        "SELECT 1; DROP TABLE cameras",
        "SELECT * FROM schema_meta",
        "PRAGMA database_size",
        "",
    ],
)
def test_dml_and_unknown_tables_are_internal_errors(store: Store, sql: str) -> None:
    env = warehouse(store, sql)
    assert env.status == "error" and env.records == []
    assert env.error is not None
    assert env.error.kind == ErrorKind.INTERNAL
    assert env.error.feed == FeedName.WAREHOUSE
    assert all(t in env.error.message for t in WAREHOUSE_READ_ONLY_TABLES)
    assert store.execute("SELECT COUNT(*) FROM cameras") == [(0,)]


def test_binder_error_is_internal_error(store: Store) -> None:
    env = warehouse(store, "SELECT no_such_column FROM cameras")
    assert env.status == "error" and env.error is not None
    assert env.error.kind == ErrorKind.INTERNAL
    assert "no_such_column" in env.error.message


def test_missing_store_is_internal_error() -> None:
    env = warehouse(None, "SELECT 1")
    assert env.status == "error" and env.error is not None
    assert env.error.kind == ErrorKind.INTERNAL
    assert "NYC_LIVE_DUCKDB_PATH" in env.error.message


def test_truncation(store: Store) -> None:
    env = warehouse(store, "SELECT unnest([1, 2, 3, 4, 5]) AS x", max_rows=3)
    (res,) = env.records
    assert res.row_count == 3 and res.truncated is True and env.truncated is True


def test_timestamptz_columns_come_back_as_iso_utc_strings(store: Store) -> None:
    """DuckDB needs pytz to hand back TIMESTAMPTZ cells; the service casts them instead."""
    ts = datetime(2026, 9, 8, 12, 0, 0, 250000, tzinfo=UTC)
    store.insert_density_samples(
        [DensitySample(camera_id="c", ts=ts, cls=DetectionClass.PERSON, count=3, model="m")]
    )
    store.record_feed_fetch(FeedName.WEATHER, ok=False, latency_ms=12.5, status_code=503)
    env = warehouse(store, 'SELECT camera_id, ts AS "when", count FROM density_samples')
    assert env.status == "fresh", env.error
    (res,) = env.records
    assert res.columns == ["camera_id", "when", "count"]
    assert res.rows == [["c", "2026-09-08T12:00:00.250000Z", 3]]
    assert res.sql == 'SELECT camera_id, ts AS "when", count FROM density_samples'
    env = warehouse(store, "SELECT * FROM feed_fetches")
    assert env.status == "fresh", env.error
    row = env.records[0].rows[0]
    assert row[0] == "weather" and row[1].endswith("Z") and row[3] == 503


def test_rows_are_json_serialisable(store: Store) -> None:
    env = warehouse(
        store,
        "SELECT CAST(1.5 AS DECIMAL(4,2)) AS d, [1, 2] AS l, {'a': 1} AS s, "
        "INTERVAL 1 HOUR AS i, DATE '2026-09-08' AS day, TIMESTAMP '2026-09-08 12:00' AS naive",
    )
    assert env.status == "fresh", env.error
    dumped = env.model_dump(mode="json")
    assert dumped["records"][0]["rows"] == [
        [1.5, [1, 2], {"a": 1}, "PT1H", "2026-09-08", "2026-09-08T12:00:00"]
    ]
