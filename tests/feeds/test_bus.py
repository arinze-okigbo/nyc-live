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
from nyc_live.feeds.bus import BUS_TIME_ENV_VAR, BusPositionsAdapter
from nyc_live.http import make_client


def _settings(tmp_path: Path, key: str | None) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        NYC_LIVE_DATA_DIR=tmp_path / "data",
        MTA_BUS_TIME_API_KEY=key,
    )


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
