"""Seams: places where two agents' assumptions have to line up, asserted directly.

Everything here is a cross-boundary agreement test. Where the two sides already agree
the test passes; where they do not it is an `xfail(strict=True)` whose reason names the
defect, the owner and the file, so the suite records it without this agent editing code
it does not own.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastmcp import Client

from nyc_dash.api import health_payload
from nyc_live.config import Settings
from nyc_live.contracts import FeedName
from nyc_live.services import Services, density_now, open_services, warehouse
from nyc_live.store import Store
from nyc_mcp.server import create_server
from nyc_vision.report import cameras_covered
from tests.integration.conftest import CAM_A, parse_iso, seed_density

# --------------------------------------------------------------------------- feed_health


async def test_feed_health_means_the_same_thing_to_nyc_mcp_and_nyc_dash(
    services: Services,
) -> None:
    """`feed_health` (MCP tool) and `GET /api/health` are built independently; compare them."""
    async with Client(create_server(services)) as client:
        mcp_payload = (await client.call_tool("feed_health", {})).structured_content
    dash_payload = health_payload(services)
    assert isinstance(mcp_payload, dict)
    assert set(mcp_payload) == set(dash_payload)
    assert mcp_payload["store"] == dash_payload["store"]
    mcp_feeds = {f["feed"]: f for f in mcp_payload["feeds"]}
    dash_feeds = {f["feed"]: f for f in dash_payload["feeds"]}
    assert set(mcp_feeds) == set(dash_feeds)
    for name, entry in mcp_feeds.items():
        assert entry == dash_feeds[name], f"feed_health disagrees about {name}"
    parse_iso(mcp_payload["checked_at"])
    parse_iso(dash_payload["checked_at"])


# --------------------------------------------------------------------------- TIMESTAMPTZ


def test_the_three_timestamptz_workarounds_agree(tmp_path: Path) -> None:
    """`services/density.py`, `nyc_vision/report.py` and `services/warehouse.py` each work
    around DuckDB's missing `pytz` differently (epoch_ms twice, strftime once). They must
    still hand back the same instant for the same row."""
    with Store(tmp_path / "tz.duckdb") as store:
        t = seed_density(store)
        density = density_now(store, window=timedelta(minutes=10), camera_id=CAM_A, now=t)
        assert density.status == "fresh"
        latest = density.records[0].latest_ts
        assert latest is not None

        coverage = cameras_covered(store, t - timedelta(hours=1), until=t + timedelta(minutes=1))
        assert coverage.last_ts is not None

        env = warehouse(
            store, f"SELECT max(ts) AS ts FROM density_samples WHERE camera_id = '{CAM_A}'"
        )
        assert env.status == "fresh", env.error
        from_sql = parse_iso(str(env.records[0].rows[0][0]))

    assert latest == coverage.last_ts, "density_now and nyc_vision.report disagree on the instant"
    assert abs((from_sql - latest).total_seconds()) < 0.001, (
        f"query_warehouse returned {from_sql}, density_now returned {latest}"
    )


# --------------------------------------------------------------------------- warehouse guard


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (owner: orchestrator, src/nyc_live/store.py::Store.query_readonly): the "
        "referenced-table regex only matches bare identifiers, so a quoted path after FROM "
        "is never checked against WAREHOUSE_READ_ONLY_TABLES. DuckDB's replacement scan then "
        "reads it, so the query_warehouse MCP tool can read any CSV / Parquet / JSON file the "
        "server process can open. Writes are blocked; reads are not."
    ),
)
def test_warehouse_rejects_reading_files_off_the_local_disk(tmp_path: Path) -> None:
    """DuckDB's replacement scan turns a quoted path into a table; the guard must catch it."""
    secret = tmp_path / "secret.csv"
    secret.write_text("a,b\n1,2\n")
    with Store(tmp_path / "guard.duckdb") as store:
        env = warehouse(store, f"SELECT * FROM '{secret}'")
    assert env.status == "error", (
        f"query_warehouse read {secret} off disk and returned "
        f"{env.records[0].rows if env.records else None}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (owner: orchestrator, src/nyc_live/store.py::Store.query_readonly): the "
        "table allow-list treats a CTE alias as an unknown table, so every WITH query that "
        "references its own CTE is rejected - although the query_warehouse tool docstring "
        "(nyc_mcp/server.py) advertises 'a single SELECT/WITH statement'."
    ),
)
def test_warehouse_accepts_a_with_query_that_uses_its_own_cte(tmp_path: Path) -> None:
    with Store(tmp_path / "cte.duckdb") as store:
        seed_density(store)
        env = warehouse(
            store,
            "WITH frames AS (SELECT camera_id, ts FROM density_samples) "
            "SELECT count(*) FROM frames",
        )
    assert env.status == "fresh", env.error.message if env.error else ""


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (owner: orchestrator, src/nyc_live/store.py::_FORBIDDEN_SQL): the forbidden "
        "keyword regex is applied to the whole statement including string literals, so a "
        "legitimate SELECT whose WHERE clause contains 'set', 'insert', 'copy' etc. in a "
        "quoted value is rejected as a write. The not-configured feed error message stored "
        "in feed_fetches literally contains 'is not set'."
    ),
)
def test_warehouse_accepts_a_keyword_inside_a_string_literal(tmp_path: Path) -> None:
    with Store(tmp_path / "literal.duckdb") as store:
        env = warehouse(store, "SELECT count(*) FROM feed_fetches WHERE error LIKE '%is not set%'")
    assert env.status == "fresh", env.error.message if env.error else ""


# --------------------------------------------------------------------------- env overrides


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (owners: feed-transit for feeds/transit.py, orchestrator for config.py): "
        "SubwayStopsAdapter.source_url is the module constant STATIC_GTFS_URL, with no "
        "Settings field behind it, so mta_subway_stops is the one feed that cannot be "
        "redirected or killed by environment - which the Phase 4 brief ('kill one feed via "
        "env') and any offline test run both need. Every other adapter honours a "
        "NYC_LIVE_*_BASE setting."
    ),
)
async def test_every_feed_can_be_redirected_by_settings(
    offline_upstreams: int, integration_settings: Settings, tmp_path: Path
) -> None:
    """Point every base URL at a dead local address; every feed must report that address."""
    marker = f"http://127.0.0.1:{offline_upstreams}/redirected-by-integration-tester"
    redirected = integration_settings.model_copy(
        update={
            "dot_cameras_base": f"{marker}/cameras",
            "mta_gtfs_base": f"{marker}/mta",
            "citibike_gbfs_root": f"{marker}/gbfs.json",
            "socrata_base": f"{marker}/socrata",
            "weather_base": f"{marker}/weather",
        }
    )
    key_gated = {FeedName.MTA_BUS, FeedName.NY511_CAMERAS}
    async with open_services(redirected, open_store=False, strict=True) as svc:
        envelopes = await svc.registry.refresh_all()
    not_redirected = sorted(
        name.value
        for name, env in envelopes.items()
        if name not in key_gated and (env.error is None or marker not in (env.error.url or ""))
    )
    assert not not_redirected, f"feeds that ignored the settings override: {not_redirected}"
