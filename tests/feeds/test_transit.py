"""Offline tests for the MTA subway adapters.

* Recorded-fixture replays skip (naming the missing file) until the protobuf fixtures in
  tests/fixtures/transit/ are recorded per RECORD.md; the static GTFS fixture IS recorded.
* Error paths and the shared raw-bytes cache are exercised with inline bodies via respx.
* `test_trips_parsing_synthetic_feed` builds a FeedMessage in-test to unit-test parsing.
  It is a synthetic parsing test, not an upstream fixture, and is never saved to disk.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from google.transit import gtfs_realtime_pb2

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    ErrorKind,
    FeedAdapter,
    FeedName,
    FeedUnavailable,
    VehicleStatus,
)
from nyc_live.feeds.transit import (
    ALERTS_SLUG,
    STATIC_GTFS_URL,
    SUBWAY_FEED_SLUGS,
    SubwayAlertsAdapter,
    SubwayStopsAdapter,
    SubwayTripsAdapter,
    alerts_url,
    direction_from_trip_id,
    raw_cache,
    reset_raw_cache,
    subway_feed_url,
)
from nyc_live.http import make_client

FIXTURES = Path(__file__).parent.parent / "fixtures" / "transit"
STOPS_FIXTURE = FIXTURES / "gtfs_subway_trimmed.zip"


def _fixture_bytes(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"fixture not recorded yet: {path} (see {FIXTURES / 'RECORD.md'})")
    return path.read_bytes()


@pytest.fixture(autouse=True)
def _fresh_raw_cache() -> Iterator[None]:
    reset_raw_cache()
    yield
    reset_raw_cache()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # retries=0 keeps the 5xx/timeout tests free of backoff sleeps; one test opts back in
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        NYC_LIVE_DATA_DIR=tmp_path / "data",
        NYC_LIVE_HTTP_RETRIES=0,
        NYC_LIVE_USER_AGENT="nyc-live-tests/0.1 (https://github.com/arinze-okigbo/nyc-live)",
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(settings) as c:
        yield c


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


def _slug_url(settings: Settings, slug: str) -> str:
    return subway_feed_url(settings.mta_gtfs_base, slug)


def _header_only(ts: int = 1_757_300_000) -> bytes:
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = ts
    return msg.SerializeToString()


def _mock_all_slugs(
    mock: respx.MockRouter, settings: Settings, body: bytes
) -> dict[str, respx.Route]:
    """Register every slug with `body`. Keep the returned routes: respx's `mock.get(url)`
    re-registers (and un-mocks) a route rather than looking it up."""
    return {
        slug: mock.get(_slug_url(settings, slug)).mock(
            return_value=httpx.Response(
                200, content=body, headers={"content-type": "application/x-protobuf"}
            )
        )
        for slug in SUBWAY_FEED_SLUGS
    }


# ---------------------------------------------------------------------------
# Recorded-fixture replays (skip until recorded; see RECORD.md)
# ---------------------------------------------------------------------------


async def test_trips_replays_recorded_fixtures(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    for slug in SUBWAY_FEED_SLUGS:
        mock.get(_slug_url(settings, slug)).mock(
            return_value=httpx.Response(200, content=_fixture_bytes(f"nyct-{slug}.pb"))
        )
    adapter = SubwayTripsAdapter(client=client, settings=settings)
    snap = await adapter.fetch()
    assert snap.feed is FeedName.MTA_SUBWAY and len(snap.records) > 0
    assert {t.feed_slug for t in snap.records} <= set(SUBWAY_FEED_SLUGS)
    assert all(t.trip_id and t.route_id for t in snap.records)
    assert any(t.direction in ("N", "S") for t in snap.records)
    assert any(t.stop_times for t in snap.records)
    assert any(t.vehicle is not None for t in snap.records)
    assert snap.upstream_generated_at is not None


async def test_alerts_replays_recorded_fixture(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    mock.get(alerts_url(settings.mta_gtfs_base)).mock(
        return_value=httpx.Response(200, content=_fixture_bytes("subway-alerts.pb"))
    )
    adapter = SubwayAlertsAdapter(client=client, settings=settings)
    snap = await adapter.fetch()
    assert snap.feed is FeedName.MTA_SUBWAY_ALERTS and len(snap.records) > 0
    assert all(a.id and a.header for a in snap.records)
    assert any(a.routes for a in snap.records)
    assert snap.upstream_generated_at is not None


async def test_stops_replays_recorded_trimmed_gtfs(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    route = mock.get(STATIC_GTFS_URL).mock(
        return_value=httpx.Response(
            200,
            content=STOPS_FIXTURE.read_bytes(),
            headers={
                "content-type": "application/zip",
                "last-modified": "Thu, 27 Aug 2026 15:02:11 GMT",
            },
        )
    )
    adapter = SubwayStopsAdapter(client=client, settings=settings)
    assert isinstance(adapter, FeedAdapter)
    snap = await adapter.fetch()
    assert route.call_count == 1
    assert snap.feed is FeedName.MTA_SUBWAY_STOPS
    assert snap.source_url == STATIC_GTFS_URL
    assert snap.stale_after == snap.fetched_at + DEFAULT_TTL[FeedName.MTA_SUBWAY_STOPS]
    assert snap.upstream_generated_at == datetime(2026, 8, 27, 15, 2, 11, tzinfo=UTC)

    by_id = {s.stop_id: s for s in snap.records}
    assert len(by_id) == 18  # 6 stations x (parent, N, S) in the trimmed fixture
    times_sq = by_id["127"]
    assert times_sq.name == "Times Sq-42 St" and times_sq.parent_station is None
    assert (times_sq.lat, times_sq.lon) == (40.755290, -73.987495)
    assert by_id["127N"].parent_station == "127" and by_id["127S"].parent_station == "127"
    # routes derived from trips.txt + stop_times.txt and unioned up to the parent
    assert by_id["127N"].routes == ["1", "2", "3"]
    assert by_id["127"].routes == ["1", "2", "3"]
    assert by_id["R16"].routes == ["N", "Q", "R", "W"]
    assert by_id["L01S"].routes == ["L"]
    assert by_id["G22"].routes == ["G"]
    assert by_id["S31"].routes == ["SI"]
    assert by_id["A27"].routes == ["A", "C", "E"]


# ---------------------------------------------------------------------------
# Synthetic parsing unit test (NOT an upstream fixture; built in-memory only)
# ---------------------------------------------------------------------------


def _synthetic_gtfs_feed() -> bytes:
    """A hand-built FeedMessage exercising trip_update/vehicle merge and direction rules."""
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = 1_757_300_000

    tu = msg.entity.add(id="000001").trip_update
    tu.trip.trip_id = "063000_1..S03R"
    tu.trip.route_id = "1"
    tu.trip.start_date = "20260908"
    s1 = tu.stop_time_update.add(stop_id="127S")
    s1.arrival.time = 1_757_300_100
    s1.departure.time = 1_757_300_130
    s2 = tu.stop_time_update.add(stop_id="128S")
    s2.arrival.time = 1_757_300_200

    vp = msg.entity.add(id="000002").vehicle
    vp.trip.trip_id = "063000_1..S03R"
    vp.trip.route_id = "1"
    vp.current_stop_sequence = 12
    vp.stop_id = "127S"
    vp.current_status = gtfs_realtime_pb2.VehiclePosition.STOPPED_AT
    vp.timestamp = 1_757_299_990

    # vehicle-only trip, direction only derivable from the stop id suffix
    vp2 = msg.entity.add(id="000003").vehicle
    vp2.trip.trip_id = "064500_GS"
    vp2.trip.route_id = "GS"
    vp2.stop_id = "901N"
    vp2.current_status = gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO

    # entity with no route id anywhere: must be dropped, not fabricated
    msg.entity.add(id="000004").trip_update.trip.trip_id = "070000_X..N"
    return msg.SerializeToString()


async def test_trips_parsing_synthetic_feed(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    empty = _header_only()
    for slug in SUBWAY_FEED_SLUGS:
        body = _synthetic_gtfs_feed() if slug == "gtfs" else empty
        mock.get(_slug_url(settings, slug)).mock(return_value=httpx.Response(200, content=body))
    adapter = SubwayTripsAdapter(client=client, settings=settings)
    assert isinstance(adapter, FeedAdapter)
    snap = await adapter.fetch()

    assert len(snap.records) == 2
    assert snap.stale_after == snap.fetched_at + DEFAULT_TTL[FeedName.MTA_SUBWAY]
    assert snap.upstream_generated_at == datetime.fromtimestamp(1_757_300_000, UTC)
    assert snap.source_url.endswith("/nyct/{" + ",".join(SUBWAY_FEED_SLUGS) + "}")

    one, shuttle = snap.records
    assert one.trip_id == "063000_1..S03R" and one.route_id == "1" and one.feed_slug == "gtfs"
    assert one.direction == "S" and one.start_date == "20260908"
    assert [s.stop_id for s in one.stop_times] == ["127S", "128S"]
    assert one.stop_times[0].arrival == datetime.fromtimestamp(1_757_300_100, UTC)
    assert one.stop_times[0].departure == datetime.fromtimestamp(1_757_300_130, UTC)
    assert one.stop_times[1].departure is None
    assert one.vehicle is not None
    assert one.vehicle.current_stop_id == "127S"
    assert one.vehicle.status is VehicleStatus.STOPPED_AT
    assert one.vehicle.current_stop_sequence == 12
    assert one.vehicle.timestamp == datetime.fromtimestamp(1_757_299_990, UTC)

    assert shuttle.trip_id == "064500_GS" and shuttle.route_id == "GS"
    assert shuttle.direction == "N"  # from stop id 901N
    assert shuttle.stop_times == [] and shuttle.start_date is None
    assert shuttle.vehicle is not None
    assert shuttle.vehicle.status is VehicleStatus.IN_TRANSIT_TO
    assert shuttle.vehicle.timestamp is None and shuttle.vehicle.current_stop_sequence is None


@pytest.mark.parametrize(
    ("trip_id", "expected"),
    [
        ("063000_1..S03R", "S"),
        ("A20240102WKD_021150_A..N55R", "N"),
        ("064500_GS.N01R", "N"),
        ("109350_L..S", "S"),
        ("SIR-FA2017-SI017-Weekday-08_146500_SI..N", "N"),
        ("SATURDAY_only", None),
        ("", None),
    ],
)
def test_direction_from_trip_id(trip_id: str, expected: str | None) -> None:
    assert direction_from_trip_id(trip_id) == expected


def _synthetic_alerts_feed() -> bytes:
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = 1_757_300_000
    al = msg.entity.add(id="lmm:planned_work:1").alert
    p = al.active_period.add()
    p.start = 1_757_290_000
    p.end = 1_757_390_000
    al.informed_entity.add(route_id="A")
    al.informed_entity.add(route_id="C")
    al.informed_entity.add(route_id="A", stop_id="A27S")
    al.effect = gtfs_realtime_pb2.Alert.DETOUR
    al.header_text.translation.add(text="<p>html</p>", language="en-html")
    al.header_text.translation.add(text="A and C trains skip 42 St", language="en")
    al.description_text.translation.add(text="Use the E instead.", language="en")
    # open-ended second alert
    al2 = msg.entity.add(id="lmm:alert:2").alert
    al2.active_period.add().start = 1_757_295_000
    al2.informed_entity.add(route_id="L")
    al2.header_text.translation.add(text="L delays", language="en")
    # alert without header text is dropped
    msg.entity.add(id="lmm:alert:3").alert.informed_entity.add(route_id="G")
    return msg.SerializeToString()


async def test_alerts_parsing_synthetic_feed(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    mock.get(alerts_url(settings.mta_gtfs_base)).mock(
        return_value=httpx.Response(200, content=_synthetic_alerts_feed())
    )
    adapter = SubwayAlertsAdapter(client=client, settings=settings)
    assert isinstance(adapter, FeedAdapter)
    snap = await adapter.fetch()
    assert snap.source_url.endswith("/" + ALERTS_SLUG)
    assert snap.stale_after == snap.fetched_at + DEFAULT_TTL[FeedName.MTA_SUBWAY_ALERTS]
    assert len(snap.records) == 2
    a, b = snap.records
    assert a.id == "lmm:planned_work:1"
    assert a.header == "A and C trains skip 42 St"  # plain 'en' preferred over 'en-html'
    assert a.description == "Use the E instead."
    assert a.routes == ["A", "C"] and a.stop_ids == ["A27S"]
    assert a.effect == "DETOUR"
    assert a.active_from == datetime.fromtimestamp(1_757_290_000, UTC)
    assert a.active_until == datetime.fromtimestamp(1_757_390_000, UTC)
    assert a.updated_at is None
    assert b.active_until is None and b.effect is None and b.description is None


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_trips_404_on_one_slug_is_not_found_naming_slug(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_all_slugs(mock, settings, _synthetic_gtfs_feed())
    mock.get(_slug_url(settings, "gtfs-ace")).mock(return_value=httpx.Response(404))
    adapter = SubwayTripsAdapter(client=client, settings=settings)
    with pytest.raises(FeedUnavailable) as excinfo:
        await adapter.fetch()
    err = excinfo.value
    assert err.kind is ErrorKind.NOT_FOUND and err.upstream_status == 404
    assert "gtfs-ace" in err.message and err.url == _slug_url(settings, "gtfs-ace")
    assert err.feed is FeedName.MTA_SUBWAY


async def test_alerts_404_is_not_found_naming_slug(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    mock.get(alerts_url(settings.mta_gtfs_base)).mock(return_value=httpx.Response(404))
    adapter = SubwayAlertsAdapter(client=client, settings=settings)
    with pytest.raises(FeedUnavailable) as excinfo:
        await adapter.fetch()
    assert excinfo.value.kind is ErrorKind.NOT_FOUND
    assert ALERTS_SLUG in excinfo.value.message


async def test_stops_404_is_not_found(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    mock.get(STATIC_GTFS_URL).mock(return_value=httpx.Response(404))
    adapter = SubwayStopsAdapter(client=client, settings=settings)
    with pytest.raises(FeedUnavailable) as excinfo:
        await adapter.fetch()
    assert excinfo.value.kind is ErrorKind.NOT_FOUND and excinfo.value.url == STATIC_GTFS_URL


async def test_trips_5xx_retries_then_upstream_http(
    mock: respx.MockRouter, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        NYC_LIVE_DATA_DIR=tmp_path / "data",
        NYC_LIVE_HTTP_RETRIES=1,
    )
    _mock_all_slugs(mock, settings, _synthetic_gtfs_feed())
    bad = mock.get(_slug_url(settings, "gtfs-l")).mock(return_value=httpx.Response(503))
    adapter = SubwayTripsAdapter(client=client, settings=settings)
    with pytest.raises(FeedUnavailable) as excinfo:
        await adapter.fetch()
    assert excinfo.value.kind is ErrorKind.UPSTREAM_HTTP
    assert excinfo.value.upstream_status == 503
    assert "gtfs-l" in excinfo.value.message
    assert bad.call_count == 2  # retries=1 -> 2 attempts


async def test_trips_timeout_is_upstream_timeout(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_all_slugs(mock, settings, _synthetic_gtfs_feed())
    mock.get(_slug_url(settings, "gtfs-si")).mock(side_effect=httpx.ReadTimeout("slow"))
    adapter = SubwayTripsAdapter(client=client, settings=settings)
    with pytest.raises(FeedUnavailable) as excinfo:
        await adapter.fetch()
    assert excinfo.value.kind is ErrorKind.UPSTREAM_TIMEOUT
    assert "gtfs-si" in excinfo.value.message


async def test_trips_non_protobuf_body_is_upstream_parse(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_all_slugs(mock, settings, _synthetic_gtfs_feed())
    mock.get(_slug_url(settings, "gtfs-g")).mock(
        return_value=httpx.Response(200, text="<html><body>maintenance</body></html>")
    )
    adapter = SubwayTripsAdapter(client=client, settings=settings)
    with pytest.raises(FeedUnavailable) as excinfo:
        await adapter.fetch()
    assert excinfo.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "gtfs-g" in excinfo.value.message


async def test_trips_all_slugs_empty_is_upstream_parse(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_all_slugs(mock, settings, _header_only())
    adapter = SubwayTripsAdapter(client=client, settings=settings)
    with pytest.raises(FeedUnavailable) as excinfo:
        await adapter.fetch()
    assert excinfo.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "zero trips" in excinfo.value.message


async def test_stops_bad_zip_is_upstream_parse(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    mock.get(STATIC_GTFS_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    adapter = SubwayStopsAdapter(client=client, settings=settings)
    with pytest.raises(FeedUnavailable) as excinfo:
        await adapter.fetch()
    assert excinfo.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "not a zip" in excinfo.value.message


async def test_stops_zip_without_stops_txt_is_upstream_parse(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("agency.txt", "agency_id,agency_name\nMTA NYCT,MTA New York City Transit\n")
    mock.get(STATIC_GTFS_URL).mock(return_value=httpx.Response(200, content=buf.getvalue()))
    adapter = SubwayStopsAdapter(client=client, settings=settings)
    with pytest.raises(FeedUnavailable) as excinfo:
        await adapter.fetch()
    assert excinfo.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "stops.txt" in excinfo.value.message


async def test_stops_out_of_bbox_rows_are_dropped(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    # real trimmed stops.txt plus one inline synthetic row placed in Philadelphia
    with zipfile.ZipFile(STOPS_FIXTURE) as src:
        stops_txt = src.read("stops.txt").decode("utf-8")
    stops_txt += "ZZ1,Not In NYC (synthetic test row),39.952583,-75.165222,1,\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("stops.txt", stops_txt)  # no trips/stop_times -> routes stay empty
    mock.get(STATIC_GTFS_URL).mock(return_value=httpx.Response(200, content=buf.getvalue()))
    adapter = SubwayStopsAdapter(client=client, settings=settings)
    snap = await adapter.fetch()
    ids = {s.stop_id for s in snap.records}
    assert "ZZ1" not in ids and len(ids) == 18
    assert all(s.routes == [] for s in snap.records)
    assert snap.upstream_generated_at is None  # no Last-Modified header in this response


# ---------------------------------------------------------------------------
# Shared raw-bytes cache: no double fetch within TTL, single-flight under concurrency
# ---------------------------------------------------------------------------


async def test_raw_cache_prevents_double_fetch_within_ttl(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    routes = _mock_all_slugs(mock, settings, _synthetic_gtfs_feed())
    alerts_route = mock.get(alerts_url(settings.mta_gtfs_base)).mock(
        return_value=httpx.Response(200, content=_synthetic_alerts_feed())
    )
    trips = SubwayTripsAdapter(client=client, settings=settings)
    alerts = SubwayAlertsAdapter(client=client, settings=settings)

    first = await trips.fetch()
    second = await trips.fetch()
    await alerts.fetch()
    await alerts.fetch()

    for slug, route in routes.items():
        assert route.call_count == 1, slug
    assert alerts_route.call_count == 1
    assert second.fetched_at == first.fetched_at  # served from the shared cache
    assert raw_cache().peek(alerts_url(settings.mta_gtfs_base)) is not None

    # expiring the cache entry forces exactly one more upstream fetch per URL
    reset_raw_cache()
    await trips.fetch()
    for slug, route in routes.items():
        assert route.call_count == 2, slug


async def test_raw_cache_single_flight_under_concurrency(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    routes = _mock_all_slugs(mock, settings, _synthetic_gtfs_feed())
    trips = SubwayTripsAdapter(client=client, settings=settings)
    snaps = await asyncio.gather(trips.fetch(), trips.fetch(), trips.fetch())
    assert len({s.fetched_at for s in snaps}) == 1
    for slug, route in routes.items():
        assert route.call_count == 1, slug


async def test_raw_cache_failure_does_not_poison_next_attempt(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_all_slugs(mock, settings, _synthetic_gtfs_feed())
    flaky = mock.get(_slug_url(settings, "gtfs-jz"))
    flaky.side_effect = [httpx.Response(503), httpx.Response(200, content=_header_only())]
    trips = SubwayTripsAdapter(client=client, settings=settings)
    with pytest.raises(FeedUnavailable):
        await trips.fetch()
    started = datetime.now(UTC)
    snap = await trips.fetch()  # must not sleep out a 30 s rate-limit floor
    assert datetime.now(UTC) - started < timedelta(seconds=5)
    assert flaky.call_count == 2
    assert len(snap.records) == 2 * (len(SUBWAY_FEED_SLUGS) - 1)  # jz replays a header-only body
