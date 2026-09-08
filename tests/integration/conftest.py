"""Fixtures for the cross-boundary integration suite.

No mocks, no `respx`, no fixture bodies. Every test here drives either

* the REAL upstreams (marked `live`, skipped unless `NYC_LIVE_TESTS=1`), or
* the REAL local stack: the real `FeedRegistry` over the real adapters, a real
  DuckDB `Store` on `tmp_path`, the real FastMCP server in its own process over
  stdio, and the real FastAPI app.

HOW "UPSTREAM DOWN" IS PRODUCED HERE
------------------------------------
The offline tests do not patch, stub or record anything. They point the process's
HTTPS proxy at a closed local port and clear `NO_PROXY`, so every real adapter
issues its real request to its real URL and gets a real transport failure. That is
the same code path a laptop with no network takes, and the `FeedError.url` in the
result is the genuine upstream URL. Nothing is substituted for a response, so no
number produced under this fixture can be mistaken for upstream data.

The only rows written to DuckDB by this suite are `DensitySample`s carrying
`model="fake:integration-tester"` and cameras whose ids start with `int-cam-`,
so any row this suite created is obvious on inspection. They are inputs for the
aggregation path, never presented as observations.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from nyc_live.config import Settings
from nyc_live.contracts import (
    DensitySample,
    DetectionClass,
    Envelope,
    FeedName,
    now_utc,
)
from nyc_live.http import make_client
from nyc_live.services import Services, build_services
from nyc_live.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]

SYNTHETIC_MODEL = "fake:integration-tester"
"""Marks every density row this suite writes. Nothing here observed the world."""

CAM_A = "int-cam-a"
CAM_B = "int-cam-b"
TIMES_SQ = (40.7580, -73.9855)
BATTERY = (40.7033, -74.0170)

UPSERT_CAMERA_SQL = """
INSERT INTO cameras (camera_id, source, name, lat, lon, is_online, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

ENVELOPE_KEYS: frozenset[str] = frozenset(Envelope[Any].model_fields)
"""The exact key set every tool / endpoint payload must have (contracts.Envelope)."""


def free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def unreachable_proxy_env(port: int) -> dict[str, str]:
    """Env that makes every https upstream fail at the transport, with no stubbing."""
    dead = f"http://127.0.0.1:{port}"
    return {
        "HTTPS_PROXY": dead,
        "https_proxy": dead,
        "ALL_PROXY": dead,
        "all_proxy": dead,
        "NO_PROXY": "",
        "no_proxy": "",
    }


@pytest.fixture
def dead_proxy_port() -> int:
    return free_port()


@pytest.fixture
def offline_upstreams(monkeypatch: pytest.MonkeyPatch, dead_proxy_port: int) -> int:
    """Make every real upstream genuinely unreachable from this process."""
    for key, value in unreachable_proxy_env(dead_proxy_port).items():
        monkeypatch.setenv(key, value)
    return dead_proxy_port


@pytest.fixture
def integration_settings(tmp_path: Path) -> Settings:
    """Real `Settings`, real upstream URLs, temp data dir, no key-gated feeds configured."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        NYC_LIVE_DATA_DIR=tmp_path / "data",
        NYC_LIVE_DUCKDB_PATH=tmp_path / "data" / "integration.duckdb",
        NYC_LIVE_HTTP_RETRIES=0,
        NYC_LIVE_HTTP_TIMEOUT_S=5.0,
        NYC_LIVE_USER_AGENT="nyc-live-integration-tests/0.1 (https://github.com/arinze-okigbo/nyc-live)",
    )


@pytest.fixture
def http_client(
    offline_upstreams: int, integration_settings: Settings
) -> Iterator[httpx.AsyncClient]:
    """The real shared client, built after the proxy env is in place."""
    client = make_client(integration_settings)
    yield client


def seed_density(store: Store, *, now: datetime | None = None) -> datetime:
    """Write the known DensitySample / cameras rows the Phase 3 + 4 tests assert on.

    Two cameras, three frames. Every row is tagged `fake:integration-tester`.

        int-cam-a  t-120s  person 4, car 2                 -> persons 4, vehicles 2
        int-cam-a  t-60s   person 6, car 4, bus 1          -> persons 6, vehicles 5
        int-cam-b  t-90s   person 0, car 10                -> persons 0, vehicles 10
    """
    t = now or now_utc()
    store.executemany(
        UPSERT_CAMERA_SQL,
        [
            (CAM_A, "nyc_dot", "Integration cam A", TIMES_SQ[0], TIMES_SQ[1], True, t, t),
            (CAM_B, "nyc_dot", "Integration cam B", BATTERY[0], BATTERY[1], True, t, t),
        ],
    )
    rows: list[DensitySample] = []
    for camera_id, offset, counts in (
        (CAM_A, 120, {DetectionClass.PERSON: 4, DetectionClass.CAR: 2}),
        (CAM_A, 60, {DetectionClass.PERSON: 6, DetectionClass.CAR: 4, DetectionClass.BUS: 1}),
        (CAM_B, 90, {DetectionClass.PERSON: 0, DetectionClass.CAR: 10}),
    ):
        ts = t - timedelta(seconds=offset)
        for cls in DetectionClass:
            rows.append(
                DensitySample(
                    camera_id=camera_id,
                    ts=ts,
                    cls=cls,
                    count=counts.get(cls, 0),
                    model=SYNTHETIC_MODEL,
                )
            )
    store.insert_density_samples(rows)
    return t


@pytest.fixture
def seeded_db(integration_settings: Settings) -> Path:
    """A real DuckDB file with the frozen schema and the known density rows, then closed."""
    path = integration_settings.duckdb_path
    with Store(path) as store:
        seed_density(store)
    return path


@pytest.fixture
def services(
    offline_upstreams: int, integration_settings: Settings, seeded_db: Path
) -> Iterator[Services]:
    """The real `Services`: real adapters, real cache, real read-only store."""
    svc = build_services(integration_settings, strict=True)
    yield svc
    if svc.store is not None:
        svc.store.close()


def child_env(
    *, duckdb_path: Path, data_dir: Path, dead_proxy_port: int, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Environment for a spawned nyc-mcp / nyc-dash process: real code, dead upstreams."""
    env = dict(os.environ)
    env.update(unreachable_proxy_env(dead_proxy_port))
    env.update(
        {
            "NYC_LIVE_DUCKDB_PATH": str(duckdb_path),
            "NYC_LIVE_DATA_DIR": str(data_dir),
            "NYC_LIVE_HTTP_RETRIES": "0",
            "NYC_LIVE_HTTP_TIMEOUT_S": "5",
            "MTA_BUS_TIME_API_KEY": "",
            "NY511_API_KEY": "",
            "SOCRATA_APP_TOKEN": "",
            "NYC_LIVE_TESTS": "0",
        }
    )
    env.update(extra or {})
    return env


def check_envelope(payload: dict[str, Any], *, feed: FeedName | None = None) -> str:
    """Assert the frozen Envelope contract on a serialised payload; returns its status.

    Enforced (contracts.py module docstring + Envelope):
      * exactly the Envelope keys, no more and no fewer;
      * `status` is one of fresh / stale / error;
      * `status == "error"` implies `records == []` and `error is not None`;
      * a non-error envelope has both timestamps and `stale_after >= fetched_at`;
      * there is no such thing as a silent empty result: fresh with no records and
        no error is only legal for a feed that really returned zero records, so we
        additionally require `total_before_filter` to be present on non-error results.
    """
    assert set(payload) == ENVELOPE_KEYS, (
        f"envelope keys {sorted(set(payload) ^ ENVELOPE_KEYS)} differ from contracts.Envelope"
    )
    status = payload["status"]
    assert status in {"fresh", "stale", "error"}, f"illegal envelope status {status!r}"
    if feed is not None:
        assert payload["feed"] == feed.value, f"expected feed {feed.value}, got {payload['feed']}"
    if status == "error":
        assert payload["records"] == [], (
            f"contracts: status=error implies records==[], got {len(payload['records'])} records"
        )
        assert payload["error"] is not None, "contracts: status=error implies error is not None"
        assert payload["fetched_at"] is None and payload["stale_after"] is None, (
            "an error envelope must not carry freshness timestamps"
        )
        error = payload["error"]
        assert set(error) >= {"kind", "message", "feed", "occurred_at"}
        assert error["message"], "FeedError.message must not be empty"
    else:
        assert payload["fetched_at"] is not None, f"{status} envelope without fetched_at"
        assert payload["stale_after"] is not None, f"{status} envelope without stale_after"
        assert parse_iso(payload["stale_after"]) >= parse_iso(payload["fetched_at"]), (
            f"stale_after {payload['stale_after']} precedes fetched_at {payload['fetched_at']}"
        )
        assert payload["total_before_filter"] is not None, (
            f"{status} envelope without total_before_filter"
        )
    return str(status)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
