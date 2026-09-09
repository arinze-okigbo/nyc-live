"""NY511EventsAdapter: unit tests for the parsers, a fixture replay, and one live test.

The fixture (`tests/fixtures/cameras/ny511_events.json`) is a trimmed copy of a real
`GET https://511ny.org/api/getevents?format=json` response recorded on 2026-09-09 --
see `tests/fixtures/cameras/RECORD.md` for the exact command. Nothing here is invented.
"""

from __future__ import annotations

import importlib
import json
import logging
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
import respx

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    ErrorKind,
    FeedAdapter,
    FeedName,
    FeedUnavailable,
    NY511Event,
    NY511Severity,
)
from nyc_live.feeds import ADAPTER_SPECS, load_adapters
from nyc_live.feeds.ny511 import (
    POLYLINE_PRECISION,
    NY511EventsAdapter,
    decode_polyline,
    event_points,
    map_ny511_event,
    parse_ny511_event_list,
    parse_ny511_severity,
    parse_ny511_timestamp,
    unrecognized_severity,
)
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import make_client

EVENTS_URL = "https://511ny.test/api/getevents"
FIXTURE_NAME = "ny511_events.json"

# One real row from the recorded fixture, kept inline so the error-path tests can
# mutate a single field without hand-writing a fake event.
NYC_ROW: dict[str, object] = {
    "LastUpdated": "27/07/2026 16:26:08",
    "Latitude": 40.702773,
    "Longitude": -74.012347,
    "PlannedEndDate": "22/10/2026 23:59:00",
    "Reported": "27/07/2026 00:00:00",
    "StartDate": "27/07/2026 00:00:00",
    "ID": "TRANSCOM-ORC300565110",
    "RegionName": "New York City Area",
    "CountyName": "New York",
    "Severity": "Unknown",
    "RoadwayName": "MOORE ST",
    "DirectionOfTravel": "Northbound",
    "Description": (
        "Construction on MOORE ST northbound between WATER ST (New York) and PEARL ST "
        "(New York) for Water Street streetscape improvements., Continuous Monday July "
        "27th, 2026 12:00 AM thru Thursday October 22nd, 2026 11:59 PM All lanes closed"
    ),
    "LanesAffected": "No Data",
    "EventType": "closures",
    "EventSubType": "roadwork",
    # fields the frozen contract has no home for; must be ignored, not smuggled through
    "Location": "WATER ST|PEARL ST",
    "MapEncodedPolyline": None,
    "NavteqLinkId": "21638180",
    "Schedule": [],
}


@pytest.fixture
def ev_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"http_retries": 0, "ny511_api_key": None})


@pytest.fixture
async def client(ev_settings: Settings):
    async with make_client(ev_settings) as c:
        yield c


def adapter(client: httpx.AsyncClient, settings: Settings) -> NY511EventsAdapter:
    return NY511EventsAdapter(client=client, settings=settings, base_url=EVENTS_URL)


# ---------------------------------------------------------------------------
# Timestamps: DD/MM/YYYY, New York local -> aware UTC
# ---------------------------------------------------------------------------


def test_parses_day_first_not_month_first() -> None:
    # 22/10/2026 is October 22nd; under MM/DD it would not parse at all.
    assert parse_ny511_timestamp("22/10/2026 23:59:00") == datetime(2026, 10, 23, 3, 59, tzinfo=UTC)


def test_localises_to_new_york_not_utc() -> None:
    # EDT (UTC-4) in September, EST (UTC-5) in January: the offset must follow the zone.
    assert parse_ny511_timestamp("09/09/2026 15:50:08") == datetime(
        2026, 9, 9, 19, 50, 8, tzinfo=UTC
    )
    assert parse_ny511_timestamp("05/01/2025 12:23:38") == datetime(
        2025, 1, 5, 17, 23, 38, tzinfo=UTC
    )


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_timestamp_is_none_not_epoch(raw: str | None) -> None:
    assert parse_ny511_timestamp(raw) is None


