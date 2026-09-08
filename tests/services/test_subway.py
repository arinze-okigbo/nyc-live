"""services.subway: arrivals join and alert geo filter over hand-built Envelopes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nyc_live.contracts import (
    Envelope,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    GeoQuery,
    StopTimeUpdate,
    SubwayAlert,
    SubwayStop,
    SubwayTrip,
)
from nyc_live.services.subway import alerts_near, subway_arrivals

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
TIMES_SQ = (40.75529, -73.987495)
SOUTH_FERRY = (40.702068, -74.013664)


def stops_env(status: str = "fresh") -> Envelope[SubwayStop]:
    recs = [
        SubwayStop(stop_id="127", name="Times Sq-42 St", lat=TIMES_SQ[0], lon=TIMES_SQ[1], routes=["1", "2", "3"]),
        SubwayStop(stop_id="127N", name="Times Sq-42 St", lat=TIMES_SQ[0], lon=TIMES_SQ[1], parent_station="127", routes=["1", "2", "3"]),
        SubwayStop(stop_id="127S", name="Times Sq-42 St", lat=TIMES_SQ[0], lon=TIMES_SQ[1], parent_station="127", routes=["1", "2", "3"]),
        SubwayStop(stop_id="142N", name="South Ferry", lat=SOUTH_FERRY[0], lon=SOUTH_FERRY[1], parent_station="142", routes=["1"]),
    ]  # fmt: skip
    return Envelope[SubwayStop](
        feed=FeedName.MTA_SUBWAY_STOPS,
        status=status,  # type: ignore[arg-type]
        fetched_at=T0 - timedelta(hours=1),
        stale_after=T0 + timedelta(hours=23),
        records=recs,
    )


def trips_env(status: str = "fresh") -> Envelope[SubwayTrip]:
    recs = [
        SubwayTrip(
            trip_id="t1",
            route_id="1",
            feed_slug="gtfs",
            direction="N",
            stop_times=[
                StopTimeUpdate(stop_id="142N", arrival=T0 + timedelta(minutes=2)),
                StopTimeUpdate(stop_id="127N", arrival=T0 + timedelta(minutes=9)),
            ],
        ),
        SubwayTrip(
            trip_id="t2",
            route_id="2",
            feed_slug="gtfs",
            direction="S",
            stop_times=[
                StopTimeUpdate(stop_id="127S", arrival=None, departure=T0 + timedelta(minutes=4)),
                StopTimeUpdate(stop_id="127S", arrival=T0 - timedelta(minutes=1)),
                StopTimeUpdate(stop_id="999X", arrival=T0 + timedelta(minutes=5)),
            ],
        ),
        SubwayTrip(
            trip_id="t3",
            route_id="3",
            feed_slug="gtfs",
            direction=None,
            stop_times=[StopTimeUpdate(stop_id="127N", arrival=T0 + timedelta(hours=2))],
        ),
    ]
    return Envelope[SubwayTrip](
        feed=FeedName.MTA_SUBWAY,
        status=status,  # type: ignore[arg-type]
        fetched_at=T0,
        stale_after=T0 + timedelta(seconds=30),
        records=recs,
    )


def error_env(feed: FeedName) -> Envelope:
    return Envelope(
        feed=feed,
        status="error",
        fetched_at=None,
        stale_after=None,
        records=[],
        error=FeedUnavailable(feed, "blocked", kind=ErrorKind.UPSTREAM_HTTP).to_model(),
    )


def test_all_arrivals_sorted_soonest_first_within_horizon() -> None:
    env = subway_arrivals(trips_env(), stops_env(), now=T0)
    assert env.status == "fresh"
    assert env.feed == FeedName.MTA_SUBWAY
    assert env.fetched_at == T0 and env.stale_after == T0 + timedelta(seconds=30)
    got = [(a.stop_id, a.route_id, a.eta_s) for a in env.records]
    assert got == [
        ("142N", "1", 120.0),
        ("127S", "2", 240.0),
        ("999X", "2", 300.0),
        ("127N", "1", 540.0),
    ]
    assert env.total_before_filter == 4  # past arrival and the 2 h one are excluded
    assert env.records[2].stop_name is None and env.records[2].lat is None  # unknown stop kept
    assert env.records[0].stop_name == "South Ferry"
    assert env.records[0].lat == SOUTH_FERRY[0]


def test_departure_used_when_arrival_missing() -> None:
    env = subway_arrivals(trips_env(), stops_env(), stop_id="127S", now=T0)
    assert [a.trip_id for a in env.records] == ["t2"]
    assert env.records[0].arrival == T0 + timedelta(minutes=4)


def test_parent_stop_matches_every_platform() -> None:
    env = subway_arrivals(trips_env(), stops_env(), stop_id="127", now=T0)
    assert [(a.stop_id, a.direction) for a in env.records] == [("127S", "S"), ("127N", "N")]
    env = subway_arrivals(trips_env(), stops_env(), stop_id="127N", now=T0)
    assert [a.stop_id for a in env.records] == ["127N"]


def test_unknown_stop_id_matches_trips_verbatim() -> None:
    env = subway_arrivals(trips_env(), stops_env(), stop_id="999X", now=T0)
    assert [a.stop_id for a in env.records] == ["999X"]
    assert env.records[0].stop_name is None


def test_geo_query_restricts_to_platforms_in_radius() -> None:
    q = GeoQuery(lat=SOUTH_FERRY[0], lon=SOUTH_FERRY[1], radius_m=300)
    env = subway_arrivals(trips_env(), stops_env(), query=q, now=T0)
    assert [a.stop_id for a in env.records] == ["142N"]
    assert env.records[0].distance_m == 0.0
    assert env.query == q


def test_stop_id_and_query_intersect() -> None:
    q = GeoQuery(lat=SOUTH_FERRY[0], lon=SOUTH_FERRY[1], radius_m=300)
    env = subway_arrivals(trips_env(), stops_env(), query=q, stop_id="127", now=T0)
    assert env.records == []
    assert env.status == "fresh"  # feeds were fine; the filter simply matched nothing


def test_horizon_and_limit() -> None:
    env = subway_arrivals(trips_env(), stops_env(), horizon=timedelta(hours=3), limit=2, now=T0)
    assert len(env.records) == 2 and env.truncated is True
    assert env.total_before_filter == 5
    env = subway_arrivals(trips_env(), stops_env(), horizon=timedelta(minutes=3), now=T0)
    assert [a.stop_id for a in env.records] == ["142N"]


def test_error_trips_propagates_error_never_empty_success() -> None:
    env = subway_arrivals(error_env(FeedName.MTA_SUBWAY), stops_env(), stop_id="127", now=T0)
    assert env.status == "error" and env.records == []
    assert env.error is not None and env.error.feed == FeedName.MTA_SUBWAY
    assert env.error.kind == ErrorKind.UPSTREAM_HTTP


def test_error_stops_propagates_stops_error() -> None:
    env = subway_arrivals(trips_env(), error_env(FeedName.MTA_SUBWAY_STOPS), now=T0)
    assert env.status == "error" and env.records == []
    assert env.error is not None and env.error.feed == FeedName.MTA_SUBWAY_STOPS
    assert env.feed == FeedName.MTA_SUBWAY


def test_stale_input_makes_result_stale_with_error_attached() -> None:
    err = FeedUnavailable(FeedName.MTA_SUBWAY, "timeout", kind=ErrorKind.UPSTREAM_TIMEOUT)
    stale_trips = trips_env(status="stale").model_copy(update={"error": err.to_model()})
    env = subway_arrivals(stale_trips, stops_env(), now=T0)
    assert env.status == "stale"
    assert env.error is not None and env.error.kind == ErrorKind.UPSTREAM_TIMEOUT
    assert len(env.records) == 4


def alerts_env() -> Envelope[SubwayAlert]:
    return Envelope[SubwayAlert](
        feed=FeedName.MTA_SUBWAY_ALERTS,
        status="fresh",
        fetched_at=T0,
        stale_after=T0 + timedelta(minutes=1),
        records=[
            SubwayAlert(id="a1", header="1 skips South Ferry", routes=["1"], stop_ids=["142"]),
            SubwayAlert(id="a2", header="G delays", routes=["G"]),
            SubwayAlert(id="a3", header="Elevator out at 127", routes=[], stop_ids=["127"]),
        ],
    )


def test_alerts_near_joins_through_stops() -> None:
    q = GeoQuery(lat=SOUTH_FERRY[0], lon=SOUTH_FERRY[1], radius_m=300)
    env = alerts_near(alerts_env(), stops_env(), q)
    assert [a.id for a in env.records] == ["a1"]  # route 1 serves South Ferry; a3 is at 127
    assert env.query == q and env.total_before_filter == 3
    q = GeoQuery(lat=TIMES_SQ[0], lon=TIMES_SQ[1], radius_m=300)
    env = alerts_near(alerts_env(), stops_env(), q, limit=1)
    assert [a.id for a in env.records] == ["a1"]  # route 1 also serves Times Sq
    assert env.truncated is True


def test_alerts_near_with_stops_down_returns_unfiltered() -> None:
    q = GeoQuery(lat=SOUTH_FERRY[0], lon=SOUTH_FERRY[1], radius_m=300)
    env = alerts_near(alerts_env(), error_env(FeedName.MTA_SUBWAY_STOPS), q)
    assert len(env.records) == 3 and env.query is None


def test_alerts_near_error_alerts_pass_through() -> None:
    q = GeoQuery(lat=SOUTH_FERRY[0], lon=SOUTH_FERRY[1], radius_m=300)
    err = error_env(FeedName.MTA_SUBWAY_ALERTS)
    assert alerts_near(err, stops_env(), q) is err
