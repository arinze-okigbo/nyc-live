"""/api/stream: SSE frames, per-feed isolation on the stream, and the polling fallback.

Driven over `httpx.ASGITransport`, so no socket is opened and no upstream is touched.
`cycles=N` bounds the stream (0 = until the client disconnects, which is what the browser
uses).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nyc_dash.api import DEFAULT_STREAM_KEYS
from nyc_dash.app import create_app
from nyc_dash.stream import format_event
from nyc_live.config import Settings
from nyc_live.contracts import FeedName, now_utc
from nyc_live.services import Services
from nyc_live.store import Store
from tests.dash.conftest import build_fakes, make_services


def parse_sse(text: str) -> list[tuple[str, Any]]:
    """[(event name, parsed data)] in wire order; comment lines are ignored."""
    events: list[tuple[str, Any]] = []
    name: str | None = None
    data: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:") :].strip())
        elif line == "" and name is not None:
            events.append((name, json.loads("\n".join(data)) if data else None))
            name, data = None, []
    return events


async def collect(app: FastAPI, url: str) -> tuple[httpx.Response, list[tuple[str, Any]]]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://dash.test") as client:
        async with client.stream("GET", url) as response:
            chunks = [chunk async for chunk in response.aiter_text()]
        return response, parse_sse("".join(chunks))


def test_format_event_is_valid_sse() -> None:
    frame = format_event("weather", {"status": "fresh"})
    assert frame == 'event: weather\ndata: {"status":"fresh"}\n\n'


async def test_stream_pushes_one_envelope_per_feed(app: FastAPI) -> None:
    response, events = await collect(app, "/api/stream?cycles=1")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"

    names = [name for name, _ in events]
    assert names[0] == "ready"
    assert names[1:-1] == list(DEFAULT_STREAM_KEYS)
    assert names[-1] == "health"

    ready = events[0][1]
    assert ready["feeds"] == list(DEFAULT_STREAM_KEYS)
    assert ready["interval_s"] == 15.0

    by_name = dict(events)
    for key in DEFAULT_STREAM_KEYS:
        envelope = by_name[key]
        assert set(envelope) == {
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
        assert envelope["status"] in {"fresh", "stale", "error"}
    assert by_name["citibike"]["status"] == "fresh"
    assert len(by_name["citibike"]["records"]) == 2
    # density has no samples in this store: honest error on the stream too
    assert by_name["density"]["status"] == "error"
    assert by_name["density"]["error"]["kind"] == "not_configured"
    assert by_name["health"]["feeds"]


async def test_stream_repeats_every_cycle_without_a_page_refresh(app: FastAPI) -> None:
    _, events = await collect(app, "/api/stream?cycles=2&interval_s=0.5&feeds=citibike,weather")
    names = [name for name, _ in events]
    assert names == [
        "ready",
        "citibike",
        "weather",
        "health",
        "citibike",
        "weather",
        "health",
    ]


async def test_stream_feed_selection_and_geo_arguments(app: FastAPI) -> None:
    _, events = await collect(
        app, "/api/stream?cycles=1&feeds=citibike&lat=40.7033&lon=-74.0170&radius_m=300"
    )
    by_name = dict(events)
    assert [n for n, _ in events] == ["ready", "citibike", "health"]
    assert by_name["citibike"]["query"] == {"lat": 40.7033, "lon": -74.017, "radius_m": 300.0}
    assert [r["station_id"] for r in by_name["citibike"]["records"]] == ["s2"]


async def test_stream_isolates_a_dead_feed(
    settings: Settings, http_client: httpx.AsyncClient, store: Store
) -> None:
    fakes = build_fakes(now_utc(), store=store, down={FeedName.NYC_311})
    app = create_app(make_services(settings, http_client, fakes, store))
    _, events = await collect(app, "/api/stream?cycles=1&feeds=nyc_311,citibike,weather")
    by_name = dict(events)
    assert by_name["nyc_311"]["status"] == "error"
    assert by_name["nyc_311"]["error"]["upstream_status"] == 403
    assert by_name["citibike"]["status"] == "fresh"
    assert by_name["weather"]["status"] == "fresh"
    assert "health" in by_name


def test_stream_rejects_an_unknown_feed(client: TestClient) -> None:
    response = client.get("/api/stream", params={"feeds": "not_a_feed", "cycles": 1})
    assert response.status_code == 404
    assert "not_a_feed" in response.json()["detail"]


def test_stream_rejects_an_empty_feed_list(client: TestClient) -> None:
    response = client.get("/api/stream", params={"feeds": " ", "cycles": 1})
    assert response.status_code == 400


def test_stream_interval_is_bounded(client: TestClient) -> None:
    assert client.get("/api/stream", params={"interval_s": 0.01}).status_code == 422
    assert client.get("/api/stream", params={"interval_s": 10_000}).status_code == 422


@pytest.mark.parametrize("key", DEFAULT_STREAM_KEYS)
async def test_polling_fallback_returns_the_same_envelope_as_the_stream(
    app: FastAPI, services: Services, key: str
) -> None:
    """The documented fallback: every SSE event name is also a GET endpoint."""
    _, events = await collect(app, f"/api/stream?cycles=1&feeds={key}")
    streamed = dict(events)[key]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://dash.test") as client:
        polled = (await client.get(f"/api/{key}")).json()
    assert polled["feed"] == streamed["feed"]
    assert polled["status"] == streamed["status"]
    assert len(polled["records"]) == len(streamed["records"])
