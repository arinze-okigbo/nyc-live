from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nyc_live.contracts import CameraFrameFetch, DensitySample, DetectionClass
from nyc_live.store import Store
from nyc_vision.__main__ import build_parser, main, parse_day, parse_duration
from nyc_vision.config import VisionSettings


def seed(path: Path) -> None:
    """Write a small but real set of rows through the frozen store API."""
    with Store(path) as store:
        base = datetime.now(UTC) - timedelta(hours=3)
        store.record_frame_fetches(
            CameraFrameFetch(
                camera_id=f"cam-{i % 4}",
                ts=base + timedelta(seconds=i),
                ok=i % 50 != 0,
                status_code=200 if i % 50 else 502,
                latency_ms=25.0,
                error=None if i % 50 else "upstream_http: 502",
            )
            for i in range(100)
        )
        store.insert_density_samples(
            DensitySample(
                camera_id=f"cam-{i % 4}",
                ts=base + timedelta(minutes=i),
                cls=cls,
                count=3 if cls is DetectionClass.PERSON else 0,
                model="test-model",
            )
            for i in range(30)
            for cls in DetectionClass
        )


def test_parse_duration_forms() -> None:
    assert parse_duration("24h") == timedelta(hours=24)
    assert parse_duration("90m") == timedelta(minutes=90)
    assert parse_duration("30s") == timedelta(seconds=30)
    assert parse_duration("2d") == timedelta(days=2)
    assert parse_duration("3600") == timedelta(seconds=3600)
    with pytest.raises(Exception, match="could not parse duration"):
        parse_duration("a while")


def test_parse_day() -> None:
    assert parse_day("2026-09-08") == date(2026, 9, 8)
    with pytest.raises(Exception, match="expected YYYY-MM-DD"):
        parse_day("08/09/2026")


def test_parser_exposes_the_four_commands() -> None:
    parser = build_parser()
    for argv in (["run"], ["once"], ["report"], ["chart"]):
        assert parser.parse_args(argv).command == argv[0]
    assert parser.parse_args(["report", "--since", "6h"]).since == timedelta(hours=6)
    assert parser.parse_args(["chart", "--day", "2026-09-08"]).day == date(2026, 9, 8)


def test_report_on_a_missing_database_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--db", str(tmp_path / "nope.duckdb"), "report"])
    assert code == 2
    assert "no DuckDB file at" in capsys.readouterr().err


def test_report_prints_the_gate_numbers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "vision.duckdb"
    seed(db)
    code = main(["--db", str(db), "report", "--since", "6h"])
    out = capsys.readouterr().out
    assert "frame-fetch failure rate: 2.000%" in out
    assert "(2/100 attempts over 4 cameras)" in out
    assert "coverage: 4 cameras, 30 frames" in out
    assert "gate: FAIL" in out  # 2 % is not < 2 %, and 4 cameras is not >= 50
    assert code == 1


def test_chart_command_refuses_an_empty_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "vision.duckdb"
    seed(db)
    out_png = tmp_path / "vision-rush.png"
    code = main(["--db", str(db), "chart", "--day", "1999-01-01", "--out", str(out_png)])
    assert code == 1
    assert "density_samples has nothing" in capsys.readouterr().err
    assert not out_png.exists()


def test_chart_command_writes_the_png(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "vision.duckdb"
    seed(db)
    out_png = tmp_path / "docs" / "vision-rush.png"
    code = main(["--db", str(db), "chart", "--out", str(out_png)])
    captured = capsys.readouterr()
    if code == 1:
        # the seeded rows can straddle local midnight; then there is honestly no data
        assert "density_samples has nothing" in captured.err
        return
    assert code == 0
    assert out_png.exists()
    assert f"wrote {out_png}" in captured.out


def test_vision_settings_read_the_documented_env_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in (
        ("NYC_VISION_CAMERAS", "80"),
        ("NYC_VISION_INTERVAL_S", "30"),
        ("NYC_VISION_MODEL", "yolo11s.pt"),
        ("NYC_VISION_DEVICE", "mps"),
        ("NYC_VISION_CONCURRENCY", "8"),
        ("NYC_VISION_GRID", "6"),
        ("NYC_VISION_TZ", "UTC"),
        ("NYC_VISION_ARCHIVE", "false"),
    ):
        monkeypatch.setenv(name, value)
    settings = VisionSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.cameras == 80
    assert settings.interval_s == 30
    assert settings.model == "yolo11s.pt"
    assert settings.device == "mps"
    assert settings.concurrency == 8
    assert settings.grid == 6
    assert settings.timezone == "UTC"
    assert settings.archive is False


def test_vision_settings_defaults() -> None:
    settings = VisionSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.cameras == 60
    assert settings.interval_s == 60.0
    assert settings.model == "yolo11n.pt"
    assert settings.device == "cpu"
    assert settings.timezone == "America/New_York"
