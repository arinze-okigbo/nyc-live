"""Detection: the `Detector` protocol, the ultralytics-backed `YoloDetector`, and `FakeDetector`.

Ultralytics / torch are imported LAZILY, inside `YoloDetector._load()`, via
`importlib.import_module`. Importing `nyc_vision.detector` (and therefore the whole
package and its tests) works with neither installed; only actually running a real
detection needs them. That keeps `just check` green on a machine without the
pytorch-cpu wheels while the pipeline still uses the real model in production.

Only the six `DetectionClass` members are kept. Every other COCO class ultralytics
reports (traffic light, dog, handbag, ...) is dropped on the floor, counted nowhere.

Nothing here ever touches disk: `detect()` takes JPEG bytes from the FrameSource
buffer and returns counts + box statistics. Pixels are never written anywhere.
"""

from __future__ import annotations

import importlib
import io
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np
from PIL import Image, UnidentifiedImageError

from nyc_live.contracts import DetectionClass

log = logging.getLogger(__name__)

DEFAULT_MODEL = "yolo11n.pt"
DEFAULT_DEVICE = "cpu"
DEFAULT_CONFIDENCE = 0.25
DEFAULT_IMGSZ = 640

COCO_CLASS_MAP: Mapping[int, DetectionClass] = {
    0: DetectionClass.PERSON,
    1: DetectionClass.BICYCLE,
    2: DetectionClass.CAR,
    3: DetectionClass.MOTORCYCLE,
    5: DetectionClass.BUS,
    7: DetectionClass.TRUCK,
}
"""COCO ids -> contract classes. 4 (aeroplane) and 6 (train) are deliberately absent."""


class DetectionError(Exception):
    """A frame could not be decoded or the model could not run on it.

    The pipeline logs and counts this per camera; it never aborts a tick and it is
    never recorded as a frame-fetch failure (that metric belongs to the FrameSource).
    """


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassStats:
    """Per-class summary of one frame. Counts and box statistics only."""

    count: int
    confidence_mean: float | None = None
    bbox_area_frac_mean: float | None = None


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """What every `Detector` returns for one frame."""

    model: str
    inference_ms: float
    width: int
    height: int
    classes: Mapping[DetectionClass, ClassStats] = field(default_factory=dict)

    def stats(self, cls: DetectionClass) -> ClassStats:
        """Stats for a class, or an explicit zero. Absence is a real observation."""
        return self.classes.get(cls, ClassStats(count=0))

    @property
    def total_detections(self) -> int:
        return sum(s.count for s in self.classes.values())


@runtime_checkable
class Detector(Protocol):
    """Anything that turns JPEG bytes into per-class counts.

    Implementations must be safe to call from a worker thread and must raise
    `DetectionError` (not a bare exception) for an undecodable or unusable frame.
    """

    @property
    def model_name(self) -> str: ...

    def detect(self, jpeg_bytes: bytes) -> DetectionResult: ...


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without a model)
# ---------------------------------------------------------------------------


def decode_jpeg(data: bytes) -> Image.Image:
    """Decode JPEG bytes to an RGB PIL image. Raises DetectionError on anything else."""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise DetectionError(f"could not decode frame ({len(data)} bytes): {exc}") from exc
    if image.mode != "RGB":
        image = image.convert("RGB")
    return image


def _as_float_array(value: Any) -> np.ndarray[Any, np.dtype[Any]]:
    """Torch tensor, numpy array or plain sequence -> float ndarray on the CPU."""
    cpu = getattr(value, "cpu", None)
    if callable(cpu):
        value = cpu()
    numpy_fn = getattr(value, "numpy", None)
    if callable(numpy_fn):
        value = numpy_fn()
    return np.asarray(value, dtype=np.float64)


def summarize_boxes(
    class_ids: Any,
    confidences: Any,
    xyxy: Any,
    *,
    width: int,
    height: int,
) -> dict[DetectionClass, ClassStats]:
    """Group boxes by contract class; mean confidence and mean box-area fraction per class.

    The three arrays are typed `Any` because they arrive as torch tensors from
    ultralytics; anything `_as_float_array` can turn into a float ndarray (tensor,
    ndarray, nested sequence) is accepted. Unmapped COCO ids are ignored. Area fraction is `(x2-x1)*(y2-y1)/(width*height)`
    clamped into [0, 1] so a box that overhangs the frame cannot produce a value the
    frozen `DensitySample` bounds would reject.
    """
    ids = _as_float_array(class_ids).reshape(-1)
    conf = _as_float_array(confidences).reshape(-1)
    boxes = _as_float_array(xyxy).reshape(-1, 4) if len(ids) else np.zeros((0, 4))
    n = min(len(ids), len(conf), len(boxes))
    frame_area = float(width * height)
    acc: dict[DetectionClass, list[tuple[float, float | None]]] = {}
    for i in range(n):
        cls = COCO_CLASS_MAP.get(round(float(ids[i])))
        if cls is None:
            continue
        c = min(max(float(conf[i]), 0.0), 1.0)
        x1, y1, x2, y2 = (float(v) for v in boxes[i])
        frac: float | None = None
        if frame_area > 0:
            frac = min(max(abs(x2 - x1) * abs(y2 - y1) / frame_area, 0.0), 1.0)
        acc.setdefault(cls, []).append((c, frac))
    out: dict[DetectionClass, ClassStats] = {}
    for cls, items in acc.items():
        confs = [c for c, _ in items]
        fracs = [f for _, f in items if f is not None]
        out[cls] = ClassStats(
            count=len(items),
            confidence_mean=round(sum(confs) / len(confs), 4),
            bbox_area_frac_mean=round(sum(fracs) / len(fracs), 6) if fracs else None,
        )
    return out


