"""DuckDB store: applies the frozen schema, records telemetry, archives to Parquet.

One connection per process, guarded by an asyncio-agnostic threading lock
(DuckDB connections are not thread-safe). Long-running writers (nyc-vision)
own their process's Store; readers (nyc-mcp, nyc-dash) open read-only.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from nyc_live.contracts import (
    DUCKDB_SCHEMA,
    PARQUET_ARCHIVE_TABLES,
    SCHEMA_VERSION,
    WAREHOUSE_READ_ONLY_TABLES,
    CameraFrameFetch,
    DensitySample,
    FeedError,
    FeedName,
    WarehouseResult,
    now_utc,
)

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|install|load|"
    r"pragma|set|call|truncate|vacuum|merge|replace)\b",
    re.IGNORECASE,
)

_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_SQL_STRING = re.compile(r"'(?:[^']|'')*'")
_STRING_SENTINEL = "\x00str\x00"
"""Structural checks run against a copy with comments stripped and string literals
replaced by this sentinel. Without it a keyword or a semicolon inside a quoted value
reads as syntax, and a quoted file path after FROM never reaches the table allow-list."""

_CTE_NAME = re.compile(
    r"(?:\bwith\b|,)\s+(?:recursive\s+)?([a-zA-Z_]\w*)\s*(?:\([^)]*\))?\s+as\s*\(",
    re.IGNORECASE,
)
_FROM_TARGET = re.compile(r"\b(?:from|join)\s+(\S+)", re.IGNORECASE)


def _mask_literals(sql: str) -> str:
    return _SQL_STRING.sub(_STRING_SENTINEL, _SQL_COMMENT.sub(" ", sql))


class Store:
    def __init__(self, path: Path | str = ":memory:", *, read_only: bool = False) -> None:
        self.path = Path(path) if path != ":memory:" else None
        self.read_only = read_only
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = duckdb.connect(str(path), read_only=read_only)
        if not read_only:
            self._apply_schema()

    # -- lifecycle ---------------------------------------------------------

    def _apply_schema(self) -> None:
        with self._lock:
            for stmt in DUCKDB_SCHEMA:
                self.conn.execute(stmt)
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES ('schema_version', ?)",
                [str(SCHEMA_VERSION)],
            )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- generic -----------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        with self._lock:
            return self.conn.execute(sql, params or []).fetchall()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self.conn.executemany(sql, rows)
        return len(rows)

    def tables(self) -> list[str]:
        return [r[0] for r in self.execute("SELECT table_name FROM duckdb_tables() ORDER BY 1")]

    # -- telemetry ---------------------------------------------------------

    def record_feed_fetch(
        self,
        feed: FeedName,
        *,
        ok: bool,
        latency_ms: float | None,
        record_count: int | None = None,
        status_code: int | None = None,
        error: FeedError | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO feed_fetches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                feed.value,
                now_utc(),
                ok,
                status_code
                if status_code is not None
                else (error.upstream_status if error else None),
                latency_ms,
                record_count,
                error.kind.value if error else None,
                error.message if error else None,
            ],
        )

    def record_frame_fetches(self, rows: Iterable[CameraFrameFetch]) -> int:
        return self.executemany(
            "INSERT INTO camera_frame_fetches VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (r.camera_id, r.ts, r.ok, r.status_code, r.latency_ms, r.byte_size, r.error)
                for r in rows
            ),
        )

    def insert_density_samples(self, rows: Iterable[DensitySample]) -> int:
        return self.executemany(
            "INSERT OR REPLACE INTO density_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    r.camera_id,
                    r.ts,
                    r.cls.value,
                    r.count,
                    r.confidence_mean,
                    r.bbox_area_frac_mean,
                    r.model,
                    r.inference_ms,
                    r.frame_w,
                    r.frame_h,
                )
                for r in rows
            ),
        )

    # -- warehouse (read-only SQL for the query_warehouse tool) ------------

    def query_readonly(self, sql: str, *, max_rows: int = 500) -> WarehouseResult:
        """SELECT-only over `WAREHOUSE_READ_ONLY_TABLES`, plus any CTE the query defines.

        Every structural check runs against `_mask_literals(sql)`, so content inside a
        quoted value is data rather than syntax. A quoted path or table function after
        FROM is rejected outright: DuckDB's replacement scan would otherwise read it
        off disk, and a bare-identifier allow-list never sees it.
        """
        cleaned = sql.strip().rstrip(";").strip()
        masked = _mask_literals(cleaned)
        if ";" in masked:
            raise ValueError("only a single statement is allowed")
        if not re.match(r"^(select|with)\b", masked, re.IGNORECASE):
            raise ValueError("only SELECT / WITH queries are allowed")
        if _FORBIDDEN_SQL.search(masked):
            raise ValueError("query contains a forbidden keyword; the warehouse is read-only")
        defined = {m.lower() for m in _CTE_NAME.findall(masked)}
        unknown: set[str] = set()
        for raw in _FROM_TARGET.findall(masked):
            if raw.startswith("("):  # subquery
                continue
            if _STRING_SENTINEL in raw or "(" in raw:
                raise ValueError(
                    "only plain table names may follow FROM / JOIN; reading a file path or "
                    "table function is not allowed"
                )
            name = raw.rstrip(",)").strip('"').lower().removeprefix("main.")
            if name and name not in defined and name not in WAREHOUSE_READ_ONLY_TABLES:
                unknown.add(name)
        if unknown:
            raise ValueError(f"unknown or non-queryable table(s): {sorted(unknown)}")
        started = time.perf_counter()
        with self._lock:
            cur = self.conn.execute(f"SELECT * FROM ({cleaned}) LIMIT {max_rows + 1}")
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchall()
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return WarehouseResult(
            sql=cleaned,
            columns=columns,
            rows=[list(r) for r in rows],
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    # -- parquet archive ---------------------------------------------------

    def archive_day(self, table: str, day: date, archive_dir: Path) -> Path:
        """Write one day of an append-only table to Parquet. Idempotent per (table, day)."""
        if table not in PARQUET_ARCHIVE_TABLES:
            raise ValueError(f"{table} is not an archivable table")
        ts_col = "observed_at" if table == "weather_observations" else "ts"
        out_dir = archive_dir / table
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{day.isoformat()}.parquet"
        with self._lock:
            # `AT TIME ZONE 'UTC'` is load-bearing, not decoration. Every ts column is
            # TIMESTAMPTZ and every caller passes a UTC-derived `day` (contracts.now_utc),
            # but a bare CAST(ts AS DATE) resolves in DuckDB's *session* timezone. In
            # America/New_York that silently disagrees with UTC for the four or five hours
            # each evening after local midnight has yet to arrive but UTC's has passed --
            # so a row written at 00:30Z landed under the previous local date and this
            # COPY wrote an EMPTY parquet file for the day it was asked to archive.
            # Caught by the suite failing only when run after 20:00 ET.
            self.conn.execute(
                f"COPY (SELECT * FROM {table} WHERE CAST({ts_col} AT TIME ZONE 'UTC' AS DATE) = ?) "
                f"TO '{out}' (FORMAT PARQUET)",
                [day],
            )
        return out
