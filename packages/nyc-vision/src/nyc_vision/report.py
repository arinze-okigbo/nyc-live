"""Gate measurements over what nyc-vision actually wrote. No estimates, no fill.

* `frame_failure_rate` - the <2 % gate, straight off `camera_frame_fetches`.
* `cameras_covered`    - the >=50 cameras / 24 h continuity gate, off `density_samples`.
* `hourly_rush`        - per-hour person and vehicle means, the input to the chart.
* `camera_history`     - per-frame person and vehicle counts for one camera, the input
  to a per-camera trend line.

Every function returns exactly what the tables hold in the window; an empty window
returns zero counts and `None` timestamps, never a fabricated series.

TIMESTAMPS
----------
DuckDB can only hand a `TIMESTAMPTZ` back to Python if `pytz` is installed, and it is
not a dependency of this repo. So every query selects `epoch_ms(ts)` and this module
converts. Local-time bucketing (rush hour is a local phenomenon) is done in Python
with `zoneinfo`, which also keeps the SQL free of the ICU extension.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nyc_live.contracts import VEHICLE_CLASSES, DetectionClass
from nyc_live.store import Store

log = logging.getLogger(__name__)

DEFAULT_TZ = "America/New_York"
GATE_MAX_FAILURE_RATE = 0.02
GATE_MIN_CAMERAS = 50
GATE_MIN_HOURS = 24.0

_PERSON = DetectionClass.PERSON.value
_VEHICLES_SQL = ", ".join(f"'{c.value}'" for c in sorted(VEHICLE_CLASSES))

_FAILURE_SQL = """
SELECT count(*),
       count(*) FILTER (WHERE NOT ok),
       count(DISTINCT camera_id),
       min(epoch_ms(ts)),
       max(epoch_ms(ts))
FROM camera_frame_fetches
WHERE ts >= ? AND ts < ?
"""

_TOP_ERRORS_SQL = """
SELECT coalesce(error, 'unknown'), count(*)
FROM camera_frame_fetches
WHERE ts >= ? AND ts < ? AND NOT ok
GROUP BY 1 ORDER BY 2 DESC LIMIT ?
"""

_COVERAGE_SQL = """
SELECT count(DISTINCT camera_id), count(*), sum(n), min(t), max(t)
FROM (
    SELECT camera_id, epoch_ms(ts) AS t, count(*) AS n
    FROM density_samples
    WHERE ts >= ? AND ts < ?
    GROUP BY camera_id, ts
)
"""

_FRAMES_SQL = f"""
SELECT camera_id,
       epoch_ms(ts),
       SUM(CASE WHEN class = '{_PERSON}' THEN count ELSE 0 END),
       SUM(CASE WHEN class IN ({_VEHICLES_SQL}) THEN count ELSE 0 END)
FROM density_samples
WHERE ts >= ? AND ts < ?
GROUP BY camera_id, ts
"""

_CAMERA_HISTORY_SQL = f"""
SELECT epoch_ms(ts),
       SUM(CASE WHEN class = '{_PERSON}' THEN count ELSE 0 END),
       SUM(CASE WHEN class IN ({_VEHICLES_SQL}) THEN count ELSE 0 END)
