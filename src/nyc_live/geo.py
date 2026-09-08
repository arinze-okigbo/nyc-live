"""Small geo helpers shared by every layer. Pure functions, no I/O."""

from __future__ import annotations

import math
from collections.abc import Iterable

from pydantic import BaseModel

from nyc_live.contracts import NYC_BBOX, GeoQuery

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def in_nyc_bbox(lat: float, lon: float, pad_deg: float = 0.05) -> bool:
    min_lat, min_lon, max_lat, max_lon = NYC_BBOX
    return (min_lat - pad_deg) <= lat <= (max_lat + pad_deg) and (min_lon - pad_deg) <= lon <= (
        max_lon + pad_deg
    )


def filter_nearby[T: BaseModel](
    records: Iterable[T], query: GeoQuery, *, limit: int | None = None
) -> list[T]:
    """Return records within `query.radius_m`, nearest first, with `distance_m` set.

    Records without lat/lon are dropped. Works on any model exposing `lat`, `lon`,
    and a `distance_m` field (Located / MaybeLocated).
    """
    scored: list[tuple[float, T]] = []
    for rec in records:
        lat = getattr(rec, "lat", None)
        lon = getattr(rec, "lon", None)
        if lat is None or lon is None:
            continue
        d = haversine_m(query.lat, query.lon, lat, lon)
        if d <= query.radius_m:
            scored.append((d, rec))
    scored.sort(key=lambda t: t[0])
    if limit is not None:
        scored = scored[:limit]
    return [rec.model_copy(update={"distance_m": round(d, 1)}) for d, rec in scored]
