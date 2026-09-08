"""services.nearby: geo + limit filtering over hand-built Envelopes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nyc_live.contracts import (
    Camera,
    CameraSource,
    Envelope,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    GeoQuery,
    ServiceRequest,
)
from nyc_live.services.nearby import geo_query, nearby

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def cam(i: int, lat: float, lon: float, online: bool = True) -> Camera:
    return Camera(
        id=f"cam{i}",
        source=CameraSource.NYC_DOT,
        name=f"Camera {i}",
        is_online=online,
        image_url=f"https://cams.test/{i}",
        lat=lat,
        lon=lon,
    )


def cameras_env(*cams: Camera, status: str = "fresh") -> Envelope[Camera]:
    return Envelope[Camera](
        feed=FeedName.DOT_CAMERAS,
        status=status,  # type: ignore[arg-type]
        fetched_at=T0,
        stale_after=T0 + timedelta(minutes=10),
        records=list(cams),
        total_before_filter=len(cams),
    )


def test_nearby_sorts_by_distance_and_sets_fields() -> None:
    env = cameras_env(cam(1, 40.70, -74.00), cam(2, 40.7005, -74.00), cam(3, 40.80, -73.90))
    out = nearby(env, GeoQuery(lat=40.7, lon=-74.0, radius_m=1000))
    assert [c.id for c in out.records] == ["cam1", "cam2"]
    assert out.records[0].distance_m == 0.0
    assert out.records[1].distance_m == pytest.approx(55.6, abs=0.5)
    assert out.query is not None and out.query.radius_m == 1000
    assert out.total_before_filter == 3
    assert out.truncated is False
    assert out.status == "fresh" and out.fetched_at == T0


def test_nearby_limit_marks_truncated() -> None:
    env = cameras_env(*(cam(i, 40.70 + i * 0.0001, -74.0) for i in range(5)))
    out = nearby(env, GeoQuery(lat=40.7, lon=-74.0, radius_m=5000), limit=2)
    assert [c.id for c in out.records] == ["cam0", "cam1"]
    assert out.truncated is True
    assert out.total_before_filter == 5


def test_nearby_without_query_only_applies_limit() -> None:
    env = cameras_env(cam(1, 40.70, -74.00), cam(2, 40.71, -74.00))
    out = nearby(env, None, limit=1)
    assert [c.id for c in out.records] == ["cam1"]
    assert out.query is None
    assert out.truncated is True
    assert out.records[0].distance_m is None
    out = nearby(env, None)
    assert len(out.records) == 2 and out.truncated is False


def test_nearby_drops_records_without_coordinates() -> None:
    env = Envelope[ServiceRequest](
        feed=FeedName.NYC_311,
        status="fresh",
        fetched_at=T0,
        stale_after=T0,
        records=[
            ServiceRequest(
                unique_key="a",
                created_at=T0,
                closed_at=None,
                agency="DOT",
                complaint_type="x",
                descriptor=None,
                status=None,
                borough=None,
            ),
            ServiceRequest(
                unique_key="b",
                created_at=T0,
                closed_at=None,
                agency="DOT",
                complaint_type="x",
                descriptor=None,
                status=None,
                borough=None,
                lat=40.7,
                lon=-74.0,
            ),
        ],
    )
    out = nearby(env, GeoQuery(lat=40.7, lon=-74.0))
    assert [r.unique_key for r in out.records] == ["b"]
    assert out.total_before_filter == 2


def test_nearby_passes_error_envelope_through_untouched() -> None:
    err = FeedUnavailable(FeedName.DOT_CAMERAS, "403", kind=ErrorKind.UPSTREAM_HTTP).to_model()
    env = Envelope[Camera](
        feed=FeedName.DOT_CAMERAS,
        status="error",
        fetched_at=None,
        stale_after=None,
        records=[],
        error=err,
    )
    out = nearby(env, GeoQuery(lat=40.7, lon=-74.0), limit=5)
    assert out is env
    assert out.status == "error" and out.records == [] and out.error == err


def test_nearby_keeps_stale_status_and_error() -> None:
    err = FeedUnavailable(FeedName.DOT_CAMERAS, "timeout", kind=ErrorKind.UPSTREAM_TIMEOUT)
    env = cameras_env(cam(1, 40.70, -74.00), status="stale").model_copy(
        update={"error": err.to_model()}
    )
    out = nearby(env, GeoQuery(lat=40.7, lon=-74.0))
    assert out.status == "stale"
    assert out.error is not None and out.error.kind == ErrorKind.UPSTREAM_TIMEOUT
    assert len(out.records) == 1


def test_geo_query_builder() -> None:
    assert geo_query(None, None) is None
    q = geo_query(40.7, -74.0)
    assert q == GeoQuery(lat=40.7, lon=-74.0)
    assert geo_query(40.7, -74.0, 250).radius_m == 250  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="lat and lon"):
        geo_query(40.7, None)
    with pytest.raises(ValueError, match="lat and lon"):
        geo_query(None, -74.0)