@pytest.mark.parametrize("raw", ["2026-10-22T23:59:00", "22-10-2026 23:59:00", "garbage", 17])
def test_unparseable_timestamp_raises(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_ny511_timestamp(raw)


# ---------------------------------------------------------------------------
# Severity normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Minor", NY511Severity.MINOR),
        ("moderate", NY511Severity.MODERATE),
        ("MAJOR", NY511Severity.MAJOR),
        ("Unknown", NY511Severity.UNKNOWN),
        ("None", NY511Severity.UNKNOWN),  # a real value: 1 of 2,420 events on 2026-09-09
        ("", NY511Severity.UNKNOWN),
        (None, NY511Severity.UNKNOWN),
    ],
)
def test_severity_maps_case_insensitively(raw: object, expected: NY511Severity) -> None:
    assert parse_ny511_severity(raw) is expected


def test_unrecognized_severity_degrades_to_unknown_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="nyc_live.feeds.ny511")
    assert parse_ny511_severity("Catastrophic") is NY511Severity.UNKNOWN
    assert "Catastrophic" in caplog.text


@pytest.mark.parametrize("known", ["Minor", "moderate", "MAJOR", "Unknown", "None", "", None])
def test_known_severities_are_not_flagged_as_drift(known: object) -> None:
    assert unrecognized_severity(known) is None


def test_numeric_severity_is_flagged_not_guessed() -> None:
    """511NY grew a third publisher (NITTEC) emitting `Severity: "1"` on 2026-09-09.

    Its scale is undocumented, so "1" must not be read as "minor" -- that would
    invent a severity rank. It is reported as drift and recorded as unknown.
    """
    assert unrecognized_severity("1") == "1"
    assert parse_ny511_severity("1") is NY511Severity.UNKNOWN


def test_severity_drift_is_one_warning_per_fetch_not_one_per_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="nyc_live.feeds.ny511")
    rows = [{**NYC_ROW, "ID": f"NITTEC-{i}", "Severity": "1"} for i in range(50)]
    kept, _, _ = parse_ny511_event_list(rows)
    assert len(kept) == 50
    assert all(e.severity is NY511Severity.UNKNOWN for e in kept)
    warnings = [r for r in caplog.records if "severity outside" in r.getMessage()]
    assert len(warnings) == 1
    assert "50" in caplog.text and "'1': 50" in caplog.text


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def test_maps_a_real_row_onto_the_contract() -> None:
    event = map_ny511_event(NYC_ROW)
    assert isinstance(event, NY511Event)
    assert event.id == "TRANSCOM-ORC300565110"
    assert event.event_type == "closures" and event.event_subtype == "roadwork"
    assert event.severity is NY511Severity.UNKNOWN
    assert event.roadway == "MOORE ST" and event.direction == "Northbound"
    assert event.county == "New York"
    assert event.lanes_affected is None  # "No Data" is 511NY's sentinel for absent
    assert event.started_at == datetime(2026, 7, 27, 4, 0, tzinfo=UTC)
    assert event.planned_end == datetime(2026, 10, 23, 3, 59, tzinfo=UTC)
    assert event.last_updated == datetime(2026, 7, 27, 20, 26, 8, tzinfo=UTC)
    assert (event.lat, event.lon) == (40.702773, -74.012347)
    assert event.points == []  # this row's MapEncodedPolyline is null, as all NYC rows' are


def test_lanes_affected_keeps_a_real_status() -> None:
    row = {**NYC_ROW, "LanesAffected": "1 Left lane closed"}
    assert map_ny511_event(row).lanes_affected == "1 Left lane closed"


@pytest.mark.parametrize("blank", ["", None])
def test_blank_optional_fields_become_none(blank: str | None) -> None:
    row = {**NYC_ROW, "DirectionOfTravel": blank, "CountyName": blank, "EventSubType": blank}
    event = map_ny511_event(row)
    assert event.direction is None and event.county is None and event.event_subtype is None


