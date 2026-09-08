"""`query_warehouse`: read-only SQL over the DuckDB store, wrapped in an Envelope."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from nyc_live.contracts import (
    WAREHOUSE_READ_ONLY_TABLES,
    Envelope,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    WarehouseResult,
    now_utc,
)
from nyc_live.store import Store

log = logging.getLogger(__name__)

_JSON_SCALARS = (str, int, float, bool, datetime, date, timedelta)


def _jsonable(value: Any) -> Any:
    """Coerce DuckDB cell values to something `model_dump(mode="json")` can serialise."""
    if value is None or isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID | bytes | bytearray):
        return str(value)
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _error(message: str, kind: ErrorKind = ErrorKind.INTERNAL) -> Envelope[WarehouseResult]:
    return Envelope[WarehouseResult](
        feed=FeedName.WAREHOUSE,
        status="error",
        fetched_at=None,
        stale_after=None,
        records=[],
        error=FeedUnavailable(FeedName.WAREHOUSE, message, kind=kind).to_model(),
    )


_TZ_TYPES = {"TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ"}


def _needs_pytz(exc: Exception) -> bool:
    return "pytz" in str(exc)


def _wrap_tz_columns(store: Store, validated_sql: str) -> str | None:
    """Re-express a query so TIMESTAMPTZ columns come back as ISO-8601 UTC strings.

    DuckDB's Python client needs `pytz` to materialise TIMESTAMPTZ cells and every
    warehouse table has one. `validated_sql` must already have passed
    `Store.query_readonly`'s SELECT-only checks (validation runs before execution, so an
    execution-time failure implies the statement was accepted).
    """
    described = store.execute(f"DESCRIBE SELECT * FROM ({validated_sql})")
    if not described:
        return None
    cols: list[str] = []
    for name, col_type, *_ in described:
        quoted = '"' + str(name).replace('"', '""') + '"'
        if str(col_type).upper() in _TZ_TYPES:
            cols.append(
                f"strftime({quoted} AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%fZ') AS {quoted}"
            )
        else:
            cols.append(quoted)
    return f"SELECT {', '.join(cols)} FROM ({validated_sql})"


def warehouse(store: Store | None, sql: str, *, max_rows: int = 500) -> Envelope[WarehouseResult]:
    """Run a SELECT-only query. Rejections (DML, unknown tables) become `kind=internal` errors."""
    if store is None:
        return _error(
            "DuckDB store is not open (could not open NYC_LIVE_DUCKDB_PATH); "
            "warehouse queries are unavailable in this process"
        )
    try:
        try:
            result = store.query_readonly(sql, max_rows=max_rows)
        except ValueError:
            raise
        except Exception as exc:
            if not _needs_pytz(exc):
                raise
            wrapped = _wrap_tz_columns(store, sql.strip().rstrip(";").strip())
            if wrapped is None:
                raise
            result = store.query_readonly(wrapped, max_rows=max_rows)
            result = result.model_copy(update={"sql": sql.strip().rstrip(";").strip()})
    except ValueError as exc:
        return _error(f"{exc}. Queryable tables: {', '.join(WAREHOUSE_READ_ONLY_TABLES)}")
    except Exception as exc:
        # DuckDB binder / parser errors (bad column, syntax) are user errors too
        log.warning("warehouse query failed: %s", exc)
        return _error(f"{type(exc).__name__}: {exc}")
    result = result.model_copy(update={"rows": [[_jsonable(v) for v in r] for r in result.rows]})
    t = now_utc()
    return Envelope[WarehouseResult](
        feed=FeedName.WAREHOUSE,
        status="fresh",
        fetched_at=t,
        stale_after=t,
        records=[result],
        total_before_filter=1,
        truncated=result.truncated,
    )
