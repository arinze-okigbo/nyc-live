"""Offline tests for the MTA elevator/escalator outage adapter.

* The replay tests use two recorded fixtures (both real, trimmed, see
  `tests/fixtures/transit/RECORD.md`): `nyct_ene.json` (11 whole real outage rows out of
  the 136 live on 2026-09-09) and `gtfs_subway_ene_stops.zip` (real `stops.txt` /
  `trips.txt` / `stop_times.txt` rows for exactly the stations those outages name, plus
  the two stations that must NOT be matched).
* `test_unknown_equipment_type_is_dropped` mutates a real row in memory to a value the
  contract's enum has no member for. It is a synthetic parsing test, never written to
  disk as a fixture.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    ElevatorEquipmentType,
    ElevatorOutage,
    ErrorKind,
    FeedAdapter,
    FeedName,
    FeedUnavailable,
)
from nyc_live.feeds.accessibility import (
    ENE_SLUG,
    STATION_COMPLEX_RADIUS_M,
    ElevatorOutagesAdapter,
    StationIndex,
    build_station_index,
    canonical_route,
    match_station,
    normalize_station_name,
    outages_url,
    parse_ene_timestamp,
    parse_outage_rows,
    parse_outages,
    place_outages,
    reset_station_index,
    split_routes,
)
from nyc_live.feeds.transit import parse_static_gtfs, reset_raw_cache
from nyc_live.http import make_client

FIXTURES = Path(__file__).parent.parent / "fixtures" / "transit"
ENE_FIXTURE = "nyct_ene.json"
NOSUCHKEY_FIXTURE = "nyct_ene_nosuchkey.xml"
STOPS_FIXTURE = "gtfs_subway_ene_stops.zip"

# Real coordinates from the recorded GTFS bundle; the adapter must return these exactly.
TIMES_SQ_NQRW = (40.754672, -73.986754)  # R16
PORT_AUTHORITY = (40.757308, -73.989735)  # A27
EIGHTH_AV_L = (40.739777, -74.002578)  # L01
COURT_SQ_7 = (40.747023, -73.945264)  # 719


def _fixture_bytes(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"fixture not recorded yet: {path} (see {FIXTURES / 'RECORD.md'})")
    return path.read_bytes()


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    reset_raw_cache()
    reset_station_index()
    yield
    reset_raw_cache()
    reset_station_index()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
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


def _mock_upstreams(
    mock: respx.MockRouter,
    settings: Settings,
    *,
    ene: httpx.Response | None = None,
    stops: httpx.Response | None = None,
) -> tuple[respx.Route, respx.Route]:
    ene_route = mock.get(outages_url(settings.mta_gtfs_base)).mock(
        return_value=ene or httpx.Response(200, content=_fixture_bytes(ENE_FIXTURE))
    )
    stops_route = mock.get(settings.mta_static_gtfs_url).mock(
        return_value=stops or httpx.Response(200, content=_fixture_bytes(STOPS_FIXTURE))
    )
    return ene_route, stops_route


def _station_index_from_fixture() -> StationIndex:
    parsed = parse_static_gtfs(
        _fixture_bytes(STOPS_FIXTURE), feed=FeedName.MTA_SUBWAY_STOPS, url="fixture"
    )
    return build_station_index(parsed.stops)


def _by_equipment(records: list[ElevatorOutage]) -> dict[str, ElevatorOutage]:
    return {r.equipment_id: r for r in records}


# ---------------------------------------------------------------------------
# Recorded-fixture replay
# ---------------------------------------------------------------------------


async def test_outages_replays_recorded_fixture(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_upstreams(mock, settings)
    snap = await ElevatorOutagesAdapter(client=client, settings=settings).fetch()

    assert snap.feed is FeedName.MTA_ELEVATOR_OUTAGES
    assert snap.source_url.endswith(ENE_SLUG)
    assert snap.stale_after == snap.fetched_at + DEFAULT_TTL[FeedName.MTA_ELEVATOR_OUTAGES]
    assert len(snap.records) == 11  # every row in the trimmed fixture survives

    by_id = _by_equipment(snap.records)
    assert set(by_id) == {
        "EL224",
        "EL229",
        "EL230",
        "EL712",
        "ES105",
        "EL289X",
        "EL14X",
        "ES607X",
        "EL445X",
        "ES461X",
        "EL290X",
    }

    # one whole real row, field by field
    times_sq = by_id["EL229"]
    assert times_sq.station == "Times Sq-42 St"
    assert times_sq.equipment_type is ElevatorEquipmentType.ELEVATOR
    assert times_sq.routes == ["A", "C", "E", "N", "Q", "R", "W", "1", "2", "3", "7", "S"]
    assert times_sq.serving == "mezzanine to downtown N/Q/R/W platform"
    assert times_sq.is_ada is True
    assert times_sq.reason == "Maintenance"
    assert times_sq.is_maintenance is False  # ismaintenanceoutage is "N" even so
    # "09/22/2026 10:00:00 PM" New York local (EDT, UTC-4) -> 02:00 UTC the next day
    assert times_sq.outage_started == datetime(2026, 9, 23, 2, 0, tzinfo=UTC)
    assert times_sq.estimated_return == datetime(2026, 9, 23, 10, 0, tzinfo=UTC)

    escalator = by_id["ES105"]
    assert escalator.equipment_type is ElevatorEquipmentType.ESCALATOR
    assert escalator.is_ada is False  # ADA "N"

    # no borough smuggled anywhere: every field is a declared contract field
    assert set(ElevatorOutage.model_fields) >= {"equipment_id", "station", "is_upcoming"}
    assert "borough" not in ElevatorOutage.model_fields


async def test_upcoming_outages_are_not_reported_as_currently_out(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    """The subtle one: a scheduled future outage must never look like a broken lift."""
    _mock_upstreams(mock, settings)
    snap = await ElevatorOutagesAdapter(client=client, settings=settings).fetch()
    by_id = _by_equipment(snap.records)

    # real rows: EL229 is isupcomingoutage "Y", EL712 is "N" (out right now)
    assert by_id["EL229"].is_upcoming is True
    assert by_id["EL712"].is_upcoming is False
    upcoming = [o for o in snap.records if o.is_upcoming]
    active = [o for o in snap.records if not o.is_upcoming]
    assert {o.equipment_id for o in upcoming} == {"EL224", "EL229", "EL230"}
    assert len(active) == 8
    # both kinds ship in the same payload; the adapter filters neither away
    assert len(upcoming) + len(active) == len(snap.records)
    # an upcoming outage starts in the future relative to the recording
    recorded_at = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)
    assert all(o.outage_started is not None and o.outage_started > recorded_at for o in upcoming)


async def test_stations_are_placed_only_when_the_match_is_confident(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_upstreams(mock, settings)
    snap = await ElevatorOutagesAdapter(client=client, settings=settings).fetch()
    by_id = _by_equipment(snap.records)

    # unique name + route match
    assert (by_id["EL289X"].lat, by_id["EL289X"].lon) == PORT_AUTHORITY  # "42St/Port Authority-"
    assert (by_id["EL224"].lat, by_id["EL224"].lon) == EIGHTH_AV_L
    # inside one station complex: 127 / 725 / R16 are all "Times Sq-42 St"; the N/Q/R/W
    # house (R16) serves the most of trainno, and all three are within 120 m
    assert (by_id["EL229"].lat, by_id["EL229"].lon) == TIMES_SQ_NQRW
    assert (by_id["EL445X"].lat, by_id["EL445X"].lon) == COURT_SQ_7

    # "Cortlandt St" on the 1: the only GTFS station with that name is the N/R/W one,
    # so the name matches and the routes contradict -> unplaced, not mis-placed
    assert by_id["EL712"].lat is None and by_id["EL712"].lon is None
    assert by_id["EL14X"].lat is None
    # two real, distinct "Gun Hill Rd" stations 1.9 km apart -> unplaced
    assert by_id["ES105"].lat is None and by_id["ES105"].lon is None

    placed = [o for o in snap.records if o.lat is not None]
    assert len(placed) == 8
    # every unplaceable outage still survives in the list
    assert len(snap.records) == 11


def test_match_station_reasons_on_recorded_stops() -> None:
    index = _station_index_from_fixture()
    assert match_station(index, "8 Av", ["A", "C", "E", "L"])[1] == "unique"
    assert match_station(index, "Times Sq-42 St", ["N", "Q", "R", "W", "1", "7"])[1] == "complex"
    assert match_station(index, "Cortlandt St", ["1"])[1] == "route_contradiction"
    assert match_station(index, "Gun Hill Rd", ["2", "5"])[1] == "spread_too_wide"
    assert match_station(index, "Nowhere Plaza", ["Q"])[1] == "no_name_match"
    # the complex pick is a real GTFS stop, not a centroid of several
    picked, _ = match_station(index, "Court Sq", ["E", "F", "G", "7"])
    assert picked is not None
    assert (picked.lat, picked.lon) == COURT_SQ_7


def test_place_outages_without_a_station_index_keeps_every_record() -> None:
    parsed = parse_outages(_fixture_bytes(ENE_FIXTURE), url="fixture")
    records, stats = place_outages(parsed.outages, None)
    assert len(records) == len(parsed.outages) == 11
    assert all(r.lat is None and r.lon is None for r in records)
    assert stats.placed == 0


# ---------------------------------------------------------------------------
# Parsing units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # EDT (UTC-4): 10 PM local -> 02:00 UTC next day
        ("09/28/2026 10:00:00 PM", datetime(2026, 9, 29, 2, 0, tzinfo=UTC)),
        # the 12-hour clock actually matters: 06:00 AM is not 18:00
        ("09/29/2026 06:00:00 AM", datetime(2026, 9, 29, 10, 0, tzinfo=UTC)),
        ("09/09/2026 12:23:00 PM", datetime(2026, 9, 9, 16, 23, tzinfo=UTC)),
        ("09/09/2026 12:23:00 AM", datetime(2026, 9, 9, 4, 23, tzinfo=UTC)),
        # EST (UTC-5) on the other side of the DST boundary
        ("12/31/2026 11:00:00 PM", datetime(2027, 1, 1, 4, 0, tzinfo=UTC)),
        ("", None),
        ("   ", None),
    ],
)
def test_parse_ene_timestamp(raw: str, expected: datetime | None) -> None:
    assert parse_ene_timestamp(raw) == expected


def test_parse_ene_timestamp_rejects_a_foreign_format() -> None:
    with pytest.raises(ValueError):
        parse_ene_timestamp("2026-09-28T22:00:00")


def test_unparseable_timestamp_keeps_the_outage() -> None:
    row = dict(_first_fixture_row(), outagedate="not a date")
    parsed = parse_outage_rows([row])
    assert parsed.bad_timestamps == 1
    assert len(parsed.outages) == 1 and parsed.outages[0].outage_started is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("F", ["F"]),
        ("B/D/N/Q/R/2/3/4/5/LIRR", ["B", "D", "N", "Q", "R", "2", "3", "4", "5", "LIRR"]),
        ("A/C/E/N/Q/R/W/1/2/3/7/S", ["A", "C", "E", "N", "Q", "R", "W", "1", "2", "3", "7", "S"]),
        ("", []),
        ("F//M", ["F", "M"]),
        ("F/F", ["F"]),
    ],
)
def test_split_routes(raw: str, expected: list[str]) -> None:
    assert split_routes(raw) == expected


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("42St/Port Authority-Bus Terminal", "42 St-Port Authority Bus Terminal"),
        ("West 8 St-NY Aquarium", "W 8 St-NY Aquarium"),
        ("Times Sq-42 St", "Times Square - 42 St"),
        ("Atlantic Av-Barclays Ctr", "Atlantic Avenue-Barclays Center"),
    ],
)
def test_normalize_station_name_folds_real_variants(a: str, b: str) -> None:
    assert normalize_station_name(a) == normalize_station_name(b)


def test_normalize_station_name_keeps_different_stations_apart() -> None:
    # all four are real, distinct GTFS station names
    assert normalize_station_name("8 Av") != normalize_station_name("8 St-NYU")
    assert normalize_station_name("Court Sq") != normalize_station_name("Court St")


@pytest.mark.parametrize(
    ("route_id", "expected"), [("6X", "6"), ("FX", "F"), ("GS", "S"), ("SI", "S"), ("Q", "Q")]
)
def test_canonical_route(route_id: str, expected: str) -> None:
    assert canonical_route(route_id) == expected


def test_unknown_equipment_type_is_dropped_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Synthetic: a real row mutated to a type the frozen enum has no member for.

    Dropping (rather than coercing to "elevator") is deliberate: telling a wheelchair
    user a moving walkway is a lift would be worse than omitting one row, and the drop
    is counted and logged. If every row were unusable the feed raises instead.
    """
    good = _first_fixture_row()
    bad = dict(good, equipment="XX999", equipmenttype="MW")
    with caplog.at_level("WARNING"):
        parsed = parse_outage_rows([good, bad])
    assert [o.equipment_id for o in parsed.outages] == [good["equipment"]]
    assert parsed.dropped_unknown_type == 1
    assert "MW" in caplog.text