@pytest.mark.parametrize("field", ["ID", "EventType", "Description"])
def test_missing_required_field_raises(field: str) -> None:
    row = {k: v for k, v in NYC_ROW.items() if k != field}
    with pytest.raises(ValueError):
        map_ny511_event(row)


def test_missing_coordinates_raise() -> None:
    with pytest.raises(ValueError):
        map_ny511_event({**NYC_ROW, "Latitude": None})


def test_unmappable_rows_are_skipped_and_counted() -> None:
    rows = [NYC_ROW, {"ID": "X"}, "not-an-object", {**NYC_ROW, "ID": "TRANSCOM-ORC300565110"}]
    kept, skipped, dropped = parse_ny511_event_list(rows)
    assert [e.id for e in kept] == ["TRANSCOM-ORC300565110"]  # duplicate id collapsed
    assert (skipped, dropped) == (2, 0)


# ---------------------------------------------------------------------------
# MapEncodedPolyline -> points
# ---------------------------------------------------------------------------


def polyline_rows(recorded: list[dict[str, object]]) -> list[dict[str, object]]:
    return [r for r in recorded if isinstance(r.get("MapEncodedPolyline"), str)]


def test_polyline_precision_is_five_not_six(recorded_events: list[dict[str, object]]) -> None:
    """The one way this feature can be silently, catastrophically wrong.

    Decoding a real polyline at 1e5 must land on the event's own coordinates; at
    1e6 the same bytes land ~39 deg away -- plottable, and completely false.
    """
    rows = polyline_rows(recorded_events)
    assert len(rows) == 4, "fixture no longer carries a real polyline to pin the precision"
    assert POLYLINE_PRECISION == 5

    for row in rows:
        encoded = str(row["MapEncodedPolyline"])
        lat, lon = float(row["Latitude"]), float(row["Longitude"])  # type: ignore[arg-type]

        at_five = decode_polyline(encoded, precision=5)
        assert at_five, f"{row['ID']} decoded to nothing"
        assert abs(at_five[0][0] - lat) < 0.02
        assert abs(at_five[0][1] - lon) < 0.02

        at_six = decode_polyline(encoded, precision=6)
        assert abs(at_six[0][0] - lat) > 1.0, "precision 6 must be visibly wrong, not close"


def test_decoded_line_is_ordered_and_contiguous(recorded_events: list[dict[str, object]]) -> None:
    """Vertices are deltas: consecutive points must be metres apart, not scattered."""
    row = next(r for r in polyline_rows(recorded_events) if r["ID"] == "TRAVELIQ-1593")
    points = decode_polyline(str(row["MapEncodedPolyline"]))
    assert len(points) == 30
    assert all(len(p) == 2 for p in points)
    hops = [max(abs(b[0] - a[0]), abs(b[1] - a[1])) for a, b in pairwise(points)]
    assert max(hops) < 0.01, f"largest hop {max(hops)} deg is not a contiguous road segment"


def test_event_points_decodes_a_real_row(recorded_events: list[dict[str, object]]) -> None:
    row = next(r for r in polyline_rows(recorded_events) if r["ID"] == "TRAVELIQ-1593")
    event = map_ny511_event(row)
    assert len(event.points) == 30
    assert event.points[0] == pytest.approx((event.lat, event.lon), abs=0.001)


@pytest.mark.parametrize("raw", [None, "", "   ", 12345, [], {"a": 1}])
def test_absent_or_non_string_polyline_is_empty_points(raw: object) -> None:
    assert event_points(raw, lat=40.7, lon=-74.0, event_id="X") == []


@pytest.mark.parametrize("junk", ["!!!!", "egufG", "~~~~~~~~~~~~"])
def test_undecodable_polyline_drops_the_line_not_the_event(
    junk: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="nyc_live.feeds.ny511")
    event = map_ny511_event({**NYC_ROW, "MapEncodedPolyline": junk})
    assert event.id == "TRANSCOM-ORC300565110"  # the event survives
    assert event.points == []


