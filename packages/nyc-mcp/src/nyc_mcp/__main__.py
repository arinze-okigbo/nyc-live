"""`nyc-mcp` console script: stdio transport by default, `--http` optional.

stdout is the MCP wire on stdio, so all logging goes to stderr.
"""

from __future__ import annotations

import argparse
import logging
import sys

from nyc_live.config import get_settings
from nyc_mcp import __version__
from nyc_mcp.server import create_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nyc-mcp",
        description=(
            "MCP server for NYC live civic data (cameras, subway, Citi Bike, 311, weather, "
            "DuckDB warehouse). Speaks stdio by default; use --http for Streamable HTTP."
        ),
        epilog=(
            "Register with Claude Code:\n"
            "  claude mcp add nyc-live -- uv run --directory <repo> nyc-mcp\n"
            "Reads NYC_LIVE_* / *_API_KEY settings from the environment or .env "
            "(see .env.example). The DuckDB file at NYC_LIVE_DUCKDB_PATH is opened read-only "
            "when it exists and created with the schema when it does not."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"nyc-mcp {__version__}")
    p.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    p.add_argument(
        "--host", default=DEFAULT_HOST, help=f"bind host for --http (default {DEFAULT_HOST})"
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"bind port for --http (default {DEFAULT_PORT})",
    )
    p.add_argument("--path", default="/mcp", help="URL path for --http (default /mcp)")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log level for stderr (default INFO)",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = create_server(settings=get_settings())
    if args.http:
        server.run(
            transport="http",
            host=args.host,
            port=args.port,
            path=args.path,
            show_banner=False,
            log_level=args.log_level.lower(),
        )
    else:
        server.run(transport="stdio", show_banner=False, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
