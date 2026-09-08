"""nyc-vision CLI: `run`, `once`, `report`, `chart`.

    nyc-vision run                       # the 24 h gate: tick forever, Ctrl-C to stop
    nyc-vision once                      # one tick, useful to prove the wiring works
    nyc-vision report --since 24h        # frame-fetch failure rate + coverage + rush
    nyc-vision chart --day 2026-09-08 --out docs/vision-rush.png

Configuration is environment-only, `NYC_VISION_*` (see `nyc_vision.config`) plus the
core `NYC_LIVE_*` settings for the DuckDB path and HTTP behaviour. `run` and `once`
open the DuckDB file read-write; `report` and `chart` open it read-only so they can be
run against a database a live `run` is writing to only when that writer is stopped
(DuckDB allows a single writer process).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import signal
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nyc_live.config import Settings, get_settings
from nyc_live.contracts import Camera, FeedAdapter, FeedName
from nyc_live.feeds import load_adapters, load_frame_source
from nyc_live.http import make_client
from nyc_live.store import Store
from nyc_vision.chart import DEFAULT_OUT, NoChartData, render_rush_chart
from nyc_vision.config import VisionSettings, get_vision_settings
from nyc_vision.detector import YoloDetector
from nyc_vision.pipeline import DensityPipeline, TickResult, run_forever
from nyc_vision.report import (
    cameras_covered,
    frame_failure_rate,
    hourly_rush,
    rush_summary,
)

log = logging.getLogger("nyc_vision")

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "": 1.0}


def parse_duration(text: str) -> timedelta:
    """'24h' / '90m' / '30s' / '2d' / '3600' (seconds) -> timedelta."""
    match = _DURATION.match(text)
    if not match:
        raise argparse.ArgumentTypeError(
            f"could not parse duration {text!r}; use forms like 24h, 90m, 30s, 2d"
        )
    return timedelta(seconds=float(match.group(1)) * _UNITS[match.group(2).lower()])


def parse_day(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}") from exc


def _db_path(args: argparse.Namespace, settings: Settings) -> Path:
    return Path(args.db) if args.db else settings.duckdb_path


def _open_reader(args: argparse.Namespace, settings: Settings) -> Store | None:
    path = _db_path(args, settings)
    if not path.exists():
        print(
            f"error: no DuckDB file at {path}. Run `nyc-vision run` first, or point "
            f"--db / NYC_LIVE_DUCKDB_PATH at an existing database.",
            file=sys.stderr,
        )
        return None
    try:
        return Store(path, read_only=True)
    except Exception as exc:
        print(f"error: could not open {path} read-only: {exc}", file=sys.stderr)
        return None


def _camera_feed(adapters: list[FeedAdapter[Camera]]) -> FeedAdapter[Camera] | None:
    for adapter in adapters:
        if adapter.name is FeedName.DOT_CAMERAS:
            return adapter
    return None


# ---------------------------------------------------------------------------
# run / once
# ---------------------------------------------------------------------------


async def _pipeline_session(
    args: argparse.Namespace, settings: Settings, vision: VisionSettings
) -> int:
    path = _db_path(args, settings)
    cameras = args.cameras or vision.cameras
    store = Store(path)
    client = make_client(settings)
    try:
        adapters = load_adapters(client, settings)
        camera_feed = _camera_feed(adapters)
        if camera_feed is None:
            print(
                "error: the DOT camera list adapter is not available "
                "(nyc_live.feeds.cameras did not load)",
                file=sys.stderr,
            )
            return 2
        detector = YoloDetector(
            vision.model,
            device=vision.device,
            confidence=vision.confidence,
            imgsz=vision.imgsz,
        )
        pipeline = DensityPipeline(
            store=store,
            frames=load_frame_source(client, settings),
            detector=detector,
            camera_feed=camera_feed,
            camera_count=cameras,
            concurrency=vision.concurrency,
            grid=vision.grid,
            archive_dir=settings.archive_dir,
            archive=vision.archive,
        )
        print(
            f"nyc-vision: model={vision.model} device={vision.device} "
            f"cameras={cameras} interval={vision.interval_s:g}s db={path}"
        )
        if args.command == "once":
            result = await pipeline.tick()
            _print_tick(result)
            return 0 if result.cameras_ok else 1

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # add_signal_handler is POSIX-only; without it Ctrl-C still raises
            # KeyboardInterrupt out of asyncio.run, it is just less graceful.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        print("running; Ctrl-C to stop cleanly")
        ticks = await run_forever(
            pipeline,
            interval_s=args.interval or vision.interval_s,
            stop=stop,
            max_ticks=args.max_ticks,
            on_tick=_print_tick_line,
        )
        print(f"stopped after {ticks} tick(s)")
        return 0
    finally:
        await client.aclose()
        store.close()


def _print_tick_line(result: TickResult) -> None:
    print(result.describe())


def _print_tick(result: TickResult) -> None:
    print(result.describe())
    for camera_id, error in result.errors[:10]:
        print(f"  ! {camera_id}: {error}")
    if len(result.errors) > 10:
        print(f"  ... and {len(result.errors) - 10} more failures")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _report(args: argparse.Namespace, settings: Settings, vision: VisionSettings) -> int:
    store = _open_reader(args, settings)
    if store is None:
        return 2
    try:
        end = datetime.now(UTC)
        since = end - args.since
        failures = frame_failure_rate(store, since, until=end)
        coverage = cameras_covered(store, since, until=end)
        tz = args.tz or vision.timezone
        report_day = args.day or _local_today(end, tz)
        rows = hourly_rush(store, report_day, tz=tz)
        print(f"window: {since:%Y-%m-%d %H:%M:%SZ} -> {end:%Y-%m-%d %H:%M:%SZ} ({args.since})")
        print(failures.describe())
        for error, count in failures.top_errors:
            print(f"  {count:>6}  {error[:110]}")
        print(coverage.describe())
        print(f"{rush_summary(rows)} [{report_day.isoformat()}, {tz}]")
        gate_ok = failures.passes and coverage.passes
        print(f"gate: {'PASS' if gate_ok else 'FAIL'}")
        return 0 if gate_ok else 1
    finally:
        store.close()


def _local_today(now: datetime, tz: str) -> date:
    return now.astimezone(ZoneInfo(tz)).date()


# ---------------------------------------------------------------------------
# chart
# ---------------------------------------------------------------------------


def _chart(args: argparse.Namespace, settings: Settings, vision: VisionSettings) -> int:
    store = _open_reader(args, settings)
    if store is None:
        return 2
    try:
        tz = args.tz or vision.timezone
        day = args.day or _local_today(datetime.now(UTC), tz)
        rows = hourly_rush(store, day, tz=tz)
        try:
            out = render_rush_chart(rows, Path(args.out), day=day, tz=tz)
        except NoChartData as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {out} ({len(rows)}/24 hours with data)")
        print(rush_summary(rows))
        return 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nyc-vision",
        description="YOLO density pipeline over NYC DOT camera frames.",
    )
    parser.add_argument("--db", help="DuckDB path (default: NYC_LIVE_DUCKDB_PATH)")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("run", "sample cameras on an interval until interrupted"),
        ("once", "run a single tick and exit"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--cameras", type=int, help="override NYC_VISION_CAMERAS")
        if name == "run":
            p.add_argument("--interval", type=float, help="override NYC_VISION_INTERVAL_S")
            p.add_argument("--max-ticks", type=int, help="stop after N ticks (testing)")

    p_report = sub.add_parser("report", help="gate measurements from the tables")
    p_report.add_argument("--since", type=parse_duration, default=timedelta(hours=24))
    p_report.add_argument("--day", type=parse_day, help="local day for the rush summary")
    p_report.add_argument("--tz", help="override NYC_VISION_TZ")

    p_chart = sub.add_parser("chart", help="render the AM/PM rush chart")
    p_chart.add_argument("--day", type=parse_day, help="local day (default: today)")
    p_chart.add_argument("--out", default=str(DEFAULT_OUT))
    p_chart.add_argument("--tz", help="override NYC_VISION_TZ")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = get_settings()
    vision = get_vision_settings()
    if args.command in {"run", "once"}:
        if not hasattr(args, "interval"):
            args.interval = None
        if not hasattr(args, "max_ticks"):
            args.max_ticks = 1
        return asyncio.run(_pipeline_session(args, settings, vision))
    if args.command == "report":
        return _report(args, settings, vision)
    if args.command == "chart":
        return _chart(args, settings, vision)
    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
