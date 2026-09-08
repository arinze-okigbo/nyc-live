"""Phase 2 gate: the real MCP server in its own process, driven over stdio by a real client.

`python -m nyc_mcp` is spawned exactly the way `claude mcp add nyc-live -- uv run
--directory <repo> nyc-mcp` spawns it, and every tool in `nyc_mcp.server.TOOL_NAMES`
is called through `fastmcp.Client` over the stdio transport. Nothing is patched in
that process: it builds its own `Services`, its own httpx client and opens the real
DuckDB file read-only.

The DuckDB file handed to it is seeded (by `conftest.seed_density`) with density rows
tagged `fake:integration-tester`, so the store-backed tools have something real to
read and their `stale_after` can be asserted against `DEFAULT_TTL`. The upstreams are
genuinely unreachable in that process, so the feed-backed tools return honest error
envelopes - which is exactly the shape the gate has to prove too.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StdioTransport
from mcp.types import TextContent

from nyc_live.contracts import DEFAULT_TTL, FeedName
from nyc_live.store import Store
from nyc_mcp.server import TOOL_NAMES
from tests.integration.conftest import (
    CAM_A,
    REPO_ROOT,
    TIMES_SQ,
    check_envelope,
    child_env,
    free_port,
    parse_iso,
    seed_density,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

TOOL_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_cameras", {"limit": 5}),
    ("nearby_cameras", {"lat": TIMES_SQ[0], "lon": TIMES_SQ[1], "radius_m": 800}),
    ("get_camera_frame", {"camera_id": CAM_A}),
    ("subway_arrivals", {"stop_id": "127"}),
    ("subway_alerts", {"route_id": "A"}),
    ("citibike_status", {"limit": 5}),
    ("nearby_311", {"limit": 5}),
    ("weather_now", {}),
    ("query_warehouse", {"sql": "SELECT count(*) AS n FROM density_samples"}),
    ("density_now", {}),
    ("density_history", {"bucket_s": 60, "hours": 1}),
)
"""Every tool except `feed_health`, which is not an Envelope and is asserted separately."""

WRITE_ATTEMPTS: tuple[str, ...] = (
    "INSERT INTO density_samples VALUES ('x', now(), 'person', 1, NULL, NULL, 'm', NULL, 1, 1)",
    "UPDATE cameras SET name = 'owned'",
    "DELETE FROM density_samples",
    "DROP TABLE cameras",
    "SELECT count(*) FROM cameras; DROP TABLE cameras",
    "CREATE TABLE evil (a INTEGER)",
    "ATTACH ':memory:' AS other",
    "COPY cameras TO '/tmp/nyc-live-integration-should-not-exist.csv'",
)


@pytest.fixture(scope="module")
def mcp_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real DuckDB file with the frozen schema and the known density rows."""
    path = tmp_path_factory.mktemp("mcp") / "nyc_live.duckdb"
    with Store(path) as store:
        seed_density(store)
    return path


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def mcp(mcp_db: Path) -> AsyncIterator[Client]:
    """A real `nyc-mcp` process, spoken to over stdio by a real MCP client."""
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "nyc_mcp", "--log-level", "WARNING"],
        env=child_env(duckdb_path=mcp_db, data_dir=mcp_db.parent, dead_proxy_port=free_port()),
        cwd=str(REPO_ROOT),
    )
    async with Client(transport) as client:
        yield client


def envelope_of(result: CallToolResult) -> dict[str, Any]:
    """The tool's Envelope, taken from structured content (text block as a cross-check)."""
    structured = result.structured_content
    assert isinstance(structured, dict), f"tool returned no structured content: {result.content!r}"
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    assert texts, "every tool must also emit a text block with the envelope"
    assert json.loads(texts[-1]) == structured, "text block and structured content disagree"
    return structured


async def test_server_exposes_exactly_the_documented_tools(mcp: Client) -> None:
    tools = await mcp.list_tools()
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
    for tool in tools:
        assert tool.description, f"{tool.name} has no description for the model to read"


@pytest.mark.parametrize(("tool", "args"), TOOL_CALLS, ids=[c[0] for c in TOOL_CALLS])
async def test_every_tool_returns_a_contract_envelope(
    mcp: Client, tool: str, args: dict[str, Any]
) -> None:
    """Envelope shape + `stale_after` on every tool, over the real stdio transport."""
    payload = envelope_of(await mcp.call_tool(tool, args))
    status = check_envelope(payload)
    feed = FeedName(payload["feed"])
    if status == "error":
        return
    fetched, stale = parse_iso(payload["fetched_at"]), parse_iso(payload["stale_after"])
    expected = DEFAULT_TTL[feed]
    assert stale - fetched == expected, (
        f"{tool}: stale_after - fetched_at is {stale - fetched}, "
        f"expected DEFAULT_TTL[{feed.value}] = {expected}"
    )


