"""Choose which cameras to sample, stratified across the city by lat/lon.

Algorithm (deterministic, no RNG, no borough lookup table)
----------------------------------------------------------
1. Keep cameras that are online and inside the NYC bbox. Drop the rest, counted.
2. Lay a `grid` x `grid` mesh over `contracts.NYC_BBOX` and put each camera in the
   cell its (lat, lon) falls in. With the default `grid=5` that is 25 cells of
   roughly 10 x 12 km, which separates Staten Island, Brooklyn/Queens, Manhattan
   and the Bronx without hardcoding borough polygons we would have to maintain.
3. Sort cells by (row, col) and cameras inside each cell by id; then round-robin
   across the non-empty cells, taking one camera per cell per pass, until `n` are
   chosen or every camera is used.

Round-robin over sorted cells means the same camera list always yields the same
selection, and a dense area (Midtown) cannot crowd out a sparse one (Staten
Island) until the sparse cells are exhausted. Sorting by camera id, not by list
order, keeps the selection stable when the DOT list is reordered upstream.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from nyc_live.contracts import NYC_BBOX, Camera
from nyc_live.geo import in_nyc_bbox

log = logging.getLogger(__name__)

DEFAULT_GRID = 5


@dataclass(frozen=True, slots=True)
class SelectionStats:
    total: int
    offline: int
    outside_bbox: int
    eligible: int
    cells_used: int
    selected: int

    def describe(self) -> str:
        return (
            f"selected {self.selected} of {self.eligible} eligible cameras "
            f"across {self.cells_used} grid cells "
            f"(total={self.total}, offline={self.offline}, outside_bbox={self.outside_bbox})"
        )


def grid_cell(lat: float, lon: float, grid: int) -> tuple[int, int]:
    """(row, col) of a coordinate in a `grid` x `grid` mesh over NYC_BBOX, clamped."""
    min_lat, min_lon, max_lat, max_lon = NYC_BBOX
    lat_span = max_lat - min_lat
    lon_span = max_lon - min_lon
    row = int((lat - min_lat) / lat_span * grid) if lat_span > 0 else 0
    col = int((lon - min_lon) / lon_span * grid) if lon_span > 0 else 0
    return (min(max(row, 0), grid - 1), min(max(col, 0), grid - 1))


def bucket_cameras(
    cameras: Iterable[Camera], *, grid: int = DEFAULT_GRID
) -> dict[tuple[int, int], list[Camera]]:
    """Group eligible cameras by grid cell, each cell sorted by camera id."""
    buckets: dict[tuple[int, int], list[Camera]] = {}
    for cam in cameras:
        buckets.setdefault(grid_cell(cam.lat, cam.lon, grid), []).append(cam)
    for bucket in buckets.values():
        bucket.sort(key=lambda c: c.id)
    return buckets


def select_cameras(
    cameras: Sequence[Camera],
    n: int,
    *,
    grid: int = DEFAULT_GRID,
    require_online: bool = True,
) -> tuple[list[Camera], SelectionStats]:
    """Pick up to `n` cameras, round-robin across grid cells. Deterministic.

    Returns fewer than `n` only when fewer eligible cameras exist; that is a real
    shortage of online cameras, reported through `SelectionStats`, never padded.
    """
    offline = 0
    outside = 0
    eligible: list[Camera] = []
    for cam in cameras:
        if require_online and not cam.is_online:
            offline += 1
            continue
        if not in_nyc_bbox(cam.lat, cam.lon):
            outside += 1
            continue
        eligible.append(cam)

    buckets = bucket_cameras(eligible, grid=grid)
    order = sorted(buckets)
    chosen: list[Camera] = []
    depth = 0
    deepest = max((len(b) for b in buckets.values()), default=0)
    while len(chosen) < n and depth < deepest:
        for cell in order:
            bucket = buckets[cell]
            if depth < len(bucket):
                chosen.append(bucket[depth])
                if len(chosen) >= n:
                    break
        depth += 1

    stats = SelectionStats(
        total=len(cameras),
        offline=offline,
        outside_bbox=outside,
        eligible=len(eligible),
        cells_used=len({grid_cell(c.lat, c.lon, grid) for c in chosen}),
        selected=len(chosen),
    )
    if stats.selected < n:
        log.warning(
            "requested %d cameras but only %d are eligible; %s", n, stats.selected, stats.describe()
        )
    return chosen, stats