def test_polyline_far_from_its_own_event_is_rejected(
    recorded_events: list[dict[str, object]], caplog: pytest.LogCaptureFixture
) -> None:
    """A line that decodes fine but lands elsewhere is not this event's geometry."""
    caplog.set_level(logging.WARNING, logger="nyc_live.feeds.ny511")
    upstate = next(r for r in polyline_rows(recorded_events) if r["ID"] == "TRAVELIQ-1593")
    # a real polyline, attached to an event 300 miles away
    event = map_ny511_event({**NYC_ROW, "MapEncodedPolyline": upstate["MapEncodedPolyline"]})
    assert event.points == []
    assert "dropping the line" in caplog.text


# ---------------------------------------------------------------------------
# Adapter behaviour (respx, inline bodies)
# ---------------------------------------------------------------------------


def test_satisfies_the_adapter_protocol(client: httpx.AsyncClient, ev_settings: Settings) -> None:
    a = adapter(client, ev_settings)
    assert isinstance(a, FeedAdapter)
    assert a.name is FeedName.NY511_EVENTS
    assert a.ttl == DEFAULT_TTL[FeedName.NY511_EVENTS] == timedelta(minutes=2)


def test_registered_under_the_name_the_registry_loads(
    client: httpx.AsyncClient, ev_settings: Settings
) -> None:
    spec = next(s for s in ADAPTER_SPECS if s.feed is FeedName.NY511_EVENTS)
    assert getattr(importlib.import_module(spec.module), spec.cls) is NY511EventsAdapter
    loaded = load_adapters(client, ev_settings)
    assert any(isinstance(a, NY511EventsAdapter) for a in loaded)


def test_url_comes_from_settings_not_a_hardcoded_constant(
    client: httpx.AsyncClient, ev_settings: Settings
) -> None:
    """Env-overridable like every other upstream (NYC_LIVE_NY511_EVENTS_URL)."""
    assert ev_settings.ny511_events_url == "https://511ny.org/api/getevents"
    default = NY511EventsAdapter(client=client, settings=ev_settings)
    assert default.source_url == ev_settings.ny511_events_url

    redirected = ev_settings.model_copy(update={"ny511_events_url": "https://mirror.test/events"})
    assert NY511EventsAdapter(client=client, settings=redirected).source_url == (
        "https://mirror.test/events"
    )


def test_is_configured_without_a_key(client: httpx.AsyncClient, ev_settings: Settings) -> None:
    """Not key-gated: getevents serves the full feed keylessly and ignores `key`."""
    assert adapter(client, ev_settings).is_configured() is True
    assert adapter(client, ev_settings).request_params() == {"format": "json"}


def test_key_is_sent_when_configured(client: httpx.AsyncClient, ev_settings: Settings) -> None:
    keyed = ev_settings.model_copy(update={"ny511_api_key": "  abc123  "})
    assert adapter(client, keyed).request_params() == {"format": "json", "key": "abc123"}


async def test_fetch_filters_to_nyc_and_logs_the_drop(
    client: httpx.AsyncClient,
    ev_settings: Settings,
    respx_mock: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="nyc_live.feeds.ny511")
    albany = {**NYC_ROW, "ID": "TRAVELIQ-1593", "Latitude": 43.2038, "Longitude": -77.5601}
    route = respx_mock.get(EVENTS_URL).mock(
        return_value=httpx.Response(200, json=[NYC_ROW, albany])
    )

    snap = await adapter(client, ev_settings).fetch()

    assert route.call_count == 1
    assert route.calls.last.request.url.params["format"] == "json"
    assert "key" not in route.calls.last.request.url.params
    assert snap.feed is FeedName.NY511_EVENTS
    assert snap.source_url == EVENTS_URL
    assert snap.stale_after - snap.fetched_at == DEFAULT_TTL[FeedName.NY511_EVENTS]
    assert snap.latency_ms is not None
    assert [e.id for e in snap.records] == ["TRANSCOM-ORC300565110"]
    assert "dropped 1 of 2 events outside the NYC bbox" in caplog.text


