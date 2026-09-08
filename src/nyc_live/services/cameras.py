"""Camera helpers: frame fetch as an Envelope, telemetry persistence, nearest-camera pick."""

from __future__ import annotations

import logging

from nyc_live.contracts import (
    Camera,
    CameraFrame,
    Envelope,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    FrameSource,
    GeoQuery,
    now_utc,
)
from nyc_live.geo import filter_nearby
from nyc_live.store import Store

log = logging.getLogger(__name__)


def persist_frame_telemetry(frames: FrameSource, store: Store | None) -> int:
    """Flush the frame source's CameraFrameFetch rows to a writable store. Returns rows written."""
    drain = getattr(frames, "drain_telemetry", None)
    if drain is None:
        return 0
    if store is None or store.read_only:
        # keep the in-memory buffer bounded even when nobody persists it
        drain()
        return 0
    try:
        return store.record_frame_fetches(drain())
    except Exception:
        log.exception("failed to persist camera_frame_fetches rows")
        return 0


async def camera_frame(
    frames: FrameSource, camera_id: str, *, store: Store | None = None
) -> Envelope[CameraFrame]:
    """Fetch one JPEG (2 s per-camera cadence enforced by the source) as an Envelope."""
    try:
        frame = await frames.get_frame(camera_id)
    except FeedUnavailable as exc:
        persist_frame_telemetry(frames, store)
        return Envelope[CameraFrame](
            feed=FeedName.DOT_CAMERA_FRAMES,
            status="error",
            fetched_at=None,
            stale_after=None,
            records=[],
            error=exc.to_model(),
        )
    except Exception as exc:
        log.exception("frame fetch crashed for camera %s", camera_id)
        return Envelope[CameraFrame](
            feed=FeedName.DOT_CAMERA_FRAMES,
            status="error",
            fetched_at=None,
            stale_after=None,
            records=[],
            error=FeedUnavailable(
                FeedName.DOT_CAMERA_FRAMES,
                f"{type(exc).__name__}: {exc}",
                kind=ErrorKind.INTERNAL,
            ).to_model(),
        )
    persist_frame_telemetry(frames, store)
    status = "fresh" if now_utc() < frame.stale_after else "stale"
    return Envelope[CameraFrame](
        feed=FeedName.DOT_CAMERA_FRAMES,
        status=status,
        fetched_at=frame.fetched_at,
        stale_after=frame.stale_after,
        records=[frame],
        total_before_filter=1,
    )


def nearest_camera(cameras_env: Envelope[Camera], query: GeoQuery) -> Camera | None:
    """The closest online camera within the query radius, or None."""
    for cam in filter_nearby(cameras_env.records, query):
        if cam.is_online:
            return cam
    return None
