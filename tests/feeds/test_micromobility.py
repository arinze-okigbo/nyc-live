"""Offline tests for the Citi Bike GBFS adapter.

Two groups:

* Behaviour tests use small GBFS-shaped bodies built inline. They are synthetic
  inputs for the adapter's logic (discovery, join, ttl, ebike fallback, error
  paths), not recordings of upstream data, so they live here and not under
  tests/fixtures/.
* Replay tests load the recorded fixtures under tests/fixtures/micromobility/
  and skip, naming the missing file, until they have been recorded
  (see tests/fixtures/micromobility/RECORD.md).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import respx

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    BikeStation,
    ErrorKind,
    FeedAdapter,
    FeedName,
    FeedUnavailable,
)
from nyc_live.feeds.micromobility import CitiBikeAdapter
from nyc_live.geo import in_nyc_bbox

CHILD_HOST = "https://gbfs.lyft.com"
CHILD_BASE = f"{CHILD_HOST}/gbfs/2.3/bkn/en"
INFO_URL = f"{CHILD_BASE}/station_information.json"
STATUS_URL = f"{CHILD_BASE}/station_status.json"
VEHICLE_TYPES_URL = f"{CHILD_BASE}/vehicle_types.json"

LAST_UPDATED = 1_725_800_000  # epoch seconds
CITIBIKE_TTL_S = int(DEFAULT_TTL[FeedName.CITIBIKE].total_seconds())

# Lower Manhattan; inside NYC_BBOX.
LAT, LON = 40.7128, -74.0060


# ---------------------------------------------------------------------------
# Synthetic GBFS body builders
# ---------------------------------------------------------------------------


def _feeds(
    *, info: str = INFO_URL, status: str = STATUS_URL, vehicle_types: str | None = None
) -> list[dict[str, str]]:
    feeds = [
        {"name": "system_information", "url": f"{CHILD_BASE}/system_information.json"},
        {"name": "station_information", "url": info},
        {"name": "station_status", "url": status},
    ]
    if vehicle_types is not None:
        feeds.append({"name": "vehicle_types", "url": vehicle_types})
    return feeds


def _root_v2(
    feeds: list[dict[str, str]] | None = None,
    *,
    lang: str = "en",
    ttl: int = 5,
    last_updated: int = LAST_UPDATED,
) -> dict[str, Any]:
    return {
        "last_updated": last_updated,
        "ttl": ttl,
        "version": "2.3",
        "data": {lang: {"feeds": feeds if feeds is not None else _feeds()}},
    }


def _root_v3(feeds: list[dict[str, str]] | None = None, *, ttl: int = 5) -> dict[str, Any]:
    return {
        "last_updated": datetime.fromtimestamp(LAST_UPDATED, tz=UTC).isoformat(),
        "ttl": ttl,
        "version": "3.0",
        "data": {"feeds": feeds if feeds is not None else _feeds()},
    }


def _info_row(
    i: int, *, lat: float = LAT, lon: float = LON, capacity: int | None = 30
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "station_id": f"s{i}",
        "name": f"Station {i}",
        "lat": lat + i * 0.001,
        "lon": lon + i * 0.001,
        "region_id": "71",
        "rental_methods": ["KEY", "CREDITCARD"],
    }
    if capacity is not None:
        row["capacity"] = capacity
    return row


def _status_row(
    i: int,
    *,
    bikes: int = 4,
    docks: int = 26,
    ebikes: int | None = 1,
    vehicle_types_available: list[dict[str, Any]] | None = None,
    last_reported: int | str | None = LAST_UPDATED - 30,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "station_id": f"s{i}",
        "num_bikes_available": bikes,
        "num_docks_available": docks,
        "num_bikes_disabled": 0,
        "num_docks_disabled": 0,
        "is_installed": 1,
        "is_renting": 1,
        "is_returning": 1,
        "last_reported": last_reported,
    }
    if ebikes is not None:
        row["num_ebikes_available"] = ebikes
    if vehicle_types_available is not None:
        row["vehicle_types_available"] = vehicle_types_available
    return row


def _doc(
    stations: list[dict[str, Any]], *, ttl: int = 5, last_updated: int | str = LAST_UPDATED
) -> dict[str, Any]:
    return {
        "last_updated": last_updated,
        "ttl": ttl,
        "version": "2.3",
        "data": {"stations": stations},
    }


def _vehicle_types_doc() -> dict[str, Any]:
    return {
        "last_updated": LAST_UPDATED,
        "ttl": 60,
        "version": "2.3",
        "data": {
            "vehicle_types": [
                {"vehicle_type_id": "1", "form_factor": "bicycle", "propulsion_type": "human"},
                {
                    "vehicle_type_id": "2",
                    "form_factor": "bicycle",
                    "propulsion_type": "electric_assist",
                },
            ]
        },
    }


def _mock_children(
    router: respx.Router,
    info: list[dict[str, Any]],
    status: list[dict[str, Any]],
    *,
    status_ttl: int = 5,
    status_last_updated: int | str = LAST_UPDATED,
    info_url: str = INFO_URL,
    status_url: str = STATUS_URL,
) -> None:
    router.get(info_url).mock(return_value=httpx.Response(200, json=_doc(info)))
    router.get(status_url).mock(
        return_value=httpx.Response(
            200, json=_doc(status, ttl=status_ttl, last_updated=status_last_updated)
        )
    )


@pytest.fixture
def root_url(settings: Settings) -> str:
    return settings.citibike_gbfs_root


@pytest.fixture
def adapter(settings: Settings) -> CitiBikeAdapter:
    return CitiBikeAdapter(client=httpx.AsyncClient(), settings=settings)


@pytest.fixture
def router() -> Any:
    with respx.mock(assert_all_called=False) as r:
        yield r


# ---------------------------------------------------------------------------
# Protocol + discovery
# ---------------------------------------------------------------------------


def test_adapter_satisfies_protocol(adapter: CitiBikeAdapter) -> None:
    assert isinstance(adapter, FeedAdapter)
    assert adapter.name is FeedName.CITIBIKE
    assert adapter.ttl == DEFAULT_TTL[FeedName.CITIBIKE]
    assert adapter.is_configured() is True


async def test_discovers_children_from_gbfs2_en_block(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    # A non-English block listed first must not win over `en`.
    root = _root_v2()
    root["data"] = {
        "es": {
            "feeds": _feeds(
                info=f"{CHILD_BASE}/../es/wrong.json", status=f"{CHILD_BASE}/../es/wrong2.json"
            )
        },
        **root["data"],
    }
    router.get(root_url).mock(return_value=httpx.Response(200, json=root))
    _mock_children(router, [_info_row(1), _info_row(2)], [_status_row(1), _status_row(2, bikes=0)])

    snap = await adapter.fetch()

    assert snap.feed is FeedName.CITIBIKE
    assert snap.source_url == root_url
    assert [r.station_id for r in snap.records] == ["s1", "s2"]
    s1 = snap.records[0]
    assert isinstance(s1, BikeStation)
    assert s1.name == "Station 1"
    assert s1.capacity == 30
    assert s1.bikes_available == 4
    assert s1.ebikes_available == 1
    assert s1.docks_available == 26
    assert s1.is_renting is True and s1.is_returning is True and s1.is_installed is True
    assert s1.last_reported == datetime.fromtimestamp(LAST_UPDATED - 30, tz=UTC)
    assert snap.records[1].bikes_available == 0
    assert snap.latency_ms is not None and snap.latency_ms >= 0
    assert router.get(INFO_URL).called and router.get(STATUS_URL).called


async def test_discovers_children_from_gbfs3_feeds(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v3()))
    status = _status_row(1, last_reported="2026-09-08T12:00:00Z")
    status.update({"is_installed": True, "is_renting": True, "is_returning": False})
    router.get(INFO_URL).mock(return_value=httpx.Response(200, json=_doc([_info_row(1)])))
    router.get(STATUS_URL).mock(
        return_value=httpx.Response(
            200, json=_doc([status], ttl=5, last_updated="2026-09-08T12:00:30+00:00")
        )
    )

    snap = await adapter.fetch()

    assert len(snap.records) == 1
    rec = snap.records[0]
    assert rec.is_returning is False and rec.is_installed is True
    assert rec.last_reported == datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    assert snap.upstream_generated_at == datetime(2026, 9, 8, 12, 0, 30, tzinfo=UTC)


async def test_falls_back_to_first_language_block_without_en(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2(lang="fr")))
    _mock_children(router, [_info_row(1)], [_status_row(1)])

    snap = await adapter.fetch()

    assert len(snap.records) == 1


async def test_children_on_a_different_host_than_the_root(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    other_info = "https://gbfs.example.test/v2/en/station_information.json"
    other_status = "https://gbfs.example.test/v2/en/station_status.json"
    assert urlsplit(other_info).netloc != urlsplit(root_url).netloc
    router.get(root_url).mock(
        return_value=httpx.Response(
            200, json=_root_v2(_feeds(info=other_info, status=other_status))
        )
    )
    _mock_children(
        router, [_info_row(1)], [_status_row(1)], info_url=other_info, status_url=other_status
    )

    snap = await adapter.fetch()

    assert len(snap.records) == 1
    assert router.get(other_info).called and router.get(other_status).called
    hosts = {urlsplit(str(c.request.url)).netloc for c in router.calls}
    assert hosts == {urlsplit(root_url).netloc, "gbfs.example.test"}


async def test_root_without_station_status_is_parse_error(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    feeds = [f for f in _feeds() if f["name"] != "station_status"]
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2(feeds)))

    with pytest.raises(FeedUnavailable) as exc_info:
        await adapter.fetch()

    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "station_status" in exc_info.value.message


# ---------------------------------------------------------------------------
# ttl / timestamps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("published_ttl", "expected_s"),
    [(5, CITIBIKE_TTL_S), (CITIBIKE_TTL_S, CITIBIKE_TTL_S), (300, 300)],
)
async def test_stale_after_is_max_of_default_ttl_and_published_ttl(
    adapter: CitiBikeAdapter,
    router: respx.Router,
    root_url: str,
    published_ttl: int,
    expected_s: int,
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2(ttl=published_ttl)))
    _mock_children(router, [_info_row(1)], [_status_row(1)], status_ttl=published_ttl)

    snap = await adapter.fetch()

    assert snap.stale_after - snap.fetched_at == timedelta(seconds=expected_s)
    assert snap.stale_after > snap.fetched_at


async def test_upstream_generated_at_comes_from_last_updated(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2(last_updated=1)))
    _mock_children(router, [_info_row(1)], [_status_row(1)], status_last_updated=LAST_UPDATED)

    snap = await adapter.fetch()

    assert snap.upstream_generated_at == datetime.fromtimestamp(LAST_UPDATED, tz=UTC)
    assert snap.upstream_generated_at is not None
    assert snap.upstream_generated_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Ebike fallback order
# ---------------------------------------------------------------------------


async def test_ebike_fallback_order(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(
        return_value=httpx.Response(200, json=_root_v2(_feeds(vehicle_types=VEHICLE_TYPES_URL)))
    )
    router.get(VEHICLE_TYPES_URL).mock(return_value=httpx.Response(200, json=_vehicle_types_doc()))
    vta = [{"vehicle_type_id": "1", "count": 3}, {"vehicle_type_id": "2", "count": 2}]
    status = [
        # num_ebikes_available wins even when vehicle_types_available disagrees
        _status_row(1, ebikes=7, vehicle_types_available=vta),
        # no num_ebikes_available -> sum of electric types from vehicle_types_available
        _status_row(2, ebikes=None, vehicle_types_available=vta),
        # neither -> None, never invented
        _status_row(3, ebikes=None),
    ]
    _mock_children(router, [_info_row(1), _info_row(2), _info_row(3)], status)

    snap = await adapter.fetch()

    by_id = {r.station_id: r for r in snap.records}
    assert by_id["s1"].ebikes_available == 7
    assert by_id["s2"].ebikes_available == 2
    assert by_id["s3"].ebikes_available is None
    assert router.get(VEHICLE_TYPES_URL).called


async def test_vehicle_types_not_fetched_when_num_ebikes_available_present(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(
        return_value=httpx.Response(200, json=_root_v2(_feeds(vehicle_types=VEHICLE_TYPES_URL)))
    )
    vt_route = router.get(VEHICLE_TYPES_URL).mock(return_value=httpx.Response(500))
    _mock_children(router, [_info_row(1)], [_status_row(1, ebikes=2)])

    snap = await adapter.fetch()

    assert snap.records[0].ebikes_available == 2
    assert not vt_route.called


async def test_vehicle_types_available_without_vehicle_types_feed_is_none(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2()))
    vta = [{"vehicle_type_id": "2", "count": 2}]
    _mock_children(
        router, [_info_row(1)], [_status_row(1, ebikes=None, vehicle_types_available=vta)]
    )

    with caplog.at_level(logging.WARNING, logger="nyc_live.feeds.micromobility"):
        snap = await adapter.fetch()

    assert snap.records[0].ebikes_available is None
    assert any("vehicle_types" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Join: unmatched rows, bbox
# ---------------------------------------------------------------------------


async def test_unmatched_rows_are_dropped_and_counted(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2()))
    info = [_info_row(i) for i in range(1, 41)] + [_info_row(99)]  # s99 has no status
    status = [_status_row(i) for i in range(1, 41)] + [_status_row(98)]  # s98 has no information
    _mock_children(router, info, status)

    with caplog.at_level(logging.INFO, logger="nyc_live.feeds.micromobility"):
        snap = await adapter.fetch()

    ids = {r.station_id for r in snap.records}
    assert len(snap.records) == 40
    assert "s98" not in ids and "s99" not in ids
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("2 unmatched" in m and "1 status-only" in m and "1 info-only" in m for m in msgs)


async def test_more_than_five_percent_unmatched_is_parse_error(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2()))
    info = [_info_row(i) for i in range(1, 21)]
    status = [_status_row(i) for i in range(1, 19)] + [_status_row(50), _status_row(51)]
    # 4 unmatched of 22 distinct ids = 18 %
    _mock_children(router, info, status)

    with pytest.raises(FeedUnavailable) as exc_info:
        await adapter.fetch()

    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "4 of 22" in exc_info.value.message


async def test_exactly_five_percent_unmatched_is_tolerated(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2()))
    info = [_info_row(i) for i in range(1, 21)]  # 20 ids, 1 unmatched = 5 %, not > 5 %
    status = [_status_row(i) for i in range(1, 20)]
    _mock_children(router, info, status)

    snap = await adapter.fetch()

    assert len(snap.records) == 19


async def test_out_of_bbox_stations_are_dropped_and_logged(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2()))
    info = [_info_row(1), _info_row(2, lat=51.5074, lon=-0.1278), _info_row(3, lat=0.0, lon=0.0)]
    assert not in_nyc_bbox(51.5074, -0.1278)
    _mock_children(router, info, [_status_row(1), _status_row(2), _status_row(3)])

    with caplog.at_level(logging.INFO, logger="nyc_live.feeds.micromobility"):
        snap = await adapter.fetch()

    assert [r.station_id for r in snap.records] == ["s1"]
    assert any(
        "2 stations with coordinates outside NYC bbox" in r.getMessage() for r in caplog.records
    )


async def test_empty_station_lists_never_yield_an_empty_snapshot(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2()))
    _mock_children(router, [], [])

    with pytest.raises(FeedUnavailable) as exc_info:
        await adapter.fetch()

    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE


# ---------------------------------------------------------------------------
# HTTP error paths
# ---------------------------------------------------------------------------


async def test_root_404_is_not_found(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(404))

    with pytest.raises(FeedUnavailable) as exc_info:
        await adapter.fetch()

    assert exc_info.value.kind is ErrorKind.NOT_FOUND
    assert exc_info.value.upstream_status == 404
    assert exc_info.value.url == root_url


async def test_child_404_is_not_found(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, json=_root_v2()))
    router.get(INFO_URL).mock(return_value=httpx.Response(200, json=_doc([_info_row(1)])))
    router.get(STATUS_URL).mock(return_value=httpx.Response(404))

    with pytest.raises(FeedUnavailable) as exc_info:
        await adapter.fetch()

    assert exc_info.value.kind is ErrorKind.NOT_FOUND
    assert exc_info.value.url == STATUS_URL


async def test_root_5xx_retries_then_upstream_http(
    settings: Settings, router: respx.Router, root_url: str
) -> None:
    settings = settings.model_copy(update={"http_retries": 1})
    adapter = CitiBikeAdapter(client=httpx.AsyncClient(), settings=settings)
    route = router.get(root_url).mock(return_value=httpx.Response(503))

    with pytest.raises(FeedUnavailable) as exc_info:
        await adapter.fetch()

    assert exc_info.value.kind is ErrorKind.UPSTREAM_HTTP
    assert exc_info.value.upstream_status == 503
    assert route.call_count == 2  # http_retries=1 -> two attempts


async def test_root_not_json_is_parse_error(
    adapter: CitiBikeAdapter, router: respx.Router, root_url: str
) -> None:
    router.get(root_url).mock(return_value=httpx.Response(200, text="<html>nope</html>"))

    with pytest.raises(FeedUnavailable) as exc_info:
        await adapter.fetch()

    assert exc_info.value.kind is ErrorKind.UPSTREAM_PARSE


# ---------------------------------------------------------------------------
# Replay of recorded fixtures (skip until recorded; see RECORD.md)
# ---------------------------------------------------------------------------


def _load_fixture(path: Path) -> dict[str, Any]:
    if not path.is_file():
        pytest.skip(f"fixture not recorded yet: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _feed_entries(root: dict[str, Any]) -> list[dict[str, Any]]:
    data = root["data"]
    if isinstance(data.get("feeds"), list):
        return data["feeds"]
    block = data.get("en") or next(v for v in data.values() if isinstance(v, dict))
    return block["feeds"]


def _rehost(url: str, host: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


@pytest.mark.parametrize("children_host", [None, "gbfs.example.test"])
async def test_replays_recorded_gbfs_fixtures(
    settings: Settings,
    router: respx.Router,
    root_url: str,
    fixtures_dir: Path,
    children_host: str | None,
) -> None:
    fx = fixtures_dir / "micromobility"
    root = _load_fixture(fx / "gbfs.json")
    info = _load_fixture(fx / "station_information.json")
    status = _load_fixture(fx / "station_status.json")
    bodies = {"station_information": info, "station_status": status}
    vt_path = fx / "vehicle_types.json"
    if vt_path.is_file():
        bodies["vehicle_types"] = json.loads(vt_path.read_text(encoding="utf-8"))

    if children_host is not None:
        # The root may list children on any host; the adapter must follow it.
        for entry in _feed_entries(root):
            entry["url"] = _rehost(entry["url"], children_host)
    router.get(root_url).mock(return_value=httpx.Response(200, json=root))
    routed: dict[str, str] = {}
    for entry in _feed_entries(root):
        body = bodies.get(entry["name"])
        if body is not None:
            routed[entry["name"]] = entry["url"]
            router.get(entry["url"]).mock(return_value=httpx.Response(200, json=body))
    assert {"station_information", "station_status"} <= routed.keys()

    adapter = CitiBikeAdapter(client=httpx.AsyncClient(), settings=settings)
    snap = await adapter.fetch()

    info_ids = {s["station_id"] for s in info["data"]["stations"]}
    status_ids = {s["station_id"] for s in status["data"]["stations"]}
    assert len(snap.records) <= len(info_ids & status_ids)
    assert len(snap.records) >= 1
    for rec in snap.records:
        assert rec.station_id in info_ids & status_ids
        assert in_nyc_bbox(rec.lat, rec.lon)
        assert rec.bikes_available >= 0 and rec.docks_available >= 0
    published_ttl = int(status.get("ttl") or 0)
    assert snap.stale_after - snap.fetched_at == timedelta(
        seconds=max(CITIBIKE_TTL_S, published_ttl)
    )
    assert snap.upstream_generated_at is not None
    for name, url in routed.items():
        if name != "vehicle_types":
            assert router.get(url).called, f"{name} was not fetched from {url}"
    if children_host is not None:
        child_hosts = {
            urlsplit(str(c.request.url)).netloc
            for c in router.calls
            if str(c.request.url) != root_url
        }
        assert child_hosts == {children_host}