async def test_zero_nyc_events_raises_rather_than_returning_empty(
    client: httpx.AsyncClient, ev_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    albany = {**NYC_ROW, "ID": "TRAVELIQ-1593", "Latitude": 43.2038, "Longitude": -77.5601}
    respx_mock.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=[albany]))
    with pytest.raises(FeedUnavailable) as exc:
        await adapter(client, ev_settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_PARSE
    assert "outside_bbox=1" in exc.value.message


async def test_non_array_body_is_a_parse_error(
    client: httpx.AsyncClient, ev_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"events": []}))
    with pytest.raises(FeedUnavailable) as exc:
        await adapter(client, ev_settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_PARSE


async def test_non_json_body_is_a_parse_error(
    client: httpx.AsyncClient, ev_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(EVENTS_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(FeedUnavailable) as exc:
        await adapter(client, ev_settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_PARSE


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_is_loud_and_names_the_env_var(
    client: httpx.AsyncClient,
    ev_settings: Settings,
    respx_mock: respx.MockRouter,
    status: int,
) -> None:
    """If 511NY ever starts enforcing keys here, the layer goes red -- it never empties,
    and it is not mislabelled NOT_CONFIGURED (which smoke/health treat as a benign skip)."""
    respx_mock.get(EVENTS_URL).mock(return_value=httpx.Response(status, text="denied"))
    with pytest.raises(FeedUnavailable) as exc:
        await adapter(client, ev_settings).fetch()
    assert exc.value.kind is ErrorKind.UPSTREAM_HTTP
    assert exc.value.upstream_status == status
    assert "NY511_API_KEY" in exc.value.message


async def test_404_is_fatal(
    client: httpx.AsyncClient, ev_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(EVENTS_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(FeedUnavailable) as exc:
        await adapter(client, ev_settings).fetch()
    assert exc.value.kind is ErrorKind.NOT_FOUND


async def test_failure_does_not_hold_the_cadence_floor(
    client: httpx.AsyncClient, ev_settings: Settings, respx_mock: respx.MockRouter
) -> None:
    """A 2-minute limiter must not make the retry after a failure sleep two minutes."""
    route = respx_mock.get(EVENTS_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=[NYC_ROW])]
    )
    a = adapter(client, ev_settings)
    with pytest.raises(FeedUnavailable):
        await a.fetch()
    snap = await a.fetch()  # would block for the whole TTL if the floor were held
    assert route.call_count == 2
    assert len(snap.records) == 1


# ---------------------------------------------------------------------------
# Fixture replay: a trimmed copy of the real 2026-09-09 response
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded_events(fixtures_dir: Path) -> list[dict[str, object]]:
    path = fixtures_dir / "cameras" / FIXTURE_NAME
    if not path.exists():  # pragma: no cover - fixture is checked in
        pytest.skip(f"missing fixture {path}; see tests/fixtures/cameras/RECORD.md")
    rows = json.loads(path.read_text())
    assert isinstance(rows, list) and rows
    return rows


async def test_replays_the_recorded_statewide_response(
    client: httpx.AsyncClient,
    ev_settings: Settings,
    respx_mock: respx.MockRouter,
    recorded_events: list[dict[str, object]],
) -> None:
    respx_mock.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=recorded_events))

    snap = await adapter(client, ev_settings).fetch()

    # 20 recorded rows: 16 inside the NYC bbox, 4 upstate (Monroe County).
    assert len(recorded_events) == 20
    assert len(snap.records) == 16
    assert all(in_nyc_bbox(e.lat, e.lon) for e in snap.records)
    assert len({e.id for e in snap.records}) == len(snap.records)
    assert all(e.description for e in snap.records)
    assert all(e.event_type for e in snap.records)
    # every recorded row carries LastUpdated/StartDate; PlannedEndDate is often blank
    assert all(e.last_updated is not None and e.started_at is not None for e in snap.records)
    assert any(e.planned_end is None for e in snap.records)
    assert any(e.planned_end is not None for e in snap.records)
    # the recorded set spans all six of 511NY's event types
    assert {e.event_type for e in snap.records} == {
        "accidentsAndIncidents",
        "closures",
        "generalInfo",
        "roadwork",
        "specialEvents",
        "transitOperations",
    }
    assert {e.severity for e in snap.records} >= {NY511Severity.UNKNOWN, NY511Severity.MINOR}
    # "No Data" never reaches a record; a real lane status does
    assert all(e.lanes_affected != "No Data" for e in snap.records)
    assert any(e.lanes_affected for e in snap.records)
    # Polylines: every NYC row in the recording is TRANSCOM-sourced with
    # MapEncodedPolyline null, so `points` is empty and the consumer falls back to
    # the pin. The four rows that DO carry a line are the upstate TRAVELIQ ones,
    # dropped by the bbox -- they are decoded in the polyline tests above.
    assert all(e.points == [] for e in snap.records)
    assert all(
        r["MapEncodedPolyline"] is None for r in recorded_events if "TRANSCOM" in str(r["ID"])
    )


def test_recorded_timestamps_are_day_first(recorded_events: list[dict[str, object]]) -> None:
    """Guards the one silent failure mode: a day <= 12 parses under both readings."""
    days = {str(r["LastUpdated"]).split("/")[0] for r in recorded_events}
    months = {str(r["LastUpdated"]).split("/")[1] for r in recorded_events}
    assert max(int(m) for m in months) <= 12
    assert any(int(d) > 12 for d in days), "fixture no longer proves the day-first ordering"


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_ny511_events(settings: Settings) -> None:
    async with make_client(settings) as client:
        snap = await NY511EventsAdapter(client=client, settings=settings).fetch()

    assert snap.feed is FeedName.NY511_EVENTS
    assert snap.source_url == settings.ny511_events_url == "https://511ny.org/api/getevents"
    # ~890 of ~2,420 statewide events were inside the bbox on 2026-09-09; this is a
    # floor loose enough for a quiet night, tight enough to catch a broken filter.
    assert len(snap.records) > 200, f"only {len(snap.records)} NYC events"
    assert len({e.id for e in snap.records}) == len(snap.records)
    assert all(in_nyc_bbox(e.lat, e.lon) for e in snap.records)
    assert all(e.description and e.event_type for e in snap.records)
    assert all(isinstance(e.severity, NY511Severity) for e in snap.records)
    assert all(e.lanes_affected != "No Data" for e in snap.records)

    # 511NY's real event vocabulary; a rename here is a real upstream change.
    assert {e.event_type for e in snap.records} <= {
        "accidentsAndIncidents",
        "closures",
        "generalInfo",
        "roadwork",
        "specialEvents",
        "transitOperations",
    }
    assert {"roadwork", "closures"} <= {e.event_type for e in snap.records}

    # Timestamps are real, aware, and sane: something was updated in the last day,
    # and nothing claims to have been updated far in the future.
    updated = [e.last_updated for e in snap.records if e.last_updated is not None]
    assert len(updated) > len(snap.records) * 0.9
    assert all(u.tzinfo is not None for u in updated)
    assert max(updated) > snap.fetched_at - timedelta(days=1)
    assert max(updated) < snap.fetched_at + timedelta(minutes=5)

    # Any line that survives decoding is real geometry for its own event: it starts
    # on the event's point and stays inside the city. Deliberately not asserting that
    # any exist -- as of 2026-09-09 every NYC event is TRANSCOM-sourced and publishes
    # a null polyline, so this is a correctness guard that arms itself if that changes.
    for event in snap.records:
        if not event.points:
            continue
        assert event.points[0] == pytest.approx((event.lat, event.lon), abs=0.5)
        assert all(in_nyc_bbox(lat, lon) for lat, lon in event.points)

    assert snap.stale_after - snap.fetched_at == DEFAULT_TTL[FeedName.NY511_EVENTS]
    assert snap.latency_ms is not None and snap.latency_ms > 0
