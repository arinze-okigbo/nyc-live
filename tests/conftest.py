"""Shared pytest config.

* `live` marker: tests that hit real upstreams. Skipped unless NYC_LIVE_TESTS=1.
* `store` fixture: in-memory DuckDB with the frozen schema applied.
* `settings` fixture: Settings with a temp data dir and no env leakage.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from nyc_live.config import Settings
from nyc_live.store import Store

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("NYC_LIVE_TESTS", "0") in {"1", "true", "yes"}:
        return
    skip = pytest.mark.skip(reason="live upstream test; set NYC_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


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
