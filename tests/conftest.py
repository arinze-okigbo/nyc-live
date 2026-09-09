"""Shared pytest config.

* `live` marker: tests that hit real upstreams. Skipped unless NYC_LIVE_TESTS=1.
* `store` fixture: in-memory DuckDB with the frozen schema applied.
* `settings` fixture: Settings with a temp data dir and no env leakage.
* key-gated env vars are cleared for offline runs; see `_no_ambient_api_keys`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from nyc_live.config import Settings
from nyc_live.store import Store

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Every env var that flips a key-gated feed from "not configured" to configured.
# Keep in sync with the aliases in nyc_live.config.Settings.
KEY_GATED_ENV_VARS = ("MTA_BUS_TIME_API_KEY", "NY511_API_KEY")


def _live_mode() -> bool:
    return os.environ.get("NYC_LIVE_TESTS", "0") in {"1", "true", "yes"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _live_mode():
        return
    skip = pytest.mark.skip(reason="live upstream test; set NYC_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_ambient_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make offline runs hermetic w.r.t. real API keys sitting in the environment.

    Several tests assert that key-gated feeds report `not_configured`. That is only
    true when no key is present -- and `Settings(_env_file=None)` does NOT deliver
    that on its own: it suppresses reading the `.env` *file*, but pydantic-settings
    still reads `os.environ`. The justfile sets `dotenv-load := true`, so `just check`
    (the gate CLAUDE.md requires before every commit) runs with the developer's real
    keys exported, while a bare `pytest` does not. The same five tests therefore
    passed one way and failed the other, purely on how they were invoked.

    Live runs are exempt: they need the real keys to exercise the real upstreams.
    """
    if _live_mode():
        return
    for name in KEY_GATED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        NYC_LIVE_DATA_DIR=tmp_path / "data",
        NYC_LIVE_DUCKDB_PATH=tmp_path / "data" / "test.duckdb",
        NYC_LIVE_USER_AGENT="nyc-live-tests/0.1 (https://github.com/arinze-okigbo/nyc-live)",
    )


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as s:
        yield s


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR
