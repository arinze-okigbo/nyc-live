"""The /api surface: envelope passthrough, geo/limit arguments, health, static files."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from nyc_dash.api import FEED_KEYS, ROUTE_BY_KEY
from nyc_dash.app import CACHEABLE_TTL
from nyc_live.contracts import DEFAULT_TTL, DensitySample, DetectionClass, FeedName, now_utc
from nyc_live.store import Store
from tests.dash.conftest import BATTERY, TIMES_SQ

ENVELOPE_KEYS = {
    "feed",
    "status",
    "fetched_at",
    "stale_after",
    "records",
    "error",
    "query",
    "total_before_filter",
    "truncated",
}


def test_api_index_lists_every_feed(client: TestClient) -> None:
    body = client.get("/api").json()
    assert [f["key"] for f in body["feeds"]] == list(FEED_KEYS)
    assert body["health"] == "/api/health"
    assert body["stream"] == "/api/stream"
    for entry in body["feeds"]:
        assert entry["path"] == f"/api/{entry['key']}"
        assert entry["description"]


@pytest.mark.parametrize("key", FEED_KEYS)
def test_every_feed_endpoint_returns_a_whole_envelope(client: TestClient, key: str) -> None:
    response = client.get(f"/api/{key}")
    assert response.status_code == 200
    # Cache-Control is derived from the envelope's own freshness window, not blanket
    # no-store: a live feed must never come from cache, but re-downloading 3.45 MB of
    # 24-hour-TTL subway geometry on every page load was 48% of the cold-load payload.
    # A cacheable response may never outlive the freshness it claims, so max-age is
    # bounded by the feed's TTL.
    cache_control = response.headers["cache-control"]
    body = response.json()
    ttl = DEFAULT_TTL[ROUTE_BY_KEY[key].feed]
    if body["status"] != "fresh" or ttl < CACHEABLE_TTL:
        assert cache_control == "no-store", key
    else:
        assert cache_control.startswith("private, max-age="), key
        assert 0 < int(cache_control.rpartition("=")[2]) <= ttl.total_seconds(), key
    assert set(body) == ENVELOPE_KEYS
    assert body["feed"] == ROUTE_BY_KEY[key].feed.value
    assert body["status"] in {"fresh", "stale", "error"}
    assert isinstance(body["records"], list)
    assert isinstance(body["truncated"], bool)
    if body["status"] == "error":
        assert body["records"] == []
        assert body["error"]["message"]
        assert body["error"]["kind"]
    else:
        assert body["fetched_at"] and body["stale_after"]


def test_fresh_envelope_is_passed_through_unchanged(client: TestClient) -> None:
    body = client.get("/api/citibike").json()
    assert body["status"] == "fresh"
    assert body["error"] is None
    assert body["query"] is None
    assert body["total_before_filter"] == 2
    assert body["truncated"] is False
    assert {r["station_id"] for r in body["records"]} == {"s1", "s2"}
    assert body["records"][0]["bikes_available"] == 5
    assert datetime.fromisoformat(body["stale_after"]) > datetime.fromisoformat(body["fetched_at"])


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("311", "nyc_311"),
        ("bikes", "citibike"),
        ("subway", "subway_arrivals"),
        ("cameras", "dot_cameras"),
    ],
)
def test_aliases_resolve_to_the_same_feed(client: TestClient, alias: str, canonical: str) -> None:
    assert (
        client.get(f"/api/{alias}").json()["feed"] == client.get(f"/api/{canonical}").json()["feed"]
    )


def test_unknown_feed_is_404_and_names_the_valid_feeds(client: TestClient) -> None:
    response = client.get("/api/not_a_feed")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "not_a_feed" in detail
    assert "citibike" in detail


# --------------------------------------------------------------------------- geo / limit


def test_geo_query_filters_and_is_echoed(client: TestClient) -> None:
    body = client.get(
        "/api/citibike", params={"lat": BATTERY[0], "lon": BATTERY[1], "radius_m": 300}
    ).json()
    assert body["query"] == {"lat": BATTERY[0], "lon": BATTERY[1], "radius_m": 300.0}
    assert [r["station_id"] for r in body["records"]] == ["s2"]
    assert body["records"][0]["distance_m"] is not None
    assert body["total_before_filter"] == 2
    assert body["truncated"] is False


def test_radius_defaults_per_feed(client: TestClient) -> None:
    bikes = client.get("/api/citibike", params={"lat": TIMES_SQ[0], "lon": TIMES_SQ[1]}).json()
    assert bikes["query"]["radius_m"] == 1000
    weather = client.get("/api/weather", params={"lat": TIMES_SQ[0], "lon": TIMES_SQ[1]}).json()
    assert weather["query"]["radius_m"] == 50_000
    assert len(weather["records"]) == 1


def test_lat_without_lon_is_a_bad_request(client: TestClient) -> None:
    response = client.get("/api/citibike", params={"lat": 40.75})
    assert response.status_code == 400
    assert "lat and lon" in response.json()["detail"]


def test_geo_filter_on_a_feed_without_coordinates_is_rejected(client: TestClient) -> None:
    response = client.get("/api/mta_subway", params={"lat": TIMES_SQ[0], "lon": TIMES_SQ[1]})
    assert response.status_code == 400
    assert "no located records" in response.json()["detail"]
    assert client.get("/api/mta_subway").json()["status"] == "fresh"


def test_limit_truncates_and_sets_the_flag(client: TestClient) -> None:
    body = client.get("/api/citibike", params={"limit": 1}).json()
    assert len(body["records"]) == 1
    assert body["truncated"] is True
    assert body["total_before_filter"] == 2


def test_out_of_range_arguments_are_422(client: TestClient) -> None:
    assert client.get("/api/citibike", params={"limit": 0}).status_code == 422
    assert client.get("/api/citibike", params={"lat": 120, "lon": 0}).status_code == 422
    assert (
        client.get("/api/citibike", params={"lat": 40.7, "lon": -74.0, "radius_m": 0}).status_code
        == 422
    )


# --------------------------------------------------------------------------- derived feeds


def test_subway_arrivals_carry_stop_coordinates(client: TestClient) -> None:
    body = client.get("/api/subway_arrivals").json()
    assert body["status"] == "fresh"
    assert body["feed"] == FeedName.MTA_SUBWAY.value
    assert len(body["records"]) == 3
    etas = [r["eta_s"] for r in body["records"]]
    assert etas == sorted(etas)
    for record in body["records"]:
        assert record["lat"] is not None and record["lon"] is not None
        assert record["stop_name"] in {"Times Sq-42 St", "South Ferry"}


def test_subway_arrivals_stop_id_filter(client: TestClient) -> None:
    body = client.get("/api/subway_arrivals", params={"stop_id": "127"}).json()
    assert {r["stop_id"] for r in body["records"]} == {"127N", "127S"}
    assert body["total_before_filter"] == 3


def test_subway_arrivals_geo_filter(client: TestClient) -> None:
    body = client.get(
        "/api/subway_arrivals", params={"lat": 40.702068, "lon": -74.013664, "radius_m": 200}
    ).json()
    assert {r["stop_id"] for r in body["records"]} == {"142N"}
    assert body["records"][0]["distance_m"] is not None


def test_subway_alerts_geo_join_uses_the_stop_feed(client: TestClient) -> None:
    body = client.get(
        "/api/subway_alerts", params={"lat": 40.702068, "lon": -74.013664, "radius_m": 300}
    ).json()
    assert [r["id"] for r in body["records"]] == ["a1"]
    assert body["total_before_filter"] == 2


def test_311_complaint_type_substring_filter(client: TestClient) -> None:
    body = client.get("/api/311", params={"complaint_type": "noise"}).json()
    assert [r["unique_key"] for r in body["records"]] == ["1"]
    assert body["total_before_filter"] == 2


def test_density_without_samples_is_an_honest_error(client: TestClient) -> None:
    body = client.get("/api/density").json()
    assert body["status"] == "error"
    assert body["records"] == []
    assert body["error"]["kind"] == "not_configured"
    assert "nyc-vision" in body["error"]["message"]


def test_density_with_samples_uses_the_service_layer(client: TestClient, store: Store) -> None:
    t = now_utc() - timedelta(seconds=30)
    store.execute(
        "INSERT INTO cameras VALUES (?, 'nyc_dot', '7 Ave @ 42 St', ?, ?, true, ?, ?)",
        ["cam1", TIMES_SQ[0], TIMES_SQ[1], t, t],
    )
    store.insert_density_samples(
        [
            DensitySample(
                camera_id="cam1", ts=t, cls=DetectionClass.PERSON, count=7, model="yolo11n.pt"
            ),
            DensitySample(
                camera_id="cam1", ts=t, cls=DetectionClass.CAR, count=3, model="yolo11n.pt"
            ),
        ]
    )
    body = client.get("/api/density", params={"window_s": 300}).json()
    assert body["status"] == "fresh"
    assert len(body["records"]) == 1
    record = body["records"][0]
    assert record["camera_id"] == "cam1"
    assert record["person_mean"] == 7.0
    assert record["vehicle_mean"] == 3.0


# --------------------------------------------------------------------------- health / static


def test_health_reports_every_registered_feed(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert set(body) == {"checked_at", "store", "feeds"}
    assert body["store"]["open"] is True
    assert body["store"]["read_only"] is False
    feeds = {f["feed"]: f for f in body["feeds"]}
    assert feeds[FeedName.CITIBIKE.value]["status"] == "never_fetched"
    assert feeds[FeedName.MTA_BUS.value]["configured"] is False
    assert feeds[FeedName.CITIBIKE.value]["ttl_s"] == 60.0


def test_health_tracks_what_the_api_already_fetched(client: TestClient) -> None:
    client.get("/api/citibike")
    feeds = {f["feed"]: f for f in client.get("/api/health").json()["feeds"]}
    assert feeds[FeedName.CITIBIKE.value]["status"] == "fresh"
    assert feeds[FeedName.CITIBIKE.value]["record_count"] == 2
    assert feeds[FeedName.CITIBIKE.value]["last_ok_at"] is not None


def test_index_and_assets_are_served(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "nyc-live" in index.text
    assert client.get("/js/app.js").status_code == 200
    assert client.get("/css/tokens.css").status_code == 200
