"""`density_now` / `density_history`: aggregates over `density_samples` written by nyc-vision.

Rows are (camera_id, ts, class, count). A "frame" is one (camera_id, ts). Per frame we sum
person counts and vehicle counts (VEHICLE_CLASSES); per camera (and per time bucket for
history) we then take mean / max over frames. Camera name and coordinates come from the
`cameras` table (maintained by nyc-vision) with an optional in-memory Camera list as a
fallback so the tools work before that table is populated.

With no rows in the window the result is `status="error"`, `kind=not_configured`,
saying nyc-vision has not produced samples yet. Never an empty success.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from nyc_live.contracts import (
    DEFAULT_TTL,
    VEHICLE_CLASSES,
    Camera,
    CameraDensity,
    DetectionClass,
    Envelope,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    GeoQuery,
    now_utc,
)
from nyc_live.geo import filter_nearby
from nyc_live.store import Store

log = logging.getLogger(__name__)

DEFAULT_NOW_WINDOW = timedelta(minutes=5)
DEFAULT_HISTORY_WINDOW = timedelta(hours=24)
DEFAULT_BUCKET = timedelta(minutes=15)

_VEHICLE_SQL_LIST = ", ".join(f"'{c.value}'" for c in sorted(VEHICLE_CLASSES))
_PERSON = DetectionClass.PERSON.value

_FRAMES_CTE = f"""
    WITH frames AS (
        SELECT camera_id, ts,
               SUM(CASE WHEN class = '{_PERSON}' THEN count ELSE 0 END) AS persons,
               SUM(CASE WHEN class IN ({_VEHICLE_SQL_LIST}) THEN count ELSE 0 END) AS vehicles
        FROM density_samples
        WHERE ts >= ? AND ts < ? {{camera_filter}}
        GROUP BY camera_id, ts
    )
"""

_NOW_SQL = (
    _FRAMES_CTE
    + """
    SELECT f.camera_id, COUNT(*) AS n,
           AVG(f.persons), AVG(f.vehicles), MAX(f.persons), MAX(f.vehicles), epoch_ms(MAX(f.ts)),
           ANY_VALUE(c.name), ANY_VALUE(c.lat), ANY_VALUE(c.lon)
    FROM frames f LEFT JOIN cameras c ON c.camera_id = f.camera_id
    GROUP BY f.camera_id
    ORDER BY f.camera_id
"""
)

_HISTORY_SQL = (
    _FRAMES_CTE
    + """
    SELECT f.camera_id, COUNT(*) AS n,
           AVG(f.persons), AVG(f.vehicles), MAX(f.persons), MAX(f.vehicles), epoch_ms(MAX(f.ts)),
           ANY_VALUE(c.name), ANY_VALUE(c.lat), ANY_VALUE(c.lon),
           epoch_ms(time_bucket(INTERVAL (?) SECOND, f.ts)) AS bucket
    FROM frames f LEFT JOIN cameras c ON c.camera_id = f.camera_id
    GROUP BY f.camera_id, bucket
    ORDER BY f.camera_id, bucket
