"""Phase 1 gate, end to end: every adapter through the real `FeedRegistry`, plus `just smoke`.

Two halves, split by what a sandbox without upstream access can honestly prove:

* `test_all_adapters_fresh_or_not_configured_live` is the brief's test verbatim -
  load every adapter, refresh all, assert `fresh` or `not_configured`, never `error`.
  It needs the real upstreams, so it is marked `live` and runs under
  `NYC_LIVE_TESTS=1` (`just test-live`).
* everything else runs here against the real local stack with the upstreams
  genuinely unreachable (see `conftest.offline_upstreams`), and asserts the
  invariant that must hold either way: every feed ends in exactly one of
  fresh / error-with-a-real-FeedError / not-configured, and a failure is NEVER
  laundered into an empty success.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from nyc_live import smoke
from nyc_live.config import Settings, get_settings
from nyc_live.contracts import DEFAULT_TTL, ErrorKind, FeedName
from nyc_live.feeds import ADAPTER_SPECS
from nyc_live.services import build_services, open_services
from nyc_live.store import Store
from tests.integration.conftest import REPO_ROOT, check_envelope

REGISTERED_FEEDS: frozenset[FeedName] = frozenset(spec.feed for spec in ADAPTER_SPECS)
KEY_GATED: frozenset[FeedName] = frozenset({FeedName.MTA_BUS, FeedName.NY511_CAMERAS})
RETRY_BUDGET_S = 1.0
"""How long a retry of an already-failed feed may take before it is a hang."""


# --------------------------------------------------------------------------- live


@pytest.mark.live
async def test_all_adapters_fresh_or_not_configured_live(integration_settings: Settings) -> None:
    """The Phase 1 gate: real upstreams, every feed fresh or intentionally skipped."""
    async with open_services(integration_settings, open_store=False, strict=True) as svc:
        envelopes = await svc.registry.refresh_all()
    assert set(envelopes) == REGISTERED_FEEDS
    problems: list[str] = []
    for name, env in envelopes.items():
        check_envelope(env.model_dump(mode="json"), feed=name)
        if env.status == "fresh":
            assert env.records, f"{name.value} returned a fresh but empty snapshot"
            continue
        if env.error is not None and env.error.kind is ErrorKind.NOT_CONFIGURED:
            assert name in KEY_GATED, f"{name.value} reported not_configured but is not key-gated"
            continue
        problems.append(
            f"{name.value}: status={env.status} "
            f"error={env.error.kind.value if env.error else None}: "
            f"{env.error.message if env.error else ''}"
        )
    assert not problems, "feeds that were neither fresh nor not_configured:\n" + "\n".join(problems)


@pytest.mark.live
async def test_subway_stops_live(integration_settings: Settings) -> None:
    """`mta_subway_stops` against the real MTA S3 static GTFS bundle."""
    async with open_services(integration_settings, open_store=False, strict=True) as svc:
        env = await svc.registry[FeedName.MTA_SUBWAY_STOPS].get(force=True)
    assert env.status == "fresh", (
        f"mta_subway_stops was {env.status}: {env.error.message if env.error else ''}"
    )
    assert len(env.records) > 400, f"only {len(env.records)} stops came back"
    by_id = {s.stop_id: s for s in env.records}
    assert "127" in by_id, "Times Sq-42 St (127) missing from the static stop list"
    assert "127N" in by_id and by_id["127N"].parent_station == "127"
    assert "Times Sq" in by_id["127"].name
    assert all(40.4 < s.lat < 41.0 and -74.3 < s.lon < -73.6 for s in env.records)
    assert env.stale_after - env.fetched_at == DEFAULT_TTL[FeedName.MTA_SUBWAY_STOPS]  # type: ignore[operator]


@pytest.mark.live
@pytest.mark.slow
def test_smoke_cli_live() -> None:
    """`just smoke` against the real upstreams: exit 0 and one OK/SKIP line per feed."""
    proc = subprocess.run(
        [sys.executable, "-m", "nyc_live.smoke"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == len(ADAPTER_SPECS), f"expected one line per feed, got:\n{proc.stdout}"
    assert proc.returncode == 0, f"nyc-smoke exited {proc.returncode}:\n{proc.stdout}{proc.stderr}"
    assert all(ln.startswith(("OK", "SKIP")) for ln in lines), proc.stdout


# --------------------------------------------------------------------------- local stack


async def test_registry_loads_every_registered_adapter(
    offline_upstreams: int, integration_settings: Settings
) -> None:
    """`load_adapters(strict=True)` builds every module named in ADAPTER_SPECS."""
    svc = build_services(integration_settings, open_store=False, strict=True)
    try:
        assert set(svc.registry.names()) == REGISTERED_FEEDS
        assert len(svc.registry.names()) == len(ADAPTER_SPECS) == 10
        assert hasattr(svc.frames, "get_frame")
    finally:
        await svc.aclose()


async def test_refresh_all_is_honest_when_every_upstream_is_down(
    offline_upstreams: int, integration_settings: Settings, tmp_path: Path
) -> None:
    """Every feed ends fresh, not_configured, or error-with-a-real-FeedError. Never a fake success.

    This is the local-stack form of the Phase 1 gate: with the upstreams unreachable
    the honest outcome is an error envelope per network feed, and the thing that must
    never happen is `status="fresh"` standing in for a failure.
    """
    with Store(tmp_path / "telemetry.duckdb") as store:
        async with open_services(integration_settings, store=store, strict=True) as svc:
            envelopes = await svc.registry.refresh_all()
            health = {h.feed: h for h in svc.registry.health()}

        assert set(envelopes) == REGISTERED_FEEDS
        for name, env in envelopes.items():
            payload = env.model_dump(mode="json")
            status = check_envelope(payload, feed=name)
            assert status != "stale", f"{name.value} cannot be stale on a first refresh"
            if status == "fresh":
                assert env.records, (
                    f"{name.value} reported fresh with zero records while its upstream is "
                    "unreachable; an empty snapshot must never stand in for a failure"
                )
                continue
            assert env.error is not None
            if name in KEY_GATED:
                assert env.error.kind is ErrorKind.NOT_CONFIGURED, (
                    f"{name.value} is key-gated and unset; expected not_configured, "
                    f"got {env.error.kind.value}"
                )
                assert not health[name].configured
            else:
                assert env.error.kind is not ErrorKind.NOT_CONFIGURED
                assert env.error.url and env.error.url.startswith("https://"), (
                    f"{name.value} error must name the real upstream URL, got {env.error.url!r}"
                )
                assert health[name].configured
            assert health[name].status == "error"
            assert health[name].last_error is not None
            assert health[name].consecutive_failures >= 1 or name in KEY_GATED

        # the cache -> store boundary: one feed_fetches row per feed that really tried
        rows = store.execute("SELECT feed, ok, error_kind FROM feed_fetches ORDER BY feed")
        attempted = {r[0] for r in rows}
        assert attempted == {f.value for f in REGISTERED_FEEDS - KEY_GATED}, (
            f"feed_fetches rows {sorted(attempted)} do not match the feeds that fetched"
        )
        assert all(not ok for _, ok, _ in rows)
        assert all(kind for *_, kind in rows), "a failed fetch must record its error_kind"


async def test_key_gated_feeds_report_not_configured_not_missing(
    offline_upstreams: int, integration_settings: Settings
) -> None:
    """MTA_BUS / NY511_CAMERAS are registered and honest, not silently absent."""
    async with open_services(integration_settings, open_store=False, strict=True) as svc:
        for name in sorted(KEY_GATED):
            feed = svc.registry[name]
            assert not feed.adapter.is_configured()
            env = await feed.get(force=True)
            assert env.status == "error" and env.records == []
            assert env.error is not None and env.error.kind is ErrorKind.NOT_CONFIGURED
            health = feed.health()
            assert health.configured is False
            assert health.record_count is None


async def test_failed_feed_retries_without_waiting_out_the_cadence_floor(
    offline_upstreams: int, integration_settings: Settings
) -> None:
    """A feed that has never succeeded may be retried at once, and stays honestly `error`.

    `feeds/transit.py` calls `RateLimiter.forget(url)` when an attempt fails, so a
    failed fetch does not hold the per-URL cadence floor against the next caller.
    See `test_down_feed_blocks_the_caller_for_a_whole_ttl` for the feeds that do not.
    """
    async with open_services(integration_settings, open_store=False, strict=True) as svc:
        feed = svc.registry[FeedName.MTA_SUBWAY]
        first = await feed.get(force=True)
        assert first.status == "error"
        again = await asyncio.wait_for(feed.get(), timeout=RETRY_BUDGET_S)
        assert again.status == "error"
        assert again.error is not None and first.error is not None
        assert again.error.url == first.error.url
        assert feed.health().consecutive_failures == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (owners: feed-micromobility for feeds/micromobility.py, feed-civic for "
        "feeds/socrata.py and feeds/weather.py): the adapter's own RateLimiter is armed "
        "before the request and is not released when the request fails, so the SECOND "
        "refresh of a down feed sleeps for a whole TTL inside CachedFeed._refresh_lock "
        "(citibike 60 s, nyc_311 300 s, weather 300 s, dohmh_inspections 21600 s). "
        "nyc-dash /api/<feed> and the /api/stream cycle hang for that long. "
        "feeds/transit.py already has the fix: RateLimiter.forget(url) on failure."
    ),
)
async def test_down_feed_blocks_the_caller_for_a_whole_ttl(
    offline_upstreams: int, integration_settings: Settings
) -> None:
    """The second refresh of a down feed must return promptly, not sleep out its TTL."""
    blocked: list[str] = []
    async with open_services(integration_settings, open_store=False, strict=True) as svc:
        for name in (
            FeedName.CITIBIKE,
            FeedName.NYC_311,
            FeedName.WEATHER,
            FeedName.DOHMH_INSPECTIONS,
        ):
            feed = svc.registry[name]
            assert (await feed.get(force=True)).status == "error"
            try:
                await asyncio.wait_for(feed.get(force=True), timeout=RETRY_BUDGET_S)
            except TimeoutError:
                blocked.append(f"{name.value} (ttl {DEFAULT_TTL[name].total_seconds():.0f} s)")
    assert not blocked, (
        f"feeds that did not answer within {RETRY_BUDGET_S} s after a failed fetch: {blocked}"
    )


# --------------------------------------------------------------------------- smoke


def test_smoke_run_prints_one_line_per_feed_and_fails_loudly(
    offline_upstreams: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`nyc_live.smoke.run()` with the upstreams down: one line per feed, exit code 1."""
    monkeypatch.setenv("NYC_LIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NYC_LIVE_DUCKDB_PATH", str(tmp_path / "data" / "smoke.duckdb"))
    monkeypatch.setenv("NYC_LIVE_HTTP_RETRIES", "0")
    monkeypatch.setenv("MTA_BUS_TIME_API_KEY", "")
    monkeypatch.setenv("NY511_API_KEY", "")
    get_settings.cache_clear()
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            code = asyncio.run(smoke.run())
    finally:
        get_settings.cache_clear()
    lines = [ln for ln in buffer.getvalue().splitlines() if ln.strip()]
    assert len(lines) == len(ADAPTER_SPECS), (
        f"expected one line per feed, got:\n{buffer.getvalue()}"
    )
    assert code == 1, f"smoke must exit non-zero when a feed fails; printed:\n{buffer.getvalue()}"
    tags = [ln.split()[0] for ln in lines]
    assert set(tags) <= {"OK", "SKIP", "FAIL"}, tags
    assert tags.count("SKIP") == len(KEY_GATED), (
        f"key-gated feeds must print SKIP, not FAIL:\n{buffer.getvalue()}"
    )
    for line in lines:
        assert any(feed.value in line for feed in REGISTERED_FEEDS), line
