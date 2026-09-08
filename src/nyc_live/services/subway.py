"""Subway views derived from the trips + stops feeds. Pure functions over Envelopes.

`subway_arrivals` joins GTFS-RT stop_time_updates to static stops. Direction
suffixes are kept: stop `127N` (Times Sq, northbound) and `127S` are distinct
records with distinct arrivals. A `stop_id` argument may be a parent station
(`127`) or a platform (`127N`); parent ids match every platform underneath.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from nyc_live.contracts import (
    DEFAULT_TTL,
    Envelope,
    ErrorKind,
    FeedError,
    FeedName,
    GeoQuery,
    SubwayAlert,
    SubwayArrival,
    SubwayStop,
    SubwayTrip,
    now_utc,
)
from nyc_live.geo import filter_nearby

log = logging.getLogger(__name__)

DEFAULT_HORIZON = timedelta(minutes=30)


def _error_from(
    feed: FeedName, source: Envelope[SubwayTrip] | Envelope[SubwayStop]
) -> Envelope[SubwayArrival]:
    err = source.error or FeedError(
        kind=ErrorKind.INTERNAL,
        message=f"{source.feed.value} envelope has status=error without an error attached",
        feed=source.feed,
        occurred_at=now_utc(),
    )
    return Envelope[SubwayArrival](
        feed=feed, status="error", fetched_at=None, stale_after=None, records=[], error=err
    )


def _stop_index(stops: Iterable[SubwayStop]) -> dict[str, SubwayStop]:
    return {s.stop_id: s for s in stops}


def _platform_ids(stop_id: str, index: dict[str, SubwayStop]) -> set[str]:
    """Resolve a stop id (platform or parent station) to the set of platform ids."""
    ids = {s.stop_id for s in index.values() if stop_id in (s.stop_id, s.parent_station)}
    if not ids:
        ids = {stop_id}  # unknown to the static feed; still match trips verbatim
    return ids


def subway_arrivals(
    trips_env: Envelope[SubwayTrip],
    stops_env: Envelope[SubwayStop],
    *,
    query: GeoQuery | None = None,
    stop_id: str | None = None,
    horizon: timedelta = DEFAULT_HORIZON,
    limit: int | None = 50,
    now: datetime | None = None,
) -> Envelope[SubwayArrival]:
    """Upcoming arrivals per platform, soonest first.

    * `stop_id` restricts to one station / platform; `query` restricts to platforms
      within `radius_m`. Both may be combined. With neither, every arrival in the
      horizon is returned (capped by `limit`).
    * If either input envelope is `status="error"` the result is an error envelope
      carrying that error. Never an empty success.
    * Result `status` is the weaker of the two inputs (`stale` if either is stale).
    """
    if trips_env.status == "error":
        return _error_from(FeedName.MTA_SUBWAY, trips_env)
    if stops_env.status == "error":
        return _error_from(FeedName.MTA_SUBWAY, stops_env)

    t0 = now or now_utc()
    cutoff = t0 + horizon
    index = _stop_index(stops_env.records)

    wanted: set[str] | None = None
    if stop_id is not None:
        wanted = _platform_ids(stop_id, index)
    if query is not None:
        near = {s.stop_id: s for s in filter_nearby(stops_env.records, query)}
        wanted = set(near) if wanted is None else wanted & set(near)
    else:
        near = {}

    arrivals: list[SubwayArrival] = []
    total = 0
    for trip in trips_env.records:
        for stu in trip.stop_times:
            when = stu.arrival or stu.departure
            if when is None or when < t0 or when > cutoff:
                continue
            total += 1
            if wanted is not None and stu.stop_id not in wanted:
                continue
            stop = index.get(stu.stop_id)
            near_stop = near.get(stu.stop_id)
            arrivals.append(
                SubwayArrival(
                    stop_id=stu.stop_id,
                    stop_name=stop.name if stop else None,
                    route_id=trip.route_id,
                    trip_id=trip.trip_id,
                    direction=trip.direction,
                    arrival=when,
                    eta_s=round((when - t0).total_seconds(), 1),
                    lat=stop.lat if stop else None,
                    lon=stop.lon if stop else None,
                    distance_m=near_stop.distance_m if near_stop else None,
                )
            )
    arrivals.sort(key=lambda a: (a.arrival, a.stop_id, a.route_id))
    truncated = limit is not None and len(arrivals) > limit
    if truncated and limit is not None:
        arrivals = arrivals[:limit]

    status = "stale" if "stale" in (trips_env.status, stops_env.status) else "fresh"
    error = (trips_env.error or stops_env.error) if status == "stale" else None
    return Envelope[SubwayArrival](
        feed=FeedName.MTA_SUBWAY,
        status=status,
        fetched_at=trips_env.fetched_at,
        stale_after=trips_env.stale_after,
        records=arrivals,
        error=error,
        query=query,
        total_before_filter=total,
        truncated=truncated,
    )


def alerts_near(
    alerts_env: Envelope[SubwayAlert],
    stops_env: Envelope[SubwayStop],
    query: GeoQuery,
    *,
    limit: int | None = None,
) -> Envelope[SubwayAlert]:
    """Alerts that touch a route or stop served within `query.radius_m`.

    Alerts have no coordinates; the join goes through the static stops feed. If the
    stops feed is down the geo filter cannot be applied, so the alerts envelope is
    returned unfiltered with `query` left unset to signal that no filtering happened.
    """
    if alerts_env.status == "error":
        return alerts_env
    if stops_env.status == "error":
        log.warning("alerts_near: stops feed unavailable; returning unfiltered alerts")
        return alerts_env
    near = filter_nearby(stops_env.records, query)
    stop_ids = {s.stop_id for s in near} | {s.parent_station for s in near if s.parent_station}
    routes = {r for s in near for r in s.routes}
    kept = [
        a for a in alerts_env.records if (set(a.routes) & routes) or (set(a.stop_ids) & stop_ids)
    ]
    truncated = limit is not None and len(kept) > limit
    if truncated and limit is not None:
        kept = kept[:limit]
    return alerts_env.model_copy(
        update={
            "records": kept,
            "query": query,
            "total_before_filter": len(alerts_env.records),
            "truncated": truncated,
        }
    )


def arrivals_ttl() -> timedelta:
    return DEFAULT_TTL[FeedName.MTA_SUBWAY]
