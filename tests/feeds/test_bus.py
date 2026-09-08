"""MTA Bus Time stub: key-gated skip path."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    }
    if base is not None:
        kwargs["NYC_LIVE_MTA_BUS_TIME_BASE"] = base
    return Settings(**kwargs)  # type: ignore[arg-type]


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


async def test_bus_with_key_is_configured_but_deferred(tmp_path: Path) -> None:
    secret = "test-key-do-not-leak"
    settings = _settings(tmp_path, secret)
    async with make_client(settings) as client:
        adapter = BusPositionsAdapter(client=client, settings=settings)
        assert adapter.is_configured() is True
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()
        assert not isinstance(excinfo.value, FeedNotConfigured)
        assert excinfo.value.kind is ErrorKind.INTERNAL
        assert secret not in str(excinfo.value)
        assert secret not in (excinfo.value.url or "")


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


async def test_bus_deferred_error_url_follows_override_and_omits_key(tmp_path: Path) -> None:
    base = "http://127.0.0.1:9/siri"
    settings = _settings(tmp_path, FAKE_KEY, base=base)
    async with make_client(settings) as client:
        adapter = BusPositionsAdapter(client=client, settings=settings)
        assert adapter.is_configured() is True
        assert adapter.source_url == "http://127.0.0.1:9/siri/vehicle-monitoring.json"
        with pytest.raises(FeedUnavailable) as excinfo:
            await adapter.fetch()
        err = excinfo.value
        assert err.kind is ErrorKind.INTERNAL
        assert err.url == "http://127.0.0.1:9/siri/vehicle-monitoring.json"


async def test_bus_never_leaks_the_key_in_url_error_or_envelope(tmp_path: Path) -> None:
    settings = _settings(tmp_path, FAKE_KEY)
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
        assert env.error is not None and env.error.kind is ErrorKind.INTERNAL
        assert FAKE_KEY not in env.model_dump_json()
        assert FAKE_KEY not in str(feed.health().model_dump())
