from __future__ import annotations

from nyc_live.contracts import NYC_BBOX
from nyc_vision.sampling import bucket_cameras, grid_cell, select_cameras
from tests.vision.conftest import BOROUGH_POINTS, borough_cameras, make_camera


def test_grid_cell_is_inside_the_grid_and_clamped() -> None:
    min_lat, min_lon, max_lat, max_lon = NYC_BBOX
    assert grid_cell(min_lat, min_lon, 5) == (0, 0)
    assert grid_cell(max_lat, max_lon, 5) == (4, 4)
    assert grid_cell(90.0, 0.0, 5) == (4, 4)  # clamped, never out of range
    assert grid_cell(-90.0, -180.0, 5) == (0, 0)


def test_each_borough_lands_in_a_distinct_cell() -> None:
    cells = {name: grid_cell(lat, lon, 5) for name, (lat, lon) in BOROUGH_POINTS.items()}
    assert len(set(cells.values())) == len(BOROUGH_POINTS), cells


def test_selection_is_stratified_across_boroughs() -> None:
    cams = borough_cameras(per_borough=20)  # 100 cameras, 5 clusters
    chosen, stats = select_cameras(cams, 60, grid=5)

    assert stats.eligible == 100
    assert len(chosen) == 60
    per_cluster: dict[str, int] = {}
    for cam in chosen:
        per_cluster[cam.id.split("-")[0]] = per_cluster.get(cam.id.split("-")[0], 0) + 1
    assert set(per_cluster) == set(BOROUGH_POINTS)
    # round-robin: every borough gets the same share when clusters are equal size
    assert set(per_cluster.values()) == {12}


def test_selection_is_deterministic_and_order_independent() -> None:
    cams = borough_cameras(per_borough=13)
    first, _ = select_cameras(cams, 30, grid=5)
    second, _ = select_cameras(cams, 30, grid=5)
    shuffled, _ = select_cameras(list(reversed(cams)), 30, grid=5)
    assert [c.id for c in first] == [c.id for c in second]
    assert [c.id for c in first] == [c.id for c in shuffled]


def test_sparse_cell_is_not_crowded_out_by_a_dense_one() -> None:
    dense_lat, dense_lon = BOROUGH_POINTS["manhattan"]
    sparse_lat, sparse_lon = BOROUGH_POINTS["staten_island"]
    cams = [make_camera(f"dense-{i:03d}", dense_lat, dense_lon) for i in range(50)]
    cams += [make_camera(f"sparse-{i:03d}", sparse_lat, sparse_lon) for i in range(2)]
    chosen, _ = select_cameras(cams, 10, grid=5)
    assert sum(1 for c in chosen if c.id.startswith("sparse")) == 2


def test_offline_and_out_of_bbox_cameras_are_excluded_and_counted() -> None:
    lat, lon = BOROUGH_POINTS["brooklyn"]
    cams = [
        make_camera("on-1", lat, lon),
        make_camera("off-1", lat, lon, is_online=False),
        make_camera("boston-1", 42.3601, -71.0589),
    ]
    chosen, stats = select_cameras(cams, 10, grid=5)
    assert [c.id for c in chosen] == ["on-1"]
    assert stats.offline == 1
    assert stats.outside_bbox == 1
    assert stats.eligible == 1
    assert stats.selected == 1
    assert "offline=1" in stats.describe()


def test_asking_for_more_than_exists_returns_everything_without_padding() -> None:
    cams = borough_cameras(per_borough=2)  # 10 cameras
    chosen, stats = select_cameras(cams, 60, grid=5)
    assert len(chosen) == 10
    assert stats.selected == 10
    assert len({c.id for c in chosen}) == 10


def test_bucket_cameras_sorts_within_a_cell() -> None:
    lat, lon = BOROUGH_POINTS["queens"]
    cams = [make_camera(i, lat, lon) for i in ("c", "a", "b")]
    buckets = bucket_cameras(cams, grid=5)
    (only_cell,) = buckets.values()
    assert [c.id for c in only_cell] == ["a", "b", "c"]
