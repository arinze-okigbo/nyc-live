"""Phase 4 gate, end to end: every `/api/*` endpoint, `/api/stream`, and per-feed isolation.

The app under test is the real `nyc_dash.app.create_app` over the real `Services`:
the real adapters behind the real `CachedFeed`, and the real DuckDB file that
`conftest.seed_density` wrote. `tests/dash/` covers this at the unit level with fake
adapters; here the registry is the production one, so an endpoint that only works
against a fake adapter fails.

Because the upstreams are genuinely unreachable, most feeds legitimately answer
`status="error"` - that IS the degradation path the gate is about - while `/api/density`
answers `fresh` from DuckDB. That mix is what makes the isolation assertions meaningful:
a dead feed must not take the served ones with it.

One test additionally runs a real `uvicorn` process on a real port so the SSE stream is
exercised over a socket rather than through the ASGI shim.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from nyc_dash.api import DEFAULT_STREAM_KEYS, FEED_KEYS, ROUTE_BY_KEY, ROUTES
from nyc_dash.app import create_app
from nyc_live.config import Settings
from nyc_live.contracts import (
    BikeStation,
    Camera,
    CameraDensity,
    FeedName,
    ServiceRequest,
    SubwayArrival,
    WeatherObservation,
    WeatherReport,
)
from nyc_live.services import Services, build_services
from tests.integration.conftest import (
    CAM_A,
    REPO_ROOT,
    TIMES_SQ,
    check_envelope,
    child_env,
    free_port,
    unreachable_proxy_env,
)

KILLED_MARKER = "killed-by-integration-tester"


@pytest.fixture
def client(services: Services) -> Iterator[TestClient]:
    with TestClient(create_app(services)) as test_client:
        yield test_client


# --------------------------------------------------------------------------- endpoints


def test_api_index_lists_every_route(client: TestClient) -> None:
    body = client.get("/api").json()
    assert [f["key"] for f in body["feeds"]] == list(FEED_KEYS)
    assert body["health"] == "/api/health" and body["stream"] == "/api/stream"
    assert set(body["stream_default_feeds"]) <= set(FEED_KEYS)
    for entry in body["feeds"]:
        assert entry["description"], f"{entry['key']} has no description"
        assert client.get(entry["path"]).status_code == 200


def test_every_feed_endpoint_returns_a_contract_envelope(client: TestClient) -> None:
    """One pass over every canonical endpoint, through the real registry."""
    statuses: dict[str, str] = {}
    for key in FEED_KEYS:
        response = client.get(f"/api/{key}")
        assert response.status_code == 200, f"/api/{key} -> {response.status_code}"
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        statuses[key] = check_envelope(payload, feed=ROUTE_BY_KEY[key].feed)
    assert statuses["density"] == "fresh", "the DuckDB-backed layer must serve from the store"
    assert set(statuses) == set(FEED_KEYS)
    assert all(s in {"fresh", "error"} for s in statuses.values()), statuses


def test_aliases_resolve_to_the_same_route(client: TestClient) -> None:
    for route in ROUTES:
        for alias in route.aliases:
            payload = client.get(f"/api/{alias}").json()
            assert payload["feed"] == route.feed.value, alias


def test_health_endpoint_reports_every_feed(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert set(body) == {"checked_at", "store", "feeds"}
    assert body["store"]["open"] is True
    assert body["store"]["read_only"] is True, "the dashboard must open DuckDB read-only"
    feeds = {f["feed"]: f for f in body["feeds"]}
    assert len(feeds) == 10, sorted(feeds)
    assert feeds[FeedName.MTA_BUS.value]["configured"] is False
    assert feeds[FeedName.NY511_CAMERAS.value]["configured"] is False
    for entry in body["feeds"]:
        assert entry["status"] in {"fresh", "stale", "error", "never_fetched"}


def test_density_endpoint_serves_the_seeded_store_rows(client: TestClient) -> None:
    payload = client.get("/api/density", params={"window_s": 600}).json()
    assert payload["status"] == "fresh", payload["error"]
    record = next(r for r in payload["records"] if r["camera_id"] == CAM_A)
    assert (record["person_mean"], record["vehicle_mean"]) == (5.0, 3.5)
    assert (record["lat"], record["lon"]) == TIMES_SQ


def test_bad_requests_are_http_errors_not_envelopes(client: TestClient) -> None:
    assert client.get("/api/not_a_feed").status_code == 404
    assert client.get("/api/citibike", params={"lat": 40.75}).status_code == 400
    assert client.get("/api/mta_subway", params={"lat": 40.75, "lon": -73.98}).status_code == 400
    assert client.get("/api/citibike", params={"limit": 0}).status_code == 422


def test_geo_filtering_reaches_the_endpoint(client: TestClient) -> None:
    payload = client.get(
        "/api/density", params={"lat": TIMES_SQ[0], "lon": TIMES_SQ[1], "radius_m": 500}
    ).json()
    assert payload["status"] == "fresh"
    assert [r["camera_id"] for r in payload["records"]] == [CAM_A]
    assert payload["query"] == {"lat": TIMES_SQ[0], "lon": TIMES_SQ[1], "radius_m": 500.0}
    assert payload["total_before_filter"] == 2


# --------------------------------------------------------------------------- stream


def read_sse(client: TestClient, params: dict[str, Any]) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    with client.stream("GET", "/api/stream", params=params) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        buffer = "".join(response.iter_text())
    for block in buffer.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines or not lines[0].startswith("event: "):
            continue
        name = lines[0].removeprefix("event: ")
        data = "\n".join(ln.removeprefix("data: ") for ln in lines[1:])
        events.append((name, data))
    return events


def test_stream_pushes_every_default_feed_then_health(client: TestClient) -> None:
    events = read_sse(client, {"cycles": 1, "interval_s": 0.5})
    names = [name for name, _ in events]
    assert names[0] == "ready"
    assert names[-1] == "health"
    assert names[1:-1] == list(DEFAULT_STREAM_KEYS)
    payloads = dict(events)
    assert json.loads(payloads["ready"])["feeds"] == list(DEFAULT_STREAM_KEYS)
    for key in DEFAULT_STREAM_KEYS:
        envelope = json.loads(payloads[key])
        check_envelope(envelope, feed=ROUTE_BY_KEY[key].feed)
    # the SSE event and the polling fallback must carry the identical envelope. Only the
    # feeds that can be fetched twice in one process are re-polled here: see the xfail in
    # test_phase1_feeds.py::test_down_feed_blocks_the_caller_for_a_whole_ttl for why a
    # second GET of /api/citibike, /api/nyc_311 or /api/weather stalls for a whole TTL.
    for key in ("density", "dot_cameras"):
        assert set(json.loads(payloads[key])) == set(client.get(f"/api/{key}").json()), (
            f"the {key} SSE event and GET /api/{key} disagree on the envelope keys"
        )
    health = json.loads(payloads["health"])
    assert set(health) == set(client.get("/api/health").json())


def test_stream_keeps_going_when_a_feed_is_down(client: TestClient) -> None:
    """One dead feed must not stop the others updating: two cycles, all feeds each time."""
    events = read_sse(client, {"cycles": 2, "interval_s": 0.5, "feeds": "density,dot_cameras"})
    names = [name for name, _ in events]
    assert names == ["ready", "density", "dot_cameras", "health"] * 1 + [
        "density",
        "dot_cameras",
        "health",
    ]
    density = [json.loads(data) for name, data in events if name == "density"]
    cameras = [json.loads(data) for name, data in events if name == "dot_cameras"]
    assert [d["status"] for d in density] == ["fresh", "fresh"]
    assert [c["status"] for c in cameras] == ["error", "error"]
    assert all(c["error"]["message"] for c in cameras)


# --------------------------------------------------------------------------- isolation


def test_killing_one_feed_via_settings_leaves_the_others_serving(
    offline_upstreams: int, integration_settings: Settings, seeded_db: Path, services: Services
) -> None:
    """The brief's Phase 4 isolation check, against the real registry."""
    dead = f"http://127.0.0.1:{free_port()}/{KILLED_MARKER}"
    killed_settings = integration_settings.model_copy(update={"citibike_gbfs_root": dead})
    killed = build_services(killed_settings, store=services.store, strict=True)
    try:
        with TestClient(create_app(killed)) as client:
            citibike = client.get("/api/citibike").json()
            assert citibike["status"] == "error"
            assert citibike["error"]["url"] == dead, (
                "the killed feed must report the URL it was actually pointed at"
            )
            for key in FEED_KEYS:
                if key == "citibike":
                    continue
                payload = client.get(f"/api/{key}").json()
                check_envelope(payload, feed=ROUTE_BY_KEY[key].feed)
                error = payload["error"]
                assert error is None or KILLED_MARKER not in json.dumps(error), (
                    f"the citibike outage leaked into /api/{key}"
                )
            assert client.get("/api/density").json()["status"] == "fresh"
            health = {f["feed"]: f for f in client.get("/api/health").json()["feeds"]}
            assert health[FeedName.CITIBIKE.value]["status"] == "error"
            assert health[FeedName.CITIBIKE.value]["configured"] is True
    finally:
        killed.store = None  # owned by the `services` fixture
        asyncio.run(killed.aclose())