"""
)


def _error(message: str, kind: ErrorKind) -> Envelope[CameraDensity]:
    return Envelope[CameraDensity](
        feed=FeedName.DENSITY,
        status="error",
        fetched_at=None,
        stale_after=None,
        records=[],
        error=FeedUnavailable(FeedName.DENSITY, message, kind=kind).to_model(),
    )


def _no_samples(window_start: datetime, window_end: datetime) -> Envelope[CameraDensity]:
    return _error(
        "nyc-vision has not produced density samples yet "
        f"(no density_samples rows between {window_start:%Y-%m-%d %H:%M:%SZ} and "
        f"{window_end:%Y-%m-%d %H:%M:%SZ}); run `just vision run` to start the detector",
        ErrorKind.NOT_CONFIGURED,
    )


def _store_missing() -> Envelope[CameraDensity]:
    return _error(
        "DuckDB store is not open (could not open NYC_LIVE_DUCKDB_PATH); "
        "density is unavailable in this process",
        ErrorKind.INTERNAL,
    )


def _from_epoch_ms(value: Any) -> datetime:
    """Timestamps are selected as `epoch_ms(...)`: DuckDB needs `pytz` to return TIMESTAMPTZ
    cells as Python datetimes, and that package is not a dependency."""
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _rows_to_records(
    rows: list[tuple[Any, ...]],
    *,
    window_start: datetime,
    window_end: datetime,
    bucket: timedelta | None,
    fallback: dict[str, Camera],
) -> tuple[list[CameraDensity], int]:
    """Build CameraDensity rows; returns (records, dropped_without_location)."""
    records: list[CameraDensity] = []
    dropped = 0
    for row in rows:
        cam_id, n, p_mean, v_mean, p_max, v_max, latest, name, lat, lon = row[:10]
        cam = fallback.get(cam_id)
        if lat is None or lon is None:
            if cam is None:
                dropped += 1
                continue
            lat, lon = cam.lat, cam.lon
        if name is None and cam is not None:
            name = cam.name
        if bucket is not None:
            start = _from_epoch_ms(row[10])
            end = min(start + bucket, window_end)
        else:
            start, end = window_start, window_end
        records.append(
            CameraDensity(
                lat=float(lat),
                lon=float(lon),
                camera_id=cam_id,
                name=name,
                window_start=start,
                window_end=end,
                sample_count=int(n),
                person_mean=round(float(p_mean), 3),
                vehicle_mean=round(float(v_mean), 3),
                person_max=int(p_max),
                vehicle_max=int(v_max),
                latest_ts=_from_epoch_ms(latest) if latest is not None else None,
            )
        )
    return records, dropped


def _finish(
    records: list[CameraDensity],
    *,
    query: GeoQuery | None,
    limit: int | None,
    total: int,
    t: datetime,
) -> Envelope[CameraDensity]:
    if query is not None:
        records = filter_nearby(records, query)
    truncated = limit is not None and len(records) > limit
    if truncated and limit is not None:
        records = records[:limit]
    return Envelope[CameraDensity](
        feed=FeedName.DENSITY,
        status="fresh",
        fetched_at=t,
        stale_after=t + DEFAULT_TTL[FeedName.DENSITY],
        records=records,
        query=query,
        total_before_filter=total,
        truncated=truncated,
    )


def density_now(
    store: Store | None,
    query: GeoQuery | None = None,
    *,
    window: timedelta = DEFAULT_NOW_WINDOW,
    camera_id: str | None = None,
    limit: int | None = 100,
    cameras: Iterable[Camera] = (),
    now: datetime | None = None,
) -> Envelope[CameraDensity]:
    """One CameraDensity per camera over the trailing `window`, nearest first when queried."""
    if store is None:
        return _store_missing()
    t = now or now_utc()
    start = t - window
    fallback = {c.id: c for c in cameras}
    sql, params = _bind(_NOW_SQL, start, t, camera_id=camera_id)
    try:
        rows = store.execute(sql, params)
    except Exception as exc:
        log.exception("density_now query failed")
        return _error(f"{type(exc).__name__}: {exc}", ErrorKind.INTERNAL)
    if not rows:
        return _no_samples(start, t)
    records, dropped = _rows_to_records(
        rows, window_start=start, window_end=t, bucket=None, fallback=fallback
    )
    if dropped:
        log.warning("density_now: %d camera(s) have samples but no known location", dropped)
    if not records:
        return _error(
            f"{dropped} camera(s) have density samples but none has a location in the "
            "`cameras` table; nyc-vision must upsert camera metadata",
            ErrorKind.INTERNAL,
        )
    return _finish(records, query=query, limit=limit, total=len(rows), t=t)


def density_history(
    store: Store | None,
    query: GeoQuery | None = None,
    *,
    camera_id: str | None = None,
    window: timedelta = DEFAULT_HISTORY_WINDOW,
    bucket: timedelta = DEFAULT_BUCKET,
    limit: int | None = 500,
    cameras: Iterable[Camera] = (),
    now: datetime | None = None,
) -> Envelope[CameraDensity]:
    """One CameraDensity per (camera, time bucket) over the trailing `window`.

    Ordered by camera then bucket start. `query` keeps only cameras within radius; `limit`
    caps the number of (camera, bucket) rows and sets `truncated` when it bites.
    """
    if store is None:
        return _store_missing()
    if bucket <= timedelta(0):
        return _error("bucket must be positive", ErrorKind.INTERNAL)
    t = now or now_utc()
    start = t - window
    fallback = {c.id: c for c in cameras}
    sql, params = _bind(_HISTORY_SQL, start, t, camera_id=camera_id)
    params.append(int(bucket.total_seconds()))
    try:
        rows = store.execute(sql, params)
    except Exception as exc:
        log.exception("density_history query failed")
        return _error(f"{type(exc).__name__}: {exc}", ErrorKind.INTERNAL)
    if not rows:
        return _no_samples(start, t)
    records, dropped = _rows_to_records(
        rows, window_start=start, window_end=t, bucket=bucket, fallback=fallback
    )
    if dropped:
        log.warning("density_history: %d bucket row(s) belong to cameras without location", dropped)
    if not records:
        return _error(
            "density samples exist but no camera in the window has a location in the "
            "`cameras` table; nyc-vision must upsert camera metadata",
            ErrorKind.INTERNAL,
        )
    return _finish(records, query=query, limit=limit, total=len(rows), t=t)


def _bind(
    template: str, start: datetime, end: datetime, *, camera_id: str | None
) -> tuple[str, list[Any]]:
    params: list[Any] = [start, end]
    if camera_id is not None:
        sql = template.format(camera_filter="AND camera_id = ?")
        params.append(camera_id)
    else:
        sql = template.format(camera_filter="")
    return sql, params
