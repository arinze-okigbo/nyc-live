"""`nyc-dash` console script: serve the dashboard with uvicorn.

nyc-dash                      # http://127.0.0.1:8080
nyc-dash --host 0.0.0.0 --port 9000
nyc-dash --reload             # auto-reload on source / static changes
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
APP_IMPORT_STRING = "nyc_dash.app:app"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nyc-dash",
        description="Serve the nyc-live dashboard (FastAPI + MapLibre GL / deck.gl).",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"bind port (default {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="reload on changes to the package source and static assets (development)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level (default info)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    print(f"nyc-dash serving on http://{args.host}:{args.port} (API docs at /docs)")
    uvicorn.run(
        APP_IMPORT_STRING,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(Path(__file__).parent)] if args.reload else None,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