def test_rows_without_an_equipment_id_are_dropped() -> None:
    row = dict(_first_fixture_row(), equipment="")
    parsed = parse_outage_rows([row])
    assert parsed.outages == [] and parsed.dropped_missing_id == 1


def test_all_rows_unusable_is_an_upstream_fault() -> None:
    row = dict(_first_fixture_row(), equipmenttype="MW")
    with pytest.raises(FeedUnavailable) as exc:
        parse_outages(json.dumps([row]).encode(), url="u")
    assert exc.value.kind is ErrorKind.UPSTREAM_PARSE


def _first_fixture_row() -> dict[str, object]:
    rows = json.loads(_fixture_bytes(ENE_FIXTURE))
    assert isinstance(rows, list) and rows
    return dict(rows[0])


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_recorded_nosuchkey_body_with_http_200_is_not_found(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    """MTA answers a wrong key with HTTP *200* and an S3 error XML; that must be fatal."""
    _mock_upstreams(
        mock, settings, ene=httpx.Response(200, content=_fixture_bytes(NOSUCHKEY_FIXTURE))
    )
    with pytest.raises(FeedUnavailable) as exc:
        await ElevatorOutagesAdapter(client=client, settings=settings).fetch()
    assert exc.value.kind is ErrorKind.NOT_FOUND
    assert ENE_SLUG in exc.value.message
    assert exc.value.upstream_status == 200


async def test_http_404_is_fatal(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_upstreams(mock, settings, ene=httpx.Response(404, text="nope"))
    with pytest.raises(FeedUnavailable) as exc:
        await ElevatorOutagesAdapter(client=client, settings=settings).fetch()
    assert exc.value.kind is ErrorKind.NOT_FOUND


async def test_non_json_body_is_a_parse_error(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_upstreams(mock, settings, ene=httpx.Response(200, content=b"not json at all"))
    with pytest.raises(FeedUnavailable) as exc:
        await ElevatorOutagesAdapter(client=client, settings=settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_PARSE


async def test_json_object_instead_of_array_is_a_parse_error(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_upstreams(mock, settings, ene=httpx.Response(200, content=b'{"outages": []}'))
    with pytest.raises(FeedUnavailable) as exc:
        await ElevatorOutagesAdapter(client=client, settings=settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_PARSE


async def test_outages_survive_a_dead_static_gtfs_bundle(
    mock: respx.MockRouter,
    client: httpx.AsyncClient,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stops join is enrichment; the accessibility feed must not go dark with it."""
    _mock_upstreams(mock, settings, stops=httpx.Response(500, text="boom"))
    with caplog.at_level("WARNING"):
        snap = await ElevatorOutagesAdapter(client=client, settings=settings).fetch()
    assert len(snap.records) == 11
    assert all(o.lat is None and o.lon is None for o in snap.records)
    assert "static GTFS stops unavailable" in caplog.text


async def test_upstream_error_never_yields_an_empty_snapshot(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    _mock_upstreams(mock, settings, ene=httpx.Response(503, text="unavailable"))
    with pytest.raises(FeedUnavailable) as exc:
        await ElevatorOutagesAdapter(client=client, settings=settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_HTTP


# ---------------------------------------------------------------------------
# Caching / protocol
# ---------------------------------------------------------------------------


async def test_second_fetch_within_ttl_touches_neither_upstream(
    mock: respx.MockRouter, client: httpx.AsyncClient, settings: Settings
) -> None:
    ene_route, stops_route = _mock_upstreams(mock, settings)
    adapter = ElevatorOutagesAdapter(client=client, settings=settings)
    first = await adapter.fetch()
    second = await adapter.fetch()
    assert ene_route.call_count == 1  # shared RawBytesCache, 5 min TTL
    assert stops_route.call_count == 1  # station index cached to the stops snapshot TTL
    assert [o.equipment_id for o in first.records] == [o.equipment_id for o in second.records]


async def test_adapter_shape(client: httpx.AsyncClient, settings: Settings) -> None:
    adapter = ElevatorOutagesAdapter(client=client, settings=settings)
    assert isinstance(adapter, FeedAdapter)
    assert adapter.is_configured() is True  # keyless feed
    assert adapter.name is FeedName.MTA_ELEVATOR_OUTAGES
    assert adapter.ttl == DEFAULT_TTL[FeedName.MTA_ELEVATOR_OUTAGES] == timedelta(minutes=5)
    assert adapter.source_url == outages_url(settings.mta_gtfs_base)


def test_station_complex_radius_is_below_the_real_false_positive() -> None:
    """Gun Hill Rd's two stations are 1,910 m apart; the widest real complex is 339 m."""
    assert 339 < STATION_COMPLEX_RADIUS_M < 1910