async def test_density_tools_read_the_rows_the_store_holds(mcp: Client) -> None:
    """density_now / density_history over stdio return the seeded aggregate, not a guess."""
    now = envelope_of(await mcp.call_tool("density_now", {"window_s": 600}))
    assert now["status"] == "fresh", now["error"]
    by_camera = {r["camera_id"]: r for r in now["records"]}
    assert CAM_A in by_camera, f"seeded camera missing from density_now: {sorted(by_camera)}"
    record = by_camera[CAM_A]
    assert record["sample_count"] == 2
    assert record["person_mean"] == 5.0 and record["person_max"] == 6
    assert record["vehicle_mean"] == 3.5 and record["vehicle_max"] == 5
    assert record["lat"] == TIMES_SQ[0] and record["lon"] == TIMES_SQ[1], (
        "density_now lost the location that nyc-vision upserts into the `cameras` table"
    )
    assert parse_iso(now["stale_after"]) - parse_iso(now["fetched_at"]) == timedelta(seconds=60)

    history = envelope_of(
        await mcp.call_tool("density_history", {"camera_id": CAM_A, "hours": 1, "bucket_s": 60})
    )
    assert history["status"] == "fresh", history["error"]
    assert len(history["records"]) == 2, "two frames 60 s apart must land in two 60 s buckets"
    assert {r["camera_id"] for r in history["records"]} == {CAM_A}


async def test_query_warehouse_reads_timestamptz_columns(mcp: Client) -> None:
    """The pytz workaround in services/warehouse.py, exercised through the real server."""
    payload = envelope_of(
        await mcp.call_tool(
            "query_warehouse",
            {"sql": "SELECT camera_id, ts, class, count FROM density_samples WHERE count > 0"},
        )
    )
    assert payload["status"] == "fresh", payload["error"]
    result = payload["records"][0]
    assert result["columns"] == ["camera_id", "ts", "class", "count"]
    assert result["row_count"] == 6, result["rows"]
    for camera_id, ts, cls, count in result["rows"]:
        assert camera_id.startswith("int-cam-")
        parse_iso(ts)  # must be a parseable instant, whatever the client-side tz support
        assert cls in {"person", "car", "bus"}
        assert count > 0


@pytest.mark.parametrize("sql", WRITE_ATTEMPTS)
async def test_query_warehouse_rejects_writes(mcp: Client, sql: str) -> None:
    payload = envelope_of(await mcp.call_tool("query_warehouse", {"sql": sql}))
    assert payload["status"] == "error", f"query_warehouse accepted a write: {sql!r}"
    assert payload["records"] == []
    assert payload["error"]["kind"] == "internal"
    assert payload["error"]["message"], sql


async def test_the_warehouse_is_still_intact_after_the_write_attempts(mcp: Client) -> None:
    """Belt and braces: the tables the writes targeted are unchanged and the store is read-only."""
    payload = envelope_of(
        await mcp.call_tool(
            "query_warehouse",
            {
                "sql": "SELECT (SELECT count(*) FROM cameras), (SELECT count(*) FROM density_samples)"
            },
        )
    )
    assert payload["status"] == "fresh", payload["error"]
    assert payload["records"][0]["rows"] == [[2, 18]], "the warehouse changed under a write attempt"
    health = (await mcp.call_tool("feed_health", {})).structured_content
    assert isinstance(health, dict)
    assert health["store"]["open"] is True
    assert health["store"]["read_only"] is True, "a reader process must open DuckDB read-only"


async def test_feed_health_covers_every_registered_feed(mcp: Client) -> None:
    health = (await mcp.call_tool("feed_health", {})).structured_content
    assert isinstance(health, dict)
    assert set(health) == {"checked_at", "store", "feeds"}
    feeds = {f["feed"]: f for f in health["feeds"]}
    assert len(feeds) == 10, sorted(feeds)
    for name, entry in feeds.items():
        assert set(entry) == {
            "feed",
            "configured",
            "status",
            "ttl_s",
            "last_ok_at",
            "last_error",
            "consecutive_failures",
            "record_count",
        }
        assert entry["status"] in {"fresh", "stale", "error", "never_fetched"}
        assert entry["ttl_s"] == DEFAULT_TTL[FeedName(name)].total_seconds()
    assert feeds["mta_bus"]["configured"] is False
    assert feeds["ny511_cameras"]["configured"] is False


async def test_invalid_arguments_are_tool_errors_not_envelopes(mcp: Client) -> None:
    """`isError` is reserved for bad arguments; a down feed is never an isError."""
    bad = await mcp.call_tool("get_camera_frame", {}, raise_on_error=False)
    assert bad.is_error, "get_camera_frame with neither camera_id nor lat/lon must be a ToolError"
    # subway_alerts, not citibike_status: a second call to a Citi Bike / 311 / weather /
    # inspections tool stalls this server for a whole TTL, see the xfail in
    # tests/integration/test_phase1_feeds.py::test_down_feed_blocks_the_caller_for_a_whole_ttl.
    down = await mcp.call_tool("subway_alerts", {}, raise_on_error=False)
    assert not down.is_error, "a feed outage must be an envelope, not an MCP error"
    assert envelope_of(down)["status"] == "error"


async def test_the_documented_console_script_serves_over_stdio(mcp_db: Path) -> None:
    """`claude mcp add nyc-live -- ... nyc-mcp` registers a script that really speaks MCP."""
    script = REPO_ROOT / ".venv" / "bin" / "nyc-mcp"
    if not script.exists():  # pragma: no cover - only on a tree that was never `uv sync`ed
        pytest.skip(f"console script not installed: {script}")
    readme = (REPO_ROOT / "packages" / "nyc-mcp" / "README.md").read_text()
    assert "claude mcp add nyc-live -- uv run --directory" in readme
    transport = StdioTransport(
        command=str(script),
        args=["--log-level", "WARNING"],
        env=child_env(duckdb_path=mcp_db, data_dir=mcp_db.parent, dead_proxy_port=free_port()),
        cwd=str(REPO_ROOT),
    )
    async with Client(transport) as client:
        assert sorted(t.name for t in await client.list_tools()) == sorted(TOOL_NAMES)
