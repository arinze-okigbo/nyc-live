"""Seams: places where two agents' assumptions have to line up, asserted directly.

Everything here is a cross-boundary agreement test. Where the two sides already agree
the test passes; where they do not it is an `xfail(strict=True)` whose reason names the
defect, the owner and the file, so the suite records it without this agent editing code
it does not own.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import httpx
from fastmcp import Client

from nyc_dash.api import health_payload
from nyc_live.config import Settings
from nyc_live.contracts import FeedName
from nyc_live.feeds.transit import (
    STATIC_GTFS_URL,
    SubwayAlertsAdapter,
    SubwayShapesAdapter,
    reset_raw_cache,
)
from nyc_live.services import Services, density_now, open_services, warehouse
from nyc_live.store import Store
from nyc_mcp.server import create_server
from nyc_vision.report import cameras_covered
from tests.integration.conftest import CAM_A, REPO_ROOT, parse_iso, seed_density

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


def guard_error(store: Store, sql: str) -> str:
    """Run `sql` through the real `query_warehouse` service; it must come back rejected."""
    env = warehouse(store, sql)
    assert env.status == "error", (
        f"query_warehouse accepted {sql!r} and returned "
        f"{env.records[0].rows if env.records else None}"
    )
    assert env.records == []
    assert env.error is not None
    return env.error.message


def guard_rows(store: Store, sql: str) -> list[list[object]]:
    """Run `sql` through the real `query_warehouse` service; it must be accepted."""
    env = warehouse(store, sql)
    assert env.status == "fresh", env.error.message if env.error else ""
    assert len(env.records) == 1
    return env.records[0].rows


def test_warehouse_refuses_to_read_files_and_table_functions(tmp_path: Path) -> None:
    """DuckDB's replacement scan turns a quoted path into a table; the guard catches it."""
    secret = tmp_path / "secret.csv"
    secret.write_text("a,b\n1,2\n")
    with Store(tmp_path / "guard.duckdb") as store:
        for sql in (
            f"SELECT * FROM '{secret}'",
            f"SELECT * FROM read_csv('{secret}')",
            f"SELECT * FROM read_csv_auto('{secret}') JOIN cameras ON true",
            "SELECT * FROM duckdb_tables()",
            "SELECT * FROM glob('/etc/*')",
        ):
            message = guard_error(store, sql)
            assert "only plain table names may follow FROM / JOIN" in message, sql
        assert guard_rows(store, "SELECT count(*) FROM cameras") == [[0]]


def test_warehouse_accepts_with_queries_that_use_their_own_cte(tmp_path: Path) -> None:
    """The tool docstring advertises 'a single SELECT/WITH statement'; WITH really works."""
    with Store(tmp_path / "cte.duckdb") as store:
        seed_density(store)
        assert guard_rows(
            store,
            "WITH frames AS (SELECT camera_id, ts FROM density_samples) "
            "SELECT count(*) FROM frames",
        ) == [[18]]
        assert guard_rows(
            store,
            "WITH counted (camera_id, n) AS ("
            "  SELECT camera_id, count(*) FROM density_samples GROUP BY camera_id"
            ") SELECT count(*) FROM counted",
        ) == [[2]]
        assert guard_rows(
            store,
            "WITH RECURSIVE series(n) AS ("
            "  SELECT 1 UNION ALL SELECT n + 1 FROM series WHERE n < 3"
            ") SELECT sum(n) FROM series",
        ) == [[6]]
        # a CTE name is only a target inside its own query, not a way in to another table
        assert "unknown or non-queryable table(s): ['frames']" in guard_error(
            store, "SELECT count(*) FROM frames"
        )


def test_warehouse_treats_string_literals_as_data_not_syntax(tmp_path: Path) -> None:
    """Keywords and semicolons inside quoted values are content; the guard must not trip."""
    with Store(tmp_path / "literal.duckdb") as store:
        seed_density(store)
        # the not-configured message this repo writes into feed_fetches contains "is not set"
        assert guard_rows(
            store, "SELECT count(*) FROM feed_fetches WHERE error LIKE '%is not set%'"
        ) == [[0]]
        assert guard_rows(
            store, "SELECT count(*) FROM cameras WHERE name = 'a; drop table cameras'"
        ) == [[0]]
        assert guard_rows(
            store, "SELECT count(*) FROM density_samples WHERE model = 'fake:insert-copy-set'"
        ) == [[0]]
        assert guard_rows(store, "SELECT count(*) FROM cameras /* a block comment */") == [[2]]