FROM density_samples
WHERE camera_id = ? AND ts >= ? AND ts < ?
GROUP BY ts
ORDER BY ts
"""


def _dt(value: Any) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(int(value) / 1000, tz=UTC)


# ---------------------------------------------------------------------------
# Frame-fetch failure rate (the <2 % gate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureRate:
    window_start: datetime
    window_end: datetime
    attempts: int
    failures: int
    cameras: int
    first_ts: datetime | None
    last_ts: datetime | None
    top_errors: tuple[tuple[str, int], ...] = ()

    @property
    def rate(self) -> float:
        """Failures / attempts in [0, 1]. Zero attempts is 0.0 and `passes` is False."""
        return self.failures / self.attempts if self.attempts else 0.0

    @property
    def rate_pct(self) -> float:
        return round(self.rate * 100, 4)

    @property
    def passes(self) -> bool:
        return self.attempts > 0 and self.rate < GATE_MAX_FAILURE_RATE

    def describe(self) -> str:
        if not self.attempts:
            return (
                "frame-fetch failure rate: no camera_frame_fetches rows in the window "
                f"({self.window_start:%Y-%m-%d %H:%MZ} -> {self.window_end:%Y-%m-%d %H:%MZ})"
            )
        return (
            f"frame-fetch failure rate: {self.rate_pct:.3f}% "
            f"({self.failures}/{self.attempts} attempts over {self.cameras} cameras) "
            f"[gate: <{GATE_MAX_FAILURE_RATE * 100:.0f}% -> {'PASS' if self.passes else 'FAIL'}]"
        )


def frame_failure_rate(
    store: Store,
    since: datetime,
    *,
    until: datetime | None = None,
    top_errors: int = 5,
) -> FailureRate:
    """The gate metric, measured from `camera_frame_fetches` and nothing else."""
    end = until or datetime.now(UTC)
    row = store.execute(_FAILURE_SQL, [since, end])
    attempts, failures, cameras, first_ms, last_ms = row[0] if row else (0, 0, 0, None, None)
    errors: tuple[tuple[str, int], ...] = ()
    if failures:
        errors = tuple(
            (str(e), int(n)) for e, n in store.execute(_TOP_ERRORS_SQL, [since, end, top_errors])
        )
    return FailureRate(
        window_start=since,
        window_end=end,
        attempts=int(attempts or 0),
        failures=int(failures or 0),
        cameras=int(cameras or 0),
        first_ts=_dt(first_ms),
        last_ts=_dt(last_ms),
        top_errors=errors,
    )


# ---------------------------------------------------------------------------
# Camera coverage / continuity (the 24 h over >=50 cameras gate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Coverage:
    window_start: datetime
    window_end: datetime
    cameras: int
    frames: int
    samples: int
    first_ts: datetime | None
    last_ts: datetime | None

    @property
    def span_hours(self) -> float:
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        return round((self.last_ts - self.first_ts).total_seconds() / 3600, 3)

    @property
    def passes(self) -> bool:
        return self.cameras >= GATE_MIN_CAMERAS and self.span_hours >= GATE_MIN_HOURS

    def describe(self) -> str:
        if not self.frames:
            return (
                "coverage: no density_samples rows in the window "
                f"({self.window_start:%Y-%m-%d %H:%MZ} -> {self.window_end:%Y-%m-%d %H:%MZ})"
            )
        return (
            f"coverage: {self.cameras} cameras, {self.frames} frames, {self.samples} samples "
            f"spanning {self.span_hours:.2f} h "
            f"({self.first_ts:%Y-%m-%d %H:%MZ} -> {self.last_ts:%Y-%m-%d %H:%MZ}) "
            f"[gate: >={GATE_MIN_CAMERAS} cameras and >={GATE_MIN_HOURS:.0f} h -> "
            f"{'PASS' if self.passes else 'FAIL'}]"
        )


def cameras_covered(store: Store, since: datetime, *, until: datetime | None = None) -> Coverage:
    """Distinct cameras, frames (camera_id, ts) and rows written in the window."""
    end = until or datetime.now(UTC)
    row = store.execute(_COVERAGE_SQL, [since, end])
    cameras, frames, samples, first_ms, last_ms = row[0] if row else (0, 0, 0, None, None)
    return Coverage(
        window_start=since,
        window_end=end,
        cameras=int(cameras or 0),
        frames=int(frames or 0),
        samples=int(samples or 0),
        first_ts=_dt(first_ms),
        last_ts=_dt(last_ms),
    )


# ---------------------------------------------------------------------------
# Hourly rush profile (the chart input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HourlyRush:
    hour: int
    person_mean: float
    vehicle_mean: float
    person_max: int
    vehicle_max: int
    frames: int
    cameras: int


def day_bounds_utc(day: date, tz: str = DEFAULT_TZ) -> tuple[datetime, datetime]:
    """The UTC half-open interval covering one local calendar day."""
    zone = ZoneInfo(tz)
    start_local = datetime.combine(day, time.min, tzinfo=zone)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def hourly_rush(store: Store, day: date, *, tz: str = DEFAULT_TZ) -> list[HourlyRush]:
    """Per local hour: mean persons and mean vehicles per frame, across all cameras.

    A "frame" is one (camera_id, ts): the six class rows for one detection. Person and
    vehicle counts are summed inside the frame, then averaged over every frame that
    fell in the hour. Only hours that actually have frames are returned, so a gap in
    the data stays a visible gap rather than a fake zero.
    """
    zone = ZoneInfo(tz)
    start, end = day_bounds_utc(day, tz)
    rows = store.execute(_FRAMES_SQL, [start, end])
    buckets: dict[int, list[tuple[str, int, int]]] = {}
    for camera_id, ts_ms, persons, vehicles in rows:
        local_hour = datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).astimezone(zone).hour
        buckets.setdefault(local_hour, []).append(
            (str(camera_id), int(persons or 0), int(vehicles or 0))
        )
    out: list[HourlyRush] = []
    for hour in sorted(buckets):
        items = buckets[hour]
        persons = [p for _, p, _ in items]
        vehicles = [v for _, _, v in items]
        out.append(
            HourlyRush(
                hour=hour,
                person_mean=round(sum(persons) / len(persons), 4),
                vehicle_mean=round(sum(vehicles) / len(vehicles), 4),
                person_max=max(persons),
                vehicle_max=max(vehicles),
                frames=len(items),
                cameras=len({c for c, _, _ in items}),
            )
        )
    return out


def rush_summary(rows: Sequence[HourlyRush]) -> str:
    """One line naming the AM and PM peak hours, for the gate report."""
    if not rows:
        return "rush profile: no density_samples rows for that day"
    am = [r for r in rows if 5 <= r.hour < 12]
    pm = [r for r in rows if 14 <= r.hour < 21]
    parts = [f"{len(rows)} hours with data"]
    if am:
        peak = max(am, key=lambda r: r.person_mean + r.vehicle_mean)
        parts.append(
            f"AM peak {peak.hour:02d}:00 (person {peak.person_mean:.2f}, "
            f"vehicle {peak.vehicle_mean:.2f})"
        )
    if pm:
        peak = max(pm, key=lambda r: r.person_mean + r.vehicle_mean)
        parts.append(
            f"PM peak {peak.hour:02d}:00 (person {peak.person_mean:.2f}, "
            f"vehicle {peak.vehicle_mean:.2f})"
        )
    return "rush profile: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# Per-camera history (the dashboard trend line)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CameraHistoryPoint:
    """One frame of one camera's history: person and vehicle counts at `ts`."""

    ts: datetime
    person_count: int
    vehicle_count: int


def camera_history(
    store: Store, camera_id: str, since: datetime, until: datetime
) -> list[CameraHistoryPoint]:
    """Per-frame person and vehicle counts for a single camera, oldest first.

    A "frame" is one (camera_id, ts): the class rows for one detection, summed into
    person and vehicle counts exactly as `hourly_rush` does (bicycles excluded, same
    as everywhere else in this module). Meant for a trend line on a single camera,
    e.g. when a user clicks it on the map. An empty window returns an empty list,
    never a fabricated point.
    """
    rows = store.execute(_CAMERA_HISTORY_SQL, [camera_id, since, until])
    return [
        CameraHistoryPoint(
            ts=datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC),
            person_count=int(persons or 0),
            vehicle_count=int(vehicles or 0),
        )
        for ts_ms, persons, vehicles in rows
    ]