def summarize_result(result: Any, *, width: int, height: int) -> dict[DetectionClass, ClassStats]:
    """Read `result.boxes.{cls,conf,xyxy}` off an ultralytics `Results` object.

    Kept separate from `YoloDetector` so the class mapping can be tested against a
    stand-in result object without loading a model.
    """
    boxes = getattr(result, "boxes", None)
    if boxes is None:
        return {}
    class_ids = getattr(boxes, "cls", None)
    confidences = getattr(boxes, "conf", None)
    xyxy = getattr(boxes, "xyxy", None)
    if class_ids is None or confidences is None or xyxy is None:
        return {}
    if len(_as_float_array(class_ids).reshape(-1)) == 0:
        return {}
    return summarize_boxes(class_ids, confidences, xyxy, width=width, height=height)


# ---------------------------------------------------------------------------
# The real detector
# ---------------------------------------------------------------------------


class YoloDetector:
    """Ultralytics YOLO over JPEG bytes. Model + torch are loaded on first `detect()`.

    Device selection (`NYC_VISION_DEVICE`):

    * `cpu`   - default, and what the Linux pytorch-cpu wheels give you.
    * `mps`   - Apple Silicon. macOS default torch wheels ship MPS; no extra install.
    * `cuda:0`- passed straight through if someone has an NVIDIA box.

    The model is loaded once and reused; `detect()` holds a lock so the same
    `YoloDetector` can be handed to `asyncio.to_thread` from several tasks.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        device: str = DEFAULT_DEVICE,
        confidence: float = DEFAULT_CONFIDENCE,
        imgsz: int = DEFAULT_IMGSZ,
    ) -> None:
        self._model_name = model
        self.device = device
        self.confidence = confidence
        self.imgsz = imgsz
        self._model: Any | None = None
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> Any:
        """Import ultralytics and instantiate the model. Idempotent, thread-safe."""
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            # `Any` on purpose: ultralytics is an optional, lazily imported dependency and
            # must not be resolvable at type-check time on a machine without torch.
            ultralytics: Any = importlib.import_module("ultralytics")
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise DetectionError(
                "ultralytics is not installed; nyc-vision needs `ultralytics`, `torch` and "
                "`torchvision` (see packages/nyc-vision/README.md for the dependency lines)"
            ) from exc
        yolo_cls = ultralytics.YOLO
        started = time.perf_counter()
        model = yolo_cls(self._model_name)
        try:
            model.to(self.device)
        except Exception as exc:
            raise DetectionError(
                f"could not move model {self._model_name!r} to device {self.device!r}: {exc}"
            ) from exc
        log.info(
            "loaded %s on %s in %.0f ms",
            self._model_name,
            self.device,
            (time.perf_counter() - started) * 1000,
        )
        self._model = model
        return model

    def detect(self, jpeg_bytes: bytes) -> DetectionResult:
        image = decode_jpeg(jpeg_bytes)
        width, height = image.size
        with self._lock:
            model = self._load_locked()
            started = time.perf_counter()
            try:
                results = model.predict(
                    source=image,
                    device=self.device,
                    conf=self.confidence,
                    imgsz=self.imgsz,
                    verbose=False,
                )
            except Exception as exc:
                raise DetectionError(f"{self._model_name} inference failed: {exc}") from exc
            inference_ms = round((time.perf_counter() - started) * 1000, 1)
        if not results:
            raise DetectionError(f"{self._model_name} returned no result for a decoded frame")
        classes = summarize_result(results[0], width=width, height=height)
        return DetectionResult(
            model=self._model_name,
            inference_ms=inference_ms,
            width=width,
            height=height,
            classes=classes,
        )


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class FakeDetector:
    """A LOGIC INPUT FOR TESTS ONLY. Returns fixed counts; it observes nothing.

    Its `model_name` is deliberately `fake:<...>` so that any `density_samples` row it
    produced is obviously synthetic on inspection. It exists so the pipeline, the store
    writes and the report SQL can be exercised without torch. It must never be wired
    into `nyc-vision run` against the real DuckDB file, and no number it returns is ever
    presented as an observation of the world.
    """

    is_synthetic = True

    def __init__(
        self,
        counts: Mapping[DetectionClass, int] | None = None,
        *,
        name: str = "fake",
        confidence: float = 0.9,
        bbox_area_frac: float = 0.01,
        width: int = 640,
        height: int = 360,
        inference_ms: float = 1.0,
        fails_on: Callable[[bytes], bool] | None = None,
    ) -> None:
        self.counts = dict(counts or {DetectionClass.PERSON: 2, DetectionClass.CAR: 3})
        self._model_name = f"fake:{name}"
        self.confidence = confidence
        self.bbox_area_frac = bbox_area_frac
        self.width = width
        self.height = height
        self.inference_ms = inference_ms
        self.fails_on = fails_on
        self.calls = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    def detect(self, jpeg_bytes: bytes) -> DetectionResult:
        self.calls += 1
        if self.fails_on is not None and self.fails_on(jpeg_bytes):
            raise DetectionError("FakeDetector was configured to fail on this frame")
        return DetectionResult(
            model=self._model_name,
            inference_ms=self.inference_ms,
            width=self.width,
            height=self.height,
            classes={
                cls: ClassStats(
                    count=count,
                    confidence_mean=self.confidence if count else None,
                    bbox_area_frac_mean=self.bbox_area_frac if count else None,
                )
                for cls, count in self.counts.items()
            },
        )