def test_warehouse_still_blocks_writes_multi_statements_and_unknown_tables(
    tmp_path: Path,
) -> None:
    """The guard's own job, re-asserted after the fix: nothing below may be accepted."""
    with Store(tmp_path / "blocked.duckdb") as store:
        seed_density(store)
        for sql in (
            "DROP TABLE cameras",
            "INSERT INTO cameras VALUES ('x', 'nyc_dot', 'n', 1, 2, true, now(), now())",
            "UPDATE cameras SET name = 'owned'",
            "DELETE FROM density_samples",
            "CREATE TABLE evil (a INTEGER)",
            "COPY cameras TO '/tmp/nyc-live-integration-should-not-exist.csv'",
        ):
            assert "only SELECT / WITH queries are allowed" in guard_error(store, sql), sql
        assert "only a single statement is allowed" in guard_error(
            store, "SELECT count(*) FROM cameras; DROP TABLE cameras"
        )
        assert "unknown or non-queryable table(s): ['schema_meta']" in guard_error(
            store, "SELECT * FROM schema_meta"
        )
        # a dotted name is not a way past the allow-list; only a `main.` prefix is stripped
        assert "unknown or non-queryable table(s): ['pg_catalog.pg_tables']" in guard_error(
            store, "SELECT * FROM pg_catalog.pg_tables"
        )
        assert guard_rows(store, "SELECT count(*) FROM main.cameras") == [[2]]
        # and the tables that ARE on the allow-list still answer, including joined
        assert guard_rows(
            store,
            "SELECT count(*) FROM density_samples d JOIN cameras c ON c.camera_id = d.camera_id",
        ) == [[18]]


def test_warehouse_still_allows_subqueries(tmp_path: Path) -> None:
    """Subqueries are skipped deliberately by the FROM scan; they must keep working."""
    with Store(tmp_path / "subquery.duckdb") as store:
        seed_density(store)
        assert guard_rows(store, "SELECT n FROM (SELECT 1 AS n)") == [[1]]
        assert guard_rows(
            store,
            "SELECT max(n) FROM (SELECT camera_id, count(*) AS n FROM density_samples "
            "GROUP BY camera_id)",
        ) == [[12]]  # int-cam-a: 2 frames x 6 contract classes


# --------------------------------------------------------------------------- env overrides


async def test_every_feed_can_be_redirected_by_settings(
    offline_upstreams: int, integration_settings: Settings, tmp_path: Path
) -> None:
    """Point every upstream at a dead local address; every feed must report that address.

    Regression test for the defect this suite found: `SubwayStopsAdapter.source_url` was
    the module constant `STATIC_GTFS_URL`, bound at import with no `Settings` field behind
    it, so `mta_subway_stops` was the one feed that could not be redirected or killed by
    environment - which the Phase 4 brief ("kill one feed via env") needs, and which an
    offline test run needs even more (the default URL is a 5.6 MB S3 download).

    Nothing here may reach a real upstream: every URL points at 127.0.0.1 and the proxy
    is dead, so a feed that ignored its override could only show up as a non-marker URL
    or as a `fresh` envelope, and both are asserted against below.
    """
    marker = f"http://127.0.0.1:{offline_upstreams}/redirected-by-integration-tester"
    redirected = integration_settings.model_copy(
        update={
            "dot_cameras_base": f"{marker}/cameras",
            "mta_gtfs_base": f"{marker}/mta",
            "mta_static_gtfs_url": f"{marker}/gtfs_subway.zip",
            "citibike_gbfs_root": f"{marker}/gbfs.json",
            "socrata_base": f"{marker}/socrata",
            "weather_base": f"{marker}/weather",
        }
    )
    key_gated = {FeedName.MTA_BUS, FeedName.NY511_CAMERAS}
    async with open_services(redirected, open_store=False, strict=True) as svc:
        stops_url = svc.registry[FeedName.MTA_SUBWAY_STOPS].adapter.source_url  # type: ignore[attr-defined]
        envelopes = await svc.registry.refresh_all()
    assert stops_url == f"{marker}/gtfs_subway.zip", (
        "the stops adapter must resolve its URL from Settings at fetch time"
    )
    assert all(env.status == "error" for env in envelopes.values()), (
        "no feed may succeed when every upstream has been pointed at a dead address"
    )
    not_redirected = sorted(
        name.value
        for name, env in envelopes.items()
        if name not in key_gated and (env.error is None or marker not in (env.error.url or ""))
    )
    assert not not_redirected, f"feeds that ignored the settings override: {not_redirected}"


async def test_the_stops_feed_defaults_to_the_mta_s3_zip(
    offline_upstreams: int, integration_settings: Settings
) -> None:
    """With no override the redirectable URL must still resolve to the real upstream.

    Resolution only; nothing is fetched here, so this downloads nothing even though the
    default URL is the one host this sandbox can actually reach.
    """
    assert Settings(_env_file=None).mta_static_gtfs_url == STATIC_GTFS_URL  # type: ignore[call-arg]
    async with open_services(integration_settings, open_store=False, strict=True) as svc:
        adapter = svc.registry[FeedName.MTA_SUBWAY_STOPS].adapter
        assert adapter.source_url == STATIC_GTFS_URL  # type: ignore[attr-defined]
    assert STATIC_GTFS_URL == "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip"


