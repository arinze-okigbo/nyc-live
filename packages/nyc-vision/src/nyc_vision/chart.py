"""Render the rush-hour chart with Pillow. No matplotlib: it is not a dependency.

`render_rush_chart` draws the per-hour person and vehicle means produced by
`report.hourly_rush` as two polylines over a 24 h local-time axis, with the AM
(07-10) and PM (16-19) rush windows shaded so the gate is readable at a glance.

It refuses to draw anything when there are no rows: `NoChartData` is raised and the
CLI exits non-zero. An empty or invented chart would be worse than no chart. Hours
with no frames are left as gaps in the line, not interpolated and not zeroed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from nyc_vision.report import DEFAULT_TZ, HourlyRush

DEFAULT_OUT = Path("docs/vision-rush.png")

WIDTH = 980
HEIGHT = 470
MARGIN_LEFT = 74
MARGIN_RIGHT = 26
MARGIN_TOP = 72
MARGIN_BOTTOM = 66

BG = (255, 255, 255)
INK = (24, 24, 27)
MUTED = (113, 113, 122)
GRID = (228, 228, 231)
RUSH_BAND = (243, 244, 246)
PERSON = (31, 119, 180)
VEHICLE = (255, 127, 14)

AM_RUSH = (7, 10)
PM_RUSH = (16, 19)


class NoChartData(Exception):
    """Raised instead of drawing an empty chart."""


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        return ImageFont.load_default()


def axis_max(value: float) -> tuple[float, float]:
    """A round upper bound and tick step for a y axis covering [0, value]."""
    if value <= 0:
        return 1.0, 0.25
    raw = value / 5
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = magnitude
    for multiple in (1, 2, 2.5, 5, 10):
        step = multiple * magnitude
        if step >= raw:
            break
    return math.ceil(value / step) * step, step


def render_rush_chart(
    rows: Sequence[HourlyRush],
    out: Path = DEFAULT_OUT,
    *,
    day: date | None = None,
    tz: str = DEFAULT_TZ,
) -> Path:
    """Write the chart PNG and return its path. Raises `NoChartData` on empty input."""
    if not rows:
        raise NoChartData(
            "no hourly rows to chart: density_samples has nothing for that day. "
            "Run `nyc-vision run` for a full day first; an empty chart will not be written."
        )

    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    plot_left = MARGIN_LEFT
    plot_right = WIDTH - MARGIN_RIGHT
    plot_top = MARGIN_TOP
    plot_bottom = HEIGHT - MARGIN_BOTTOM
    plot_w = plot_right - plot_left
    plot_h = plot_bottom - plot_top

    top, step = axis_max(max(max(r.person_mean, r.vehicle_mean) for r in rows))

    def x_of(hour: float) -> float:
        return plot_left + (hour / 23.0) * plot_w

    def y_of(value: float) -> float:
        return plot_bottom - (value / top) * plot_h

    # rush bands
    for lo, hi, label in ((*AM_RUSH, "AM rush"), (*PM_RUSH, "PM rush")):
        draw.rectangle([x_of(lo), plot_top, x_of(hi), plot_bottom], fill=RUSH_BAND)
        draw.text(
            ((x_of(lo) + x_of(hi)) / 2, plot_top + 6),
            label,
            fill=MUTED,
            font=_font(12),
            anchor="ma",
        )

    # y grid + labels
    ticks = round(top / step)
    for i in range(ticks + 1):
        value = i * step
        y = y_of(value)
        draw.line([plot_left, y, plot_right, y], fill=GRID, width=1)
        draw.text((plot_left - 10, y), f"{value:g}", fill=MUTED, font=_font(12), anchor="rm")

    # x axis
    draw.line([plot_left, plot_bottom, plot_right, plot_bottom], fill=INK, width=1)
    draw.line([plot_left, plot_top, plot_left, plot_bottom], fill=INK, width=1)
    for hour in range(0, 24, 2):
        x = x_of(hour)
        draw.line([x, plot_bottom, x, plot_bottom + 4], fill=MUTED, width=1)
        draw.text((x, plot_bottom + 8), f"{hour:02d}", fill=MUTED, font=_font(12), anchor="ma")

    by_hour = {r.hour: r for r in rows}
    for colour, getter in ((PERSON, "person_mean"), (VEHICLE, "vehicle_mean")):
        for hour in range(23):
            a, b = by_hour.get(hour), by_hour.get(hour + 1)
            if a is None or b is None:
                continue  # real gap in the data; do not bridge it
            draw.line(
                [
                    x_of(a.hour),
                    y_of(float(getattr(a, getter))),
                    x_of(b.hour),
                    y_of(float(getattr(b, getter))),
                ],
                fill=colour,
                width=3,
            )
        for row in rows:
            x, y = x_of(row.hour), y_of(float(getattr(row, getter)))
            draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=colour)

    # titles
    title = "NYC camera density by hour of day"
    if day is not None:
        title += f" - {day.isoformat()}"
    draw.text((MARGIN_LEFT, 20), title, fill=INK, font=_font(20), anchor="ls")
    frames = sum(r.frames for r in rows)
    cameras = max(r.cameras for r in rows)
    draw.text(
        (MARGIN_LEFT, 44),
        f"mean detections per frame - {frames} frames, up to {cameras} cameras, "
        f"{len(rows)}/24 hours with data - local time ({tz})",
        fill=MUTED,
        font=_font(12),
        anchor="ls",
    )
    draw.text(
        (MARGIN_LEFT, HEIGHT - 16),
        "source: density_samples (nyc-vision); counts and box statistics only, no frames stored",
        fill=MUTED,
        font=_font(11),
        anchor="ls",
    )

    # legend
    legend_x = plot_right - 190
    for i, (colour, label) in enumerate(((PERSON, "person"), (VEHICLE, "vehicle"))):
        y = 44 - 4
        x = legend_x + i * 95
        draw.rectangle([x, y - 5, x + 18, y + 1], fill=colour)
        draw.text((x + 24, y - 2), label, fill=INK, font=_font(12), anchor="lm")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG")
    return out
