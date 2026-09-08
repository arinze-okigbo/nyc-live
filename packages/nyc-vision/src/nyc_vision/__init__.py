"""nyc-vision: YOLO density over NYC DOT camera frames.

Counts and box statistics are persisted to DuckDB (`density_samples`), frame-fetch
telemetry to `camera_frame_fetches`. Raw frames never leave the FrameSource's
in-memory buffer; nothing in this package writes pixels anywhere. See docs/privacy.md.

`ultralytics` and `torch` are imported lazily inside `YoloDetector`, so importing this
package (and running its tests) works without them installed.
"""

from nyc_vision.config import VisionSettings, get_vision_settings
from nyc_vision.detector import (
    COCO_CLASS_MAP,
    ClassStats,
    DetectionError,
    DetectionResult,
    Detector,
    FakeDetector,
    YoloDetector,
)
from nyc_vision.pipeline import DensityPipeline, TickResult, build_samples, run_forever
from nyc_vision.report import (
    Coverage,
    FailureRate,
    HourlyRush,
    cameras_covered,
    frame_failure_rate,
    hourly_rush,
)
from nyc_vision.sampling import SelectionStats, select_cameras

__version__ = "0.1.0"

__all__ = [
    "COCO_CLASS_MAP",
    "ClassStats",
    "Coverage",
    "DensityPipeline",
    "DetectionError",
    "DetectionResult",
    "Detector",
    "FailureRate",
    "FakeDetector",
    "HourlyRush",
    "SelectionStats",
    "TickResult",
    "VisionSettings",
    "YoloDetector",
    "__version__",
    "build_samples",
    "cameras_covered",
    "frame_failure_rate",
    "get_vision_settings",
    "hourly_rush",
    "run_forever",
    "select_cameras",
]
