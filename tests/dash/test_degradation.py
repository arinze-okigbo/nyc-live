"""Degradation: fresh / stale / error envelopes, and one dead feed never touching another.

The gate this file covers: "every single feed can be killed without breaking the others".
Two halves:

* `test_killing_any_one_feed_leaves_every_other_feed_fresh` kills each feed in turn behind
  the real `CachedFeed` and asserts every other endpoint is still `fresh`.
* `test_every_upstream_dead_still_serves_honest_envelopes` builds the real adapters with
  every settings-driven base URL overridden to a dead URL (127.0.0.1:9, connection
  refused) and asserts the API still answers 200 with `status="error"` and a real error
  message for each of them, and that /api/health reports the failures.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from nyc_dash.api import ROUTE_BY_KEY, FeedRoute
from nyc_dash.app import create_app
from nyc_live.config import Settings
from nyc_live.contracts import Envelope, FeedName, now_utc
from nyc_live.store import Store
from tests.dash.conftest import Fakes, build_fakes, make_services

# feed key -> the feeds it reads; killing any of them must show up on that endpoint only
LAYER_KEYS = ("dot_cameras", "subway_arrivals", "nyc_311", "citibike", "weather")

KILLABLE: tuple[FeedName, ...] = (
    FeedName.DOT_CAMERAS,
    FeedName.MTA_SUBWAY,
    FeedName.MTA_SUBWAY_ALERTS,
    FeedName.MTA_SUBWAY_STOPS,
    FeedName.CITIBIKE,
    FeedName.NYC_311,
    FeedName.DOHMH_INSPECTIONS,
    FeedName.WEATHER,
)

# feeds whose upstream URL comes from Settings, so a dead-URL override really kills them.
# mta_subway_stops is excluded: its static GTFS URL is a module constant, not a setting,
# so overriding settings would not point it anywhere dead.
SETTINGS_DRIVEN_KEYS = (
    "dot_cameras",
    "mta_subway",
    "mta_subway_alerts",
    "citibike",
    "nyc_311",
    "dohmh_inspections",
    "weather",
)

FEED_TO_KEY = {
    FeedName.DOT_CAMERAS: "dot_cameras",
    FeedName.MTA_SUBWAY: "mta_subway",
    FeedName.MTA_SUBWAY_ALERTS: "mta_subway_alerts",
    FeedName.MTA_SUBWAY_STOPS: "mta_subway_stops",
    FeedName.CITIBIKE: "citibike",
    FeedName.NYC_311: "nyc_311",
    FeedName.DOHMH_INSPECTIONS: "dohmh_inspections",
    FeedName.WEATHER: "weather",
}


@contextmanager
def dashboard(
    settings: Settings, http_client: httpx.AsyncClient, store: Store, **kwargs: Any
) -> Iterator[tuple[TestClient, Fakes]]:
    fakes = build_fakes(now_utc(), store=store, **kwargs)
    app = create_app(make_services(settings, http_client, fakes, store))
    with TestClient(app) as client:
        yield client, fakes


def test_dead_feed_is_an_error_envelope_not_an_http_error(
    settings: Settings, http_client: httpx.AsyncClient, store: Store
) -> None:
    with dashboard(settings, http_client, store, down={FeedName.CITIBIKE}) as (client, _):
        response = client.get("/api/citibike")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "error"
        assert body["records"] == []
        assert body["fetched_at"] is None and body["stale_after"] is None
        assert body["error"]["kind"] == "upstream_http"
        assert body["error"]["upstream_status"] == 403
        assert "403" in body["error"]["message"]
        assert body["error"]["feed"] == FeedName.CITIBIKE.value


def test_stale_feed_keeps_the_last_good_snapshot_and_says_why(
    settings: Settings, http_client: httpx.AsyncClient, store: Store
) -> None:
    with dashboard(settings, http_client, store, born_stale={FeedName.NYC_311}) as (client, fakes):
        first = client.get("/api/nyc_311").json()
        assert first["status"] == "fresh"
        fakes.kill(FeedName.NYC_311)  # upstream goes down after one good snapshot
        body = client.get("/api/nyc_311").json()
        assert body["status"] == "stale"
        assert len(body["records"]) == 2  # last good records are still served
        assert body["fetched_at"] == first["fetched_at"]
        assert body["error"]["kind"] == "upstream_http"
        assert "403" in body["error"]["message"]


def test_key_gated_feed_without_a_key_is_not_configured(
    settings: Settings, http_client: httpx.AsyncClient, store: Store
) -> None:
    with dashboard(settings, http_client, store) as (client, _):
        body = client.get("/api/mta_bus").json()
        assert body["status"] == "error"
        assert body["error"]["kind"] == "not_configured"
        health = {f["feed"]: f for f in client.get("/api/health").json()["feeds"]}
        assert health[FeedName.MTA_BUS.value]["configured"] is False


def test_feed_missing_from_the_registry_is_reported_honestly(
    settings: Settings, http_client: httpx.AsyncClient, store: Store
) -> None:
    with dashboard(settings, http_client, store, missing={FeedName.WEATHER}) as (client, _):
        body = client.get("/api/weather").json()
        assert body["status"] == "error"
        assert body["error"]["kind"] == "internal"
        assert "not registered" in body["error"]["message"]
        assert client.get("/api/citibike").json()["status"] == "fresh"


@pytest.mark.parametrize("victim", KILLABLE, ids=[f.value for f in KILLABLE])
def test_killing_any_one_feed_leaves_every_other_feed_fresh(
    settings: Settings, http_client: httpx.AsyncClient, store: Store, victim: FeedName
) -> None:
    with dashboard(settings, http_client, store, down={victim}) as (client, _):
        assert client.get(f"/api/{FEED_TO_KEY[victim]}").json()["status"] == "error"
        for feed, key in FEED_TO_KEY.items():
            if feed is victim:
                continue
            body = client.get(f"/api/{key}").json()
            assert body["status"] == "fresh", f"{key} broke when {victim.value} was killed"
            assert body["error"] is None
        # the derived layers degrade only when one of their own inputs is dead
        arrivals = client.get("/api/subway_arrivals").json()
        expected = (
            "error" if victim in {FeedName.MTA_SUBWAY, FeedName.MTA_SUBWAY_STOPS} else "fresh"
        )
        assert arrivals["status"] == expected
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200


def test_a_crashing_handler_becomes_an_error_envelope(
    settings: Settings,
    http_client: httpx.AsyncClient,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> Envelope[Any]:
        raise RuntimeError("handler exploded")

    broken = FeedRoute(
        key="weather", feed=FeedName.WEATHER, label="Weather", handler=boom, default_limit=10
    )
    monkeypatch.setitem(ROUTE_BY_KEY, "weather", broken)
    with dashboard(settings, http_client, store) as (client, _):
        response = client.get("/api/weather")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "error"
        assert body["error"]["kind"] == "internal"
        assert "handler exploded" in body["error"]["message"]
        assert client.get("/api/citibike").json()["status"] == "fresh"


def test_every_upstream_dead_still_serves_honest_envelopes(settings: Settings) -> None:
    """Real adapters, every settings-driven upstream pointed at a refused connection."""
    dead = "http://127.0.0.1:9"
    killed = settings.model_copy(
        update={
            "dot_cameras_base": f"{dead}/api/cameras",
            "mta_gtfs_base": dead,
            "citibike_gbfs_root": f"{dead}/gbfs.json",
            "socrata_base": dead,
            "weather_base": dead,
            "http_retries": 0,
            "http_timeout_s": 2.0,
        }
    )
    with TestClient(create_app(settings=killed)) as client:
        for key in SETTINGS_DRIVEN_KEYS:
            response = client.get(f"/api/{key}")
            assert response.status_code == 200, key
            body = response.json()
            assert body["status"] == "error", f"{key} was {body['status']}"
            assert body["records"] == []
            assert body["error"]["message"], key
            assert body["error"]["kind"] in {
                "upstream_http",
                "upstream_timeout",
                "upstream_parse",
                "internal",
            }, body["error"]["kind"]
        health = {f["feed"]: f for f in client.get("/api/health").json()["feeds"]}
        for key in SETTINGS_DRIVEN_KEYS:
            entry = health[ROUTE_BY_KEY[key].feed.value]
            assert entry["status"] == "error", key
            assert entry["last_error"] is not None, key
            assert entry["consecutive_failures"] >= 1, key
        assert client.get("/").status_code == 200
