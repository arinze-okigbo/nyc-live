"""MTA Bus Time adapter: key-gated skip path, SIRI VehicleMonitoring mapping, live check."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from nyc_live.cache import CachedFeed
from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    BusVehicle,
    ErrorKind,
    FeedAdapter,
    FeedName,
    FeedNotConfigured,
    FeedUnavailable,
)
from nyc_live.feeds import ADAPTER_SPECS, load_adapters
from nyc_live.feeds.bus import (
    BUS_TIME_ENV_VAR,
    BUS_TIME_VEHICLE_MONITORING_URL,
    BusPositionsAdapter,
)
from nyc_live.http import make_client

FAKE_KEY = "fake-bustime-key-8f3a1c2b-do-not-leak"


def _settings(tmp_path: Path, key: str | None, base: str | None = None) -> Settings:
    kwargs: dict[str, object] = {
        "_env_file": None,
        "NYC_LIVE_DATA_DIR": tmp_path / "data",
        "MTA_BUS_TIME_API_KEY": key,
        "NYC_LIVE_HTTP_RETRIES": 0,
    }
    if base is not None:
        kwargs["NYC_LIVE_MTA_BUS_TIME_BASE"] = base
    return Settings(**kwargs)  # type: ignore[arg-type]


# A trimmed, real VehicleMonitoring shape (fields renamed/values changed; not upstream
# data verbatim, but every key here was observed live on 2026-09-08).
def _delivery(*activities: dict[str, object]) -> dict[str, object]:
    return {
        "Siri": {
            "ServiceDelivery": {
                "ResponseTimestamp": "2026-09-08T22:03:48.742-04:00",
                "VehicleMonitoringDelivery": [
                    {
                        "ResponseTimestamp": "2026-09-08T22:03:48.742-04:00",
                        "VehicleActivity": list(activities),
                    }
                ],
            }
        }
    }


def _activity(
    *,
    vehicle_ref: str = "MTA NYCT_7516",
    line_ref: str | None = "MTA NYCT_B54",
    lat: float = 40.699792,
    lon: float = -73.911946,
    bearing: float | None = 45.5,
    trip_ref: str | None = "MTA NYCT_FP_D6-Weekday-128100_B54_615",
    recorded_at: str | None = "2026-09-08T22:03:27.000-04:00",
) -> dict[str, object]:
    mvj: dict[str, object] = {
        "VehicleRef": vehicle_ref,
        "VehicleLocation": {"Latitude": lat, "Longitude": lon},
    }
    if line_ref is not None:
        mvj["LineRef"] = line_ref
    if bearing is not None:
        mvj["Bearing"] = bearing
    if trip_ref is not None:
        mvj["FramedVehicleJourneyRef"] = {"DatedVehicleJourneyRef": trip_ref}
    activity: dict[str, object] = {"MonitoredVehicleJourney": mvj}
    if recorded_at is not None:
        activity["RecordedAtTime"] = recorded_at
    return activity


async def test_bus_skip_path_without_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path, None)
    async with make_client(settings) as client:
        adapter = BusPositionsAdapter(client=client, settings=settings)
        assert isinstance(adapter, FeedAdapter)
        assert adapter.name is FeedName.MTA_BUS
        assert adapter.ttl == DEFAULT_TTL[FeedName.MTA_BUS]
        assert adapter.is_configured() is False
        with pytest.raises(FeedNotConfigured) as excinfo:
            await adapter.fetch()
        assert excinfo.value.kind is ErrorKind.NOT_CONFIGURED
        assert excinfo.value.env_var == BUS_TIME_ENV_VAR
        assert BUS_TIME_ENV_VAR in excinfo.value.message

        # the cache layer turns the skip into an honest error envelope, never empty-fresh
        feed: CachedFeed[BusVehicle] = CachedFeed(adapter)
        env = await feed.get()
        assert env.status == "error" and env.records == []
        assert env.error is not None and env.error.kind is ErrorKind.NOT_CONFIGURED
        assert feed.health().configured is False


async def test_bus_blank_key_counts_as_unconfigured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "   ")
    async with make_client(settings) as client:
        adapter = BusPositionsAdapter(client=client, settings=settings)
        assert adapter.is_configured() is False
        with pytest.raises(FeedNotConfigured):
            await adapter.fetch()


async def test_bus_is_registered_and_loadable(tmp_path: Path) -> None:
    spec = next(s for s in ADAPTER_SPECS if s.feed is FeedName.MTA_BUS)
    assert (spec.module, spec.cls) == ("nyc_live.feeds.bus", "BusPositionsAdapter")
    settings = _settings(tmp_path, None)
    async with make_client(settings) as client:
        adapters = load_adapters(client, settings)
    bus = [a for a in adapters if a.name is FeedName.MTA_BUS]
    assert len(bus) == 1 and bus[0].is_configured() is False


async def test_bus_source_url_defaults_to_documented_bustime_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NYC_LIVE_MTA_BUS_TIME_BASE", raising=False)
    settings = _settings(tmp_path, None)
    async with make_client(settings) as client:
        adapter = BusPositionsAdapter(client=client, settings=settings)
        assert adapter.source_url == "https://bustime.mta.info/api/siri/vehicle-monitoring.json"
        assert adapter.source_url == BUS_TIME_VEHICLE_MONITORING_URL


async def test_bus_source_url_follows_settings_override(tmp_path: Path) -> None:
    """Regression: source_url must be resolved from Settings when read, not bound at import."""
    base = "http://127.0.0.1:9/siri"
    settings = _settings(tmp_path, None, base=base)
    async with make_client(settings) as client:
        adapter = BusPositionsAdapter(client=client, settings=settings)
        assert adapter.source_url == "http://127.0.0.1:9/siri/vehicle-monitoring.json"
        assert "bustime.mta.info" not in adapter.source_url

        # a trailing slash on the base must not double up
        slashed = _settings(tmp_path, None, base=base + "/")
        assert BusPositionsAdapter(client=client, settings=slashed).source_url == adapter.source_url


async def test_bus_configured_path_maps_real_shape_and_drops_bad_rows(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    base = "http://127.0.0.1:9/siri"
    settings = _settings(tmp_path, FAKE_KEY, base=base)
    url = "http://127.0.0.1:9/siri/vehicle-monitoring.json"
    body = _delivery(
        _activity(),  # kept
        _activity(vehicle_ref="MTA NYCT_1", lat=90.0, lon=-73.9),  # out of NYC bbox
        {"MonitoredVehicleJourney": {"VehicleRef": "MTA NYCT_2"}},  # no VehicleLocation
        _activity(vehicle_ref="MTA NYCT_3", trip_ref=None, bearing=None),  # optionals absent
    )
    route = respx_mock.get(url, params={"key": FAKE_KEY, "version": "2"}).mock(
        return_value=httpx.Response(200, json=body)
    )
    async with make_client(settings) as client:
        adapter = BusPositionsAdapter(client=client, settings=settings)
        assert adapter.is_configured() is True
        snap = await adapter.fetch()

    assert route.call_count == 1
    assert snap.feed is FeedName.MTA_BUS
    assert snap.source_url == url  # key never appears in source_url
    assert FAKE_KEY not in snap.source_url

    by_id = {v.vehicle_id: v for v in snap.records}
    assert set(by_id) == {"MTA NYCT_7516", "MTA NYCT_3"}

    full = by_id["MTA NYCT_7516"]
    assert full.route_id == "MTA NYCT_B54"
    assert full.trip_id == "MTA NYCT_FP_D6-Weekday-128100_B54_615"
    assert full.bearing == 45.5
    assert full.timestamp is not None and full.timestamp.utcoffset() is not None

    minimal = by_id["MTA NYCT_3"]
    assert minimal.trip_id is None and minimal.bearing is None

    assert snap.upstream_generated_at is not None


async def test_bus_all_rows_dropped_is_an_upstream_fault(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """MTA buses are never all offline; zero mappable vehicles must not look like success."""
    base = "http://127.0.0.1:9/siri"
    settings = _settings(tmp_path, FAKE_KEY, base=base)
    url = "http://127.0.0.1:9/siri/vehicle-monitoring.json"
    body = _delivery({"MonitoredVehicleJourney": {"VehicleRef": "MTA NYCT_1"}})  # no location
    respx_mock.get(url, params={"key": FAKE_KEY, "version": "2"}).mock(
        return_value=httpx.Response(200, json=body)
    )
    async with make_client(settings) as client:
        adapter = BusPositionsAdapter(client=client, settings=settings)
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()
    assert excinfo.value.kind is ErrorKind.UPSTREAM_PARSE


async def test_bus_error_condition_is_reported_and_omits_key(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """An invalid key comes back as HTTP 200 with an embedded SIRI ErrorCondition."""
    base = "http://127.0.0.1:9/siri"
    settings = _settings(tmp_path, FAKE_KEY, base=base)
    url = "http://127.0.0.1:9/siri/vehicle-monitoring.json"
    body = {
        "Siri": {
            "ServiceDelivery": {
                "ResponseTimestamp": "2026-09-08T22:05:06.841-04:00",
                "VehicleMonitoringDelivery": [
                    {
                        "ResponseTimestamp": "2026-09-08T22:05:06.841-04:00",
                        "ErrorCondition": {
                            "OtherError": {"ErrorText": "API key is not authorized."},
                            "Description": "API key is not authorized.",
                        },
                    }
                ],
            }
        }
    }
    respx_mock.get(url, params={"key": FAKE_KEY, "version": "2"}).mock(
        return_value=httpx.Response(200, json=body)
    )
    async with make_client(settings) as client:
        adapter = BusPositionsAdapter(client=client, settings=settings)
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()
    err = excinfo.value
    assert err.kind is ErrorKind.UPSTREAM_HTTP
    assert "not authorized" in err.message
    for blob in (err.url or "", err.message, str(err), repr(err)):
        assert FAKE_KEY not in blob


async def test_bus_never_leaks_the_key_on_a_transport_failure(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    base = "http://127.0.0.1:9/siri"
    settings = _settings(tmp_path, FAKE_KEY, base=base)
    url = "http://127.0.0.1:9/siri/vehicle-monitoring.json"
    respx_mock.get(url, params={"key": FAKE_KEY, "version": "2"}).mock(
        return_value=httpx.Response(503)
    )
    async with make_client(settings) as client:
        adapter = BusPositionsAdapter(client=client, settings=settings)
        assert FAKE_KEY not in adapter.source_url
        assert "key=" not in adapter.source_url
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()
        err = excinfo.value
        for blob in (err.url or "", err.message, str(err), repr(err)):
            assert FAKE_KEY not in blob

        feed: CachedFeed[BusVehicle] = CachedFeed(adapter)
        env = await feed.get()
        assert env.status == "error" and env.records == []
        assert FAKE_KEY not in env.model_dump_json()
        assert FAKE_KEY not in str(feed.health().model_dump())


@pytest.mark.live
async def test_live_bus_positions(settings: Settings) -> None:
    """Hits the real MTA Bus Time API. Skipped (not failed) if no key is configured."""
    real_settings = Settings(NYC_LIVE_DATA_DIR=settings.data_dir)  # reads the real .env
    if not (real_settings.mta_bus_time_api_key or "").strip():
        pytest.skip("MTA_BUS_TIME_API_KEY not configured; nothing to verify live")
    async with make_client(real_settings) as client:
        snap = await BusPositionsAdapter(client=client, settings=real_settings).fetch()
    assert snap.feed is FeedName.MTA_BUS
    # buses run 24/7 system-wide; ~1,380 were active when this adapter was written
    assert len(snap.records) >= 50
    assert all(-90 <= v.lat <= 90 and -180 <= v.lon <= 180 for v in snap.records)
    assert sum(v.route_id is not None for v in snap.records) > 0.9 * len(snap.records)
    assert snap.upstream_generated_at is not None
