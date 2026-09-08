"""nyc-vision runtime settings, read from the environment exactly like `nyc_live.config`.

Every knob is `NYC_VISION_*`. The core `nyc_live.config.Settings` (data dir, DuckDB
path, HTTP timeouts, upstream base URLs) is separate and unchanged; this module only
adds the vision-specific ones so nothing outside `packages/nyc-vision` has to move.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VisionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),  # we want a field literally called `model`
    )

    cameras: int = Field(
        default=60,
        alias="NYC_VISION_CAMERAS",
        ge=1,
        le=2000,
        description="How many cameras to sample per tick. The gate needs >= 50.",
    )
    interval_s: float = Field(
        default=60.0,
        alias="NYC_VISION_INTERVAL_S",
        gt=0,
        description="Seconds between ticks. A tick that overruns is not compounded.",
    )
    model: str = Field(
        default="yolo11n.pt",
        alias="NYC_VISION_MODEL",
        description="Ultralytics weights name or path. Downloaded on first use by ultralytics.",
    )
    device: str = Field(
        default="cpu",
        alias="NYC_VISION_DEVICE",
        description="Torch device string: 'cpu' (default, Linux), 'mps' (Apple Silicon), 'cuda:0'.",
    )
    confidence: float = Field(default=0.25, alias="NYC_VISION_CONFIDENCE", ge=0.0, le=1.0)
    imgsz: int = Field(default=640, alias="NYC_VISION_IMGSZ", ge=64, le=4096)
    concurrency: int = Field(
        default=4,
        alias="NYC_VISION_CONCURRENCY",
        ge=1,
        le=64,
        description="Cameras fetched in parallel. Detection is serialised regardless.",
    )
    grid: int = Field(
        default=5,
        alias="NYC_VISION_GRID",
        ge=1,
        le=32,
        description="Stratification grid resolution over the NYC bbox (grid x grid cells).",
    )
    archive: bool = Field(
        default=True,
        alias="NYC_VISION_ARCHIVE",
        description="Archive yesterday's density_samples / camera_frame_fetches to Parquet daily.",
    )
    timezone: str = Field(
        default="America/New_York",
        alias="NYC_VISION_TZ",
        description="Local timezone for the rush-hour report and chart. Storage stays UTC.",
    )


@lru_cache(maxsize=1)
def get_vision_settings() -> VisionSettings:
    return VisionSettings()