# --------------------------------------------------------------------------- route_id vocabulary


TRANSIT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "transit"


@contextmanager
def _serve_transit_fixtures(
    *, alerts_path: str, alerts_body: bytes, gtfs_path: str, gtfs_body: bytes
) -> Iterator[str]:
    """Real `ThreadingHTTPServer` on an ephemeral loopback port serving both real,
    already-recorded transit fixtures. Yields the base URL.

    Not a mock of the adapters' HTTP calls: a real socket, a real response, the real
    `SubwayAlertsAdapter`/`SubwayShapesAdapter` making a real request and doing their
    real parsing. Only the transport (a loopback port instead of api-endpoint.mta.info)
    differs from the live path -- and it must be a real socket rather than
    `offline_upstreams`, since that fixture's dead-proxy env would swallow this
    loopback request too.
    """

    class _Handler(http.server.BaseHTTPRequestHandler):
        _routes = {
            alerts_path: (alerts_body, "application/x-protobuf"),
            gtfs_path: (gtfs_body, "application/zip"),
        }

        def do_GET(self) -> None:
            entry = self._routes.get(self.path)
            if entry is None:
                self.send_response(404)
                self.end_headers()
                return
            body, content_type = entry
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return  # silence stdlib's per-request stderr logging

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def test_alerts_and_shapes_agree_on_the_route_id_vocabulary() -> None:
    """The dash "alerts highlight route shapes" feature (map-layers.js: clicking an
    alert's route chip sets `highlightedRoute`, which `subwayShapesLayer` compares with
    `===` against `SubwayRouteShape.route_id`) only works if `SubwayAlert.routes` and
    `SubwayRouteShape.route_id` are drawn from the same string vocabulary. Both adapters
    are exercised for real against the real recorded fixtures feed-transit already
    checked in (`tests/fixtures/transit/subway-alerts.pb`,
    `gtfs_subway_trimmed.zip`) -- no synthetic body, no respx, nothing invented -- so a
    real naming mismatch (case, an "SIR" vs "SI" style divergence, an agency prefix like
    the bus feed's `LineRef`) would show up here exactly as it would in the browser.
    """
    alerts_body = (TRANSIT_FIXTURES / "subway-alerts.pb").read_bytes()
    gtfs_body = (TRANSIT_FIXTURES / "gtfs_subway_trimmed.zip").read_bytes()
    reset_raw_cache()
    try:
        with _serve_transit_fixtures(
            alerts_path="/camsys%2Fsubway-alerts",
            alerts_body=alerts_body,
            gtfs_path="/gtfs_subway.zip",
            gtfs_body=gtfs_body,
        ) as base:
            settings = Settings(
                _env_file=None,  # type: ignore[call-arg]
                NYC_LIVE_MTA_GTFS_BASE=base,
                NYC_LIVE_MTA_STATIC_GTFS_URL=f"{base}/gtfs_subway.zip",
                NYC_LIVE_HTTP_RETRIES=0,
            )
            async with httpx.AsyncClient(trust_env=False, timeout=10.0) as client:
                alerts_snap = await SubwayAlertsAdapter(client=client, settings=settings).fetch()
                shapes_snap = await SubwayShapesAdapter(client=client, settings=settings).fetch()
    finally:
        reset_raw_cache()

    alert_routes = {route for alert in alerts_snap.records for route in alert.routes}
    shape_routes = {shape.route_id for shape in shapes_snap.records}
    assert alert_routes, "the recorded alerts fixture has no route-bearing alerts to compare"
    assert shape_routes, "the recorded shapes fixture produced no routes"

    shared = alert_routes & shape_routes
    assert shared, (
        f"alerts routes {sorted(alert_routes)} and shapes routes {sorted(shape_routes)} share "
        "no route_id at all -- map-layers.js's highlightedRoute === route_id comparison would "
        "never match anything a user could click"
    )
    # the real recorded fixtures were pulled independently (different day, different
    # endpoints) and still agree on at least these: proof the two feeds are not just
    # coincidentally disjoint-but-compatible in shape.
    assert {"2", "3", "A", "L", "SI"} <= shared, sorted(shared)

    # every route_id in both feeds must be the bare NYCT code (e.g. "6", "SI", "GS"),
    # never something agency-qualified like the bus feed's LineRef ("MTA NYCT_B54") --
    # the `===` in map-layers.js needs the literal string, not a substring or prefix.
    for route_id in alert_routes | shape_routes:
        assert route_id and " " not in route_id and "_" not in route_id, (
            f"route_id {route_id!r} does not look like a bare NYCT route code"
        )