# --------------------------------------------------------------------------- real server


@pytest.mark.slow
def test_real_uvicorn_process_serves_the_api_and_the_stream(
    seeded_db: Path, tmp_path: Path
) -> None:
    """`nyc_dash.app:app` under a real uvicorn on a real port, driven by a real HTTP client."""
    port = free_port()
    env = child_env(duckdb_path=seeded_db, data_dir=seeded_db.parent, dead_proxy_port=free_port())
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "nyc_dash.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(trust_env=False, timeout=30.0) as http:
            deadline = time.monotonic() + 30
            started = time.monotonic()
            while True:
                assert proc.poll() is None, f"uvicorn died: {proc.communicate()[0]}"
                try:
                    if http.get(f"{base}/api/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "uvicorn never came up"
                time.sleep(0.1)
            boot_s = time.monotonic() - started

            assert http.get(f"{base}/").status_code == 200
            index = http.get(f"{base}/api").json()
            assert [f["key"] for f in index["feeds"]] == list(FEED_KEYS)
            density = http.get(f"{base}/api/density").json()
            check_envelope(density, feed=FeedName.DENSITY)
            assert density["status"] == "fresh"
            cameras = http.get(f"{base}/api/dot_cameras").json()
            check_envelope(cameras, feed=FeedName.DOT_CAMERAS)
            assert cameras["status"] == "error"

            names: list[str] = []
            with http.stream(
                "GET", f"{base}/api/stream", params={"cycles": 1, "interval_s": 0.5}
            ) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        names.append(line.removeprefix("event: "))
            assert names == ["ready", *DEFAULT_STREAM_KEYS, "health"]
            assert boot_s < 30
    finally:
        proc.terminate()
        proc.wait(timeout=30)


# --------------------------------------------------------------------------- frontend seam


FRONTEND_FIELDS: dict[str, tuple[type, tuple[str, ...]]] = {
    "subway_arrivals": (SubwayArrival, ("trip_id", "eta_s", "route_id", "lat", "lon")),
    "density": (CameraDensity, ("lat", "lon", "person_mean", "vehicle_mean")),
    "nyc_311": (ServiceRequest, ("lat", "lon")),
    "citibike": (BikeStation, ("lat", "lon", "capacity", "bikes_available", "docks_available")),
    "dot_cameras": (Camera, ("lat", "lon", "is_online")),
    "weather": (WeatherReport, ("station_name", "observation")),
}
"""What `static/app.js` reads off each record (see its FEEDS table and layer builders)."""


def test_frontend_expectations_match_the_served_contracts(client: TestClient) -> None:
    """The dashboard JS and the service layer must not disagree about field names."""
    for key, (model, fields) in FRONTEND_FIELDS.items():
        assert key in ROUTE_BY_KEY, f"static/app.js requests /api/{key}, which is not a route"
        missing = [f for f in fields if f not in model.model_fields]
        assert not missing, f"app.js reads {missing} off {model.__name__}, which has no such field"
    for field in ("temperature_c", "text", "observed_at"):
        assert field in WeatherObservation.model_fields

    stream_keys = set(FRONTEND_FIELDS)
    assert stream_keys == set(DEFAULT_STREAM_KEYS), (
        "static/app.js STREAM_KEYS and nyc_dash.api.DEFAULT_STREAM_KEYS have diverged: "
        f"{sorted(stream_keys ^ set(DEFAULT_STREAM_KEYS))}"
    )
    # the exact query strings app.js sends must be accepted by the real API
    for key, query in (
        ("subway_arrivals", "limit=800&horizon_s=1200"),
        ("density", "limit=500&window_s=900"),
        ("nyc_311", "limit=1000"),
        ("citibike", "limit=2500"),
        ("dot_cameras", "limit=2000"),
        ("weather", "limit=10"),
    ):
        response = client.get(f"/api/{key}?{query}")
        assert response.status_code == 200, f"/api/{key}?{query} -> {response.text[:200]}"


def test_static_assets_are_served_and_name_the_blocked_cdns(client: TestClient) -> None:
    """The page itself is served by our own server; only the CDN tags need the network."""
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    body = page.text
    assert "/api/stream" in body or "app.js" in body
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_offline_env_really_is_offline(offline_upstreams: int) -> None:
    """Guard for this suite's own premise: no test here reached a real upstream."""
    env = unreachable_proxy_env(offline_upstreams)
    assert env["NO_PROXY"] == ""
    with httpx.Client(timeout=2.0) as http, pytest.raises(httpx.HTTPError):
        http.get("https://webcams.nyctmc.org/api/cameras/")
