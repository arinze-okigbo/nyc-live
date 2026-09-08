from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from nyc_vision.chart import DEFAULT_OUT, NoChartData, axis_max, render_rush_chart
from nyc_vision.report import HourlyRush


def rows() -> list[HourlyRush]:
    """A plausible 24 h profile. A LOGIC INPUT for the renderer, not observed data."""
    shape = {
        0: 0.4,
        1: 0.3,
        2: 0.2,
        3: 0.2,
        4: 0.3,
        5: 0.8,
        6: 2.0,
        7: 4.5,
        8: 6.8,
        9: 5.1,
        10: 3.4,
        11: 3.2,
        12: 3.8,
        13: 3.5,
        14: 3.3,
        15: 3.9,
        16: 5.2,
        17: 7.4,
        18: 6.1,
        19: 4.0,
        20: 2.6,
        21: 1.8,
        22: 1.1,
        23: 0.7,
    }
    return [
        HourlyRush(
            hour=hour,
            person_mean=value,
            vehicle_mean=value * 1.8,
            person_max=int(value * 4) + 1,
            vehicle_max=int(value * 7) + 1,
            frames=3600,
            cameras=60,
        )
        for hour, value in shape.items()
    ]


def test_chart_refuses_to_draw_without_data(tmp_path: Path) -> None:
    out = tmp_path / "vision-rush.png"
    with pytest.raises(NoChartData) as excinfo:
        render_rush_chart([], out, day=date(2026, 9, 8))
    assert "density_samples has nothing" in str(excinfo.value)
    assert not out.exists()


def test_chart_renders_a_png(tmp_path: Path) -> None:
    out = render_rush_chart(rows(), tmp_path / "vision-rush.png", day=date(2026, 9, 8))
    assert out.exists()
    with Image.open(out) as img:
        assert img.format == "PNG"
        assert img.size == (980, 470)
        colours = {c for _, c in (img.convert("RGB").getcolors(maxcolors=100000) or [])}
    assert (31, 119, 180) in colours  # person line
    assert (255, 127, 14) in colours  # vehicle line
    assert (243, 244, 246) in colours  # rush bands


def test_chart_creates_the_output_directory(tmp_path: Path) -> None:
    out = render_rush_chart(rows(), tmp_path / "docs" / "vision-rush.png", day=date(2026, 9, 8))
    assert out.parent.is_dir()
    assert out.stat().st_size > 1000


def test_chart_survives_hours_with_gaps(tmp_path: Path) -> None:
    partial = [r for r in rows() if r.hour in {0, 1, 8, 9, 17}]
    out = render_rush_chart(partial, tmp_path / "gap.png", day=date(2026, 9, 8))
    assert out.exists()


def test_chart_handles_an_all_zero_day(tmp_path: Path) -> None:
    flat = [
        HourlyRush(
            hour=h,
            person_mean=0.0,
            vehicle_mean=0.0,
            person_max=0,
            vehicle_max=0,
            frames=6,
            cameras=2,
        )
        for h in range(24)
    ]
    out = render_rush_chart(flat, tmp_path / "flat.png", day=date(2026, 9, 8))
    assert out.exists()


def test_axis_max_is_round_and_covers_the_data() -> None:
    for value in (0.03, 0.4, 1.0, 3.7, 18.0, 240.0):
        top, step = axis_max(value)
        assert top >= value
        assert step > 0
        assert 3 <= round(top / step) <= 12
    assert axis_max(0.0) == (1.0, 0.25)


def test_default_output_path_is_the_documented_one() -> None:
    assert Path("docs/vision-rush.png") == DEFAULT_OUT
