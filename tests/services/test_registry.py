"""services.registry: every adapter registered over one client; store policy for readers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from nyc_live.config import Settings
from nyc_live.contracts import ErrorKind, FeedName
from nyc_live.feeds import ADAPTER_SPECS
from nyc_live.services.cameras import camera_frame
from nyc_live.services.registry import (
    Services,
    build_services,
    open_services,
    open_store_for_reader,
)
from nyc_live.store import Store


@pytest.fixture
def blocked() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=httpx.Response(403, text="Forbidden"))
        yield router


async def test_open_services_registers_every_spec_over_one_client(
    settings: Settings, store: Store
) -> None:
    async with open_services(settings, store=store) as svc:
        assert isinstance(svc, Services)
        assert set(svc.registry.names()) == {s.feed for s in ADAPTER_SPECS}
        for n in svc.registry.names():
            adapter_clients = [
                v
                for v in vars(svc.registry[n].adapter).values()
                if isinstance(v, httpx.AsyncClient)
            ]
            assert adapter_clients == [svc.client], n
        assert svc.frames.client is svc.client  # type: ignore[attr-defined]
        assert svc.store is store
        # telemetry goes to a writable store
        assert svc.registry[FeedName.CITIBIKE].store is store
    assert svc.client.is_closed


async def test_key_gated_feeds_report_not_configured(settings: Settings, store: Store) -> None:
    async with open_services(settings, store=store) as svc:
        for name in (FeedName.MTA_BUS, FeedName.NY511_CAMERAS):
            env = await svc.registry[name].get()
            assert env.status == "error" and env.records == []
            assert env.error is not None and env.error.kind == ErrorKind.NOT_CONFIGURED
            health = svc.registry[name].health()
            assert health.configured is False


async def test_blocked_upstream_is_error_envelope_with_telemetry(
    settings: Settings, store: Store, blocked: respx.MockRouter
) -> None:
    fast = settings.model_copy(update={"http_retries": 0})
    async with open_services(fast, store=store) as svc:
        env = await svc.registry[FeedName.CITIBIKE].get()
        assert env.status == "error" and env.records == []
        assert env.error is not None and env.error.kind == ErrorKind.UPSTREAM_HTTP
        assert env.error.upstream_status == 403
        frame = await camera_frame(svc.frames, "does-not-matter", store=store)
        assert frame.status == "error" and frame.error is not None
        assert frame.error.feed == FeedName.DOT_CAMERA_FRAMES
    rows = store.execute("SELECT feed, ok, status_code FROM feed_fetches")
    assert (FeedName.CITIBIKE.value, False, 403) in rows
    assert store.execute("SELECT ok FROM camera_frame_fetches") == [(False,)]


def test_reader_store_created_when_missing_then_read_only(settings: Settings) -> None:
    path: Path = settings.duckdb_path
    assert not path.exists()
    first = open_store_for_reader(settings)
    assert first is not None and first.read_only is False
    assert "density_samples" in first.tables()
    first.close()
    assert path.exists()
    second = open_store_for_reader(settings)
    assert second is not None and second.read_only is True
    assert "density_samples" in second.tables()
    second.close()


async def test_read_only_store_does_not_receive_telemetry(settings: Settings) -> None:
    open_store_for_reader(settings).close()  # type: ignore[union-attr]
    svc = build_services(settings)
    try:
        assert svc.store is not None and svc.store.read_only is True
        assert svc.registry[FeedName.CITIBIKE].store is None
    finally:
        await svc.aclose()
    assert svc.client.is_closed


def test_unopenable_store_degrades_to_none(settings: Settings, tmp_path: Path) -> None:
    bad = settings.model_copy(update={"duckdb_path": tmp_path / "not-a-db.duckdb"})
    bad.duckdb_path.write_bytes(b"this is not a duckdb file")
    assert open_store_for_reader(bad) is None
