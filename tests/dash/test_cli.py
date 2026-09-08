"""The `nyc-dash` console script wiring (uvicorn is stubbed; no server is started)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from nyc_dash import __main__ as cli
from nyc_dash.app import app as module_level_app


def test_defaults() -> None:
    args = cli.build_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8080
    assert args.reload is False
    assert args.log_level == "info"


def test_flags() -> None:
    args = cli.build_parser().parse_args(
        ["--host", "0.0.0.0", "--port", "9001", "--reload", "--log-level", "debug"]
    )
    assert (args.host, args.port, args.reload, args.log_level) == ("0.0.0.0", 9001, True, "debug")


def test_main_runs_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(
        cli.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)), raising=True
    )
    cli.main(["--host", "0.0.0.0", "--port", "9100"])
    app, kwargs = calls[0]
    assert app == "nyc_dash.app:app"
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9100
    assert kwargs["reload"] is False
    assert kwargs["reload_dirs"] is None


def test_main_reload_watches_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda _app, **kwargs: calls.append(kwargs))
    cli.main(["--reload"])
    assert calls[0]["reload"] is True
    assert calls[0]["reload_dirs"] and calls[0]["reload_dirs"][0].endswith("nyc_dash")


def test_the_import_string_resolves() -> None:
    assert isinstance(module_level_app, FastAPI)
    assert {"/api/health", "/api/stream", "/api/{feed}"} <= {
        getattr(r, "path", "") for r in module_level_app.routes
    }
