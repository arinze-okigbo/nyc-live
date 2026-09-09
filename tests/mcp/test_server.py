"""nyc-mcp tool surface: every tool listed and callable in-process, envelope shapes, errors."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastmcp import Client
from mcp.types import ImageContent, TextContent

from nyc_live.cache import CachedFeed
from nyc_live.contracts import FeedName, now_utc
from nyc_live.feeds.cameras import CameraFrameSource
from nyc_live.services import Services
from nyc_live.store import Store
from nyc_mcp.server import TOOL_NAMES, create_server
from tests.mcp.conftest import (
    BATTERY,
    CAM_OFFLINE,
    CAM_ONLINE,
    TIMES_SQ,
    DownAdapter,
    make_registry,
)

ENVELOPE_KEYS = {
    "feed",
    "status",
    "fetched_at",
    "stale_after",
    "records",
    "error",
    "query",
    "total_before_filter",
    "truncated",
}


async def call(mcp: Client, name: str, **args: Any) -> dict[str, Any]:
    result = await mcp.call_tool(name, args)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


# --------------------------------------------------------------------------- surface


async def test_every_tool_is_listed_with_llm_facing_docs(mcp: Client) -> None:
    tools = {t.name: t for t in await mcp.list_tools()}
    assert set(TOOL_NAMES) <= set(tools), sorted(set(TOOL_NAMES) - set(tools))
    for name in TOOL_NAMES:
        desc = tools[name].description or ""
        assert len(desc) > 80, name
        if name != "feed_health":
            assert 'status="error"' in desc, name
    for name in ("list_cameras", "subway_arrivals", "citibike_status", "nearby_311", "weather_now"):
        props = tools[name].input_schema["properties"]
        assert {"lat", "lon", "radius_m", "limit"} <= set(props), name


@pytest.mark.parametrize(
    ("name", "args", "feed"),
    [
        ("list_cameras", {}, "dot_cameras"),
        ("nearby_cameras", {"lat": TIMES_SQ[0], "lon": TIMES_SQ[1]}, "dot_cameras"),
        ("subway_arrivals", {}, "mta_subway"),
        ("subway_alerts", {}, "mta_subway_alerts"),
        ("citibike_status", {}, "citibike"),
        ("nearby_311", {}, "nyc_311"),
        ("weather_now", {}, "weather"),
        ("query_warehouse", {"sql": "SELECT 1 AS one"}, "warehouse"),
        ("density_now", {}, "density"),
        ("density_history", {}, "density"),
        ("camera_density_history", {"camera_id": "cam1"}, "density"),
    ],
)
async def test_data_tools_return_envelopes(
    mcp: Client, name: str, args: dict[str, Any], feed: str
) -> None:
    env = await call(mcp, name, **args)
    assert set(env) >= ENVELOPE_KEYS
    assert env["feed"] == feed
    assert env["status"] in {"fresh", "stale", "error"}
    if env["status"] == "error":
        assert env["records"] == [] and env["error"] is not None
    else:
        assert env["fetched_at"] and env["stale_after"]


# --------------------------------------------------------------------------- cameras


async def test_list_cameras_geo_filter_sets_distance_and_query(mcp: Client) -> None:
    env = await call(mcp, "list_cameras", lat=TIMES_SQ[0], lon=TIMES_SQ[1], radius_m=300)
    assert env["status"] == "fresh"
    assert env["query"] == {"lat": TIMES_SQ[0], "lon": TIMES_SQ[1], "radius_m": 300.0}
    assert env["total_before_filter"] == 3
    assert [c["id"] for c in env["records"]] == [CAM_ONLINE, CAM_OFFLINE]
    assert env["records"][0]["distance_m"] == 0.0
    assert env["records"][1]["distance_m"] > 0


async def test_list_cameras_online_only_and_limit(mcp: Client) -> None:
    env = await call(mcp, "list_cameras", online_only=True, limit=1)
    assert [c["is_online"] for c in env["records"]] == [True]
    assert env["truncated"] is True
    assert env["total_before_filter"] == 3


async def test_nearby_cameras_requires_lat_lon(mcp: Client) -> None:
    result = await mcp.call_tool("nearby_cameras", {"lat": 40.7}, raise_on_error=False)
    assert result.is_error


async def test_half_specified_point_is_a_tool_error(mcp: Client) -> None:
    result = await mcp.call_tool("citibike_status", {"lat": 40.7}, raise_on_error=False)
    assert result.is_error
    assert "lat and lon" in result.content[0].text  # type: ignore[union-attr]


async def test_get_camera_frame_returns_image_block_and_metadata(mcp: Client) -> None:
    result = await mcp.call_tool("get_camera_frame", {"camera_id": CAM_ONLINE})
    assert not result.is_error
    kinds = [type(b) for b in result.content]
    assert kinds == [ImageContent, TextContent]
    image = result.content[0]
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/jpeg"
    assert len(image.data) > 0
    env = result.structured_content
    assert env is not None
    assert env["feed"] == "dot_camera_frames" and env["status"] == "fresh"
    frame = env["records"][0]
    assert frame["camera_id"] == CAM_ONLINE
    assert frame["width"] == 352 and frame["height"] == 240
    assert "data" not in frame  # bytes never leave via JSON


async def test_get_camera_frame_by_point_picks_nearest_online(mcp: Client) -> None:
    result = await mcp.call_tool(
        "get_camera_frame", {"lat": TIMES_SQ[0], "lon": TIMES_SQ[1], "radius_m": 500}
    )
    assert isinstance(result.content[0], ImageContent)
    assert result.structured_content is not None
    assert result.structured_content["records"][0]["camera_id"] == CAM_ONLINE


async def test_get_camera_frame_no_camera_in_radius_is_not_found(mcp: Client) -> None:
    result = await mcp.call_tool("get_camera_frame", {"lat": 40.85, "lon": -73.90, "radius_m": 100})
    assert not result.is_error
    assert all(isinstance(b, TextContent) for b in result.content)
    env = result.structured_content
    assert env is not None
    assert env["status"] == "error" and env["error"]["kind"] == "not_found"
    assert env["records"] == []


async def test_get_camera_frame_unknown_id_is_not_found(mcp: Client) -> None:
    result = await mcp.call_tool("get_camera_frame", {"camera_id": CAM_OFFLINE})
    env = result.structured_content
    assert env is not None
    assert env["status"] == "error"
    assert env["error"]["kind"] == "not_found"
    assert env["error"]["feed"] == "dot_camera_frames"
    assert not any(isinstance(b, ImageContent) for b in result.content)


async def test_get_camera_frame_without_args_is_tool_error(mcp: Client) -> None:
    result = await mcp.call_tool("get_camera_frame", {}, raise_on_error=False)
    assert result.is_error


async def test_frame_telemetry_is_persisted_to_store(mcp: Client, store: Store) -> None:
    await mcp.call_tool("get_camera_frame", {"camera_id": CAM_ONLINE})
    await mcp.call_tool("get_camera_frame", {"camera_id": CAM_OFFLINE})
    rows = store.execute("SELECT camera_id, ok FROM camera_frame_fetches ORDER BY ok DESC")
    assert rows == [(CAM_ONLINE, True), (CAM_OFFLINE, False)]


# --------------------------------------------------------------------------- subway


async def test_subway_arrivals_by_parent_stop(mcp: Client) -> None:
    env = await call(mcp, "subway_arrivals", stop_id="127")
    assert env["status"] == "fresh"
    got = [(r["stop_id"], r["route_id"], r["direction"]) for r in env["records"]]
    assert got == [("127S", "2", "S"), ("127N", "1", "N")]
    assert env["records"][0]["stop_name"] == "Times Sq-42 St"
    assert 0 < env["records"][0]["eta_s"] <= 4 * 60
    assert env["total_before_filter"] == 3  # t3 is beyond the 30 min horizon


async def test_subway_arrivals_by_point(mcp: Client) -> None:
    env = await call(mcp, "subway_arrivals", lat=BATTERY[0], lon=BATTERY[1], radius_m=400)
    assert [r["stop_id"] for r in env["records"]] == ["142N"]
    assert env["records"][0]["distance_m"] is not None
    assert env["query"]["radius_m"] == 400.0


async def test_subway_arrivals_horizon_and_limit(mcp: Client) -> None:
    env = await call(mcp, "subway_arrivals", horizon_s=3 * 3600, limit=2)
    assert env["truncated"] is True
    assert len(env["records"]) == 2
    assert env["total_before_filter"] == 4


async def test_subway_alerts_by_route_and_point(mcp: Client) -> None:
    env = await call(mcp, "subway_alerts", route_id="1")
    assert [a["id"] for a in env["records"]] == ["a1"]
    env = await call(mcp, "subway_alerts", lat=BATTERY[0], lon=BATTERY[1], radius_m=400)
    assert [a["id"] for a in env["records"]] == ["a1"]
    assert env["query"] is not None
    env = await call(
        mcp, "subway_alerts", lat=BATTERY[0], lon=BATTERY[1], radius_m=400, route_id="G"
    )
    assert env["records"] == []
    assert env["status"] == "fresh"  # empty after a filter is fine; the feed itself was OK


# --------------------------------------------------------------------------- bikes / civic


async def test_citibike_nearby(mcp: Client) -> None:
    env = await call(mcp, "citibike_status", lat=TIMES_SQ[0], lon=TIMES_SQ[1], radius_m=600)
    assert [s["station_id"] for s in env["records"]] == ["s1"]
    assert env["records"][0]["distance_m"] > 0


async def test_nearby_311_drops_ungeocoded_and_matches_type(mcp: Client) -> None:
    env = await call(mcp, "nearby_311")
    assert len(env["records"]) == 2
    env = await call(mcp, "nearby_311", lat=TIMES_SQ[0], lon=TIMES_SQ[1])
    assert [r["unique_key"] for r in env["records"]] == ["1"]
    env = await call(mcp, "nearby_311", complaint_type="street")
    assert [r["unique_key"] for r in env["records"]] == ["1", "2"]


async def test_weather_now_default_radius_reaches_a_station(mcp: Client) -> None:
    env = await call(mcp, "weather_now", lat=BATTERY[0], lon=BATTERY[1])
    assert [r["station_id"] for r in env["records"]] == ["KNYC"]
    assert env["records"][0]["distance_m"] > 5000


# --------------------------------------------------------------------------- warehouse


async def test_query_warehouse_select(mcp: Client) -> None:
    env = await call(mcp, "query_warehouse", sql="SELECT 40 + 2 AS answer")
    assert env["status"] == "fresh"
    assert env["records"][0]["columns"] == ["answer"]
    assert env["records"][0]["rows"] == [[42]]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM feed_fetches",
        "INSERT INTO cameras VALUES ('x','nyc_dot','n',0,0,true,now(),now())",
        "DROP TABLE cameras",
        "SELECT 1; SELECT 2",
        "SELECT * FROM schema_meta",
        "COPY cameras TO '/tmp/x.csv'",
    ],
)
async def test_query_warehouse_rejects_dml(mcp: Client, sql: str) -> None:
    env = await call(mcp, "query_warehouse", sql=sql)
    assert env["status"] == "error"
    assert env["error"]["kind"] == "internal"
    assert env["records"] == []
    assert env["error"]["message"]


async def test_query_warehouse_reads_timestamptz_rows(mcp: Client, store: Store) -> None:
    store.record_feed_fetch(FeedName.CITIBIKE, ok=True, latency_ms=1.0, record_count=3)
    env = await call(mcp, "query_warehouse", sql="SELECT feed, ts, ok FROM feed_fetches")
    assert env["status"] == "fresh", env["error"]
    (row,) = env["records"][0]["rows"]
    assert row[0] == "citibike" and row[2] is True
    assert row[1].endswith("Z")


# --------------------------------------------------------------------------- health


async def test_feed_health_lists_every_feed_and_flags_key_gated(mcp: Client) -> None:
    out = await call(mcp, "feed_health")
    feeds = {h["feed"]: h for h in out["feeds"]}
    assert {"dot_cameras", "mta_subway", "mta_bus", "ny511_cameras", "citibike"} <= set(feeds)
    assert feeds["mta_bus"]["configured"] is False
    assert feeds["mta_bus"]["status"] == "never_fetched"
    assert out["store"]["open"] is True and out["store"]["read_only"] is False
    await call(mcp, "citibike_status")
    out = await call(mcp, "feed_health")
    feeds = {h["feed"]: h for h in out["feeds"]}
    assert feeds["citibike"]["status"] == "fresh" and feeds["citibike"]["record_count"] == 2


# --------------------------------------------------------------------------- density stubs


async def test_density_tools_are_not_configured_until_vision_runs(mcp: Client) -> None:
    for name in ("density_now", "density_history"):
        env = await call(mcp, name)
        assert env["status"] == "error", name
        assert env["error"]["kind"] == "not_configured"
        assert "nyc-vision" in env["error"]["message"]
        assert env["records"] == []
    env = await call(mcp, "camera_density_history", camera_id="cam1")
    assert env["status"] == "error"
    assert env["error"]["kind"] == "not_configured"
    assert "nyc-vision" in env["error"]["message"]
    assert env["records"] == []


async def test_camera_density_history_requires_camera_id(mcp: Client) -> None:
    env = await call(mcp, "camera_density_history", camera_id="")
    assert env["status"] == "error"
    assert env["error"]["kind"] == "internal"
    assert "camera_id" in env["error"]["message"]


# --------------------------------------------------------------------------- feed down


async def test_down_feed_is_an_error_envelope_not_empty_success(
    cam_settings: Any, client: httpx.AsyncClient, store: Store, mock: Any
) -> None:
    registry = make_registry(now_utc(), down={FeedName.CITIBIKE, FeedName.MTA_SUBWAY}, store=store)
    services = Services(
        settings=cam_settings,
        client=client,
        registry=registry,
        frames=CameraFrameSource(client=client, settings=cam_settings),
        store=store,
    )
    async with Client(create_server(services)) as mcp:
        env = await call(mcp, "citibike_status", lat=TIMES_SQ[0], lon=TIMES_SQ[1])
        assert env["status"] == "error"
        assert env["records"] == []
        assert env["error"]["kind"] == "upstream_http"
        assert env["error"]["upstream_status"] == 403
        assert env["error"]["feed"] == "citibike"
        # a derived tool inherits the error of its broken input
        env = await call(mcp, "subway_arrivals", stop_id="127")
        assert env["status"] == "error"
        assert env["error"]["feed"] == "mta_subway"
        # health shows the failure and the telemetry row landed
        out = await call(mcp, "feed_health")
        feeds = {h["feed"]: h for h in out["feeds"]}
        assert feeds["citibike"]["status"] == "error"
        assert feeds["citibike"]["consecutive_failures"] >= 1
    rows = store.execute("SELECT feed, ok, error_kind FROM feed_fetches WHERE feed = 'citibike'")
    assert rows and rows[0][1] is False and rows[0][2] == "upstream_http"


async def test_key_gated_feed_reports_not_configured(mcp: Client, services: Services) -> None:
    env = await services.registry[FeedName.MTA_BUS].get()
    assert env.status == "error"
    assert env.error is not None and env.error.kind == "not_configured"
    assert isinstance(services.registry[FeedName.MTA_BUS], CachedFeed)
    assert not isinstance(services.registry[FeedName.MTA_BUS].adapter, DownAdapter)
