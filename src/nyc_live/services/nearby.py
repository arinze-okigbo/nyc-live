"""Geo / limit filtering over whole-feed Envelopes. Pure functions, no fetching."""

from __future__ import annotations

from pydantic import BaseModel

from nyc_live.contracts import Envelope, GeoQuery
from nyc_live.geo import filter_nearby


def nearby[RecordT: BaseModel](
    envelope: Envelope[RecordT], query: GeoQuery | None, *, limit: int | None = None
) -> Envelope[RecordT]:
    """Filter an envelope's records to `query.radius_m` (nearest first) and cap at `limit`.

    Sets `query`, `total_before_filter`, `truncated`, and `distance_m` on every kept
    record. Records without coordinates are dropped when a query is applied. Error
    envelopes pass through untouched (they already have `records == []`); stale ones are
    filtered like fresh ones so the caller still sees "last good" data.
    """
    if envelope.status == "error":
        return envelope
    total = envelope.total_before_filter
    if total is None:
        total = len(envelope.records)
    if query is None:
        kept = list(envelope.records)
        truncated = False
        if limit is not None and len(kept) > limit:
            kept = kept[:limit]
            truncated = True
        return envelope.model_copy(
            update={"records": kept, "total_before_filter": total, "truncated": truncated}
        )
    within = filter_nearby(envelope.records, query)
    truncated = limit is not None and len(within) > limit
    if truncated and limit is not None:
        within = within[:limit]
    return envelope.model_copy(
        update={
            "records": within,
            "query": query,
            "total_before_filter": total,
            "truncated": truncated,
        }
    )


def geo_query(
    lat: float | None, lon: float | None, radius_m: float | None = None
) -> GeoQuery | None:
    """Build a GeoQuery from optional tool arguments; None when no point was given.

    Raises ValueError when only one of lat/lon is supplied (an ambiguous request).
    """
    if lat is None and lon is None:
        return None
    if lat is None or lon is None:
        raise ValueError("lat and lon must be given together")
    if radius_m is None:
        return GeoQuery(lat=lat, lon=lon)
    return GeoQuery(lat=lat, lon=lon, radius_m=radius_m)
