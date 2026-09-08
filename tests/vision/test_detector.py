from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pytest

from nyc_live.contracts import DetectionClass
from nyc_vision.detector import (
    COCO_CLASS_MAP,
    ClassStats,
    DetectionError,
    DetectionResult,
    Detector,
    FakeDetector,
    YoloDetector,
    decode_jpeg,
    summarize_boxes,
    summarize_result,
)
from tests.vision.conftest import synthetic_jpeg


class FakeBoxes:
    """Stands in for ultralytics `Results.boxes`: the three arrays we actually read."""

    def __init__(self, cls: list[float], conf: list[float], xyxy: list[list[float]]) -> None:
        self.cls = np.array(cls, dtype=np.float32)
        self.conf = np.array(conf, dtype=np.float32)
        self.xyxy = np.array(xyxy, dtype=np.float32).reshape(-1, 4)

    def __len__(self) -> int:
        return len(self.cls)


class FakeResult:
    def __init__(self, boxes: FakeBoxes | None) -> None:
        self.boxes = boxes


# -- COCO mapping ---------------------------------------------------------


def test_coco_map_covers_every_contract_class_exactly_once() -> None:
    assert set(COCO_CLASS_MAP.values()) == set(DetectionClass)
    assert len(COCO_CLASS_MAP) == len(DetectionClass)
    assert COCO_CLASS_MAP[0] is DetectionClass.PERSON
    assert COCO_CLASS_MAP[1] is DetectionClass.BICYCLE
    assert COCO_CLASS_MAP[2] is DetectionClass.CAR
    assert COCO_CLASS_MAP[3] is DetectionClass.MOTORCYCLE
    assert COCO_CLASS_MAP[5] is DetectionClass.BUS
    assert COCO_CLASS_MAP[7] is DetectionClass.TRUCK
    for ignored in (4, 6, 9, 15, 16):  # aeroplane, train, traffic light, cat, dog
        assert ignored not in COCO_CLASS_MAP


def test_summarize_result_maps_classes_and_ignores_the_rest() -> None:
    result = FakeResult(
        FakeBoxes(
            cls=[0, 0, 2, 7, 9, 16],  # 2 person, 1 car, 1 truck, 1 traffic light, 1 dog
            conf=[0.9, 0.7, 0.8, 0.6, 0.99, 0.99],
            xyxy=[
                [0, 0, 10, 10],
                [10, 10, 30, 30],
                [0, 0, 100, 50],
                [0, 0, 50, 50],
                [0, 0, 200, 200],
                [0, 0, 200, 200],
            ],
        )
    )
    stats = summarize_result(result, width=100, height=100)

    assert set(stats) == {DetectionClass.PERSON, DetectionClass.CAR, DetectionClass.TRUCK}
    assert stats[DetectionClass.PERSON].count == 2
    assert stats[DetectionClass.PERSON].confidence_mean == pytest.approx(0.8, abs=1e-4)
    # boxes are 100 and 400 px of a 10 000 px frame -> mean fraction 0.025
    assert stats[DetectionClass.PERSON].bbox_area_frac_mean == pytest.approx(0.025, abs=1e-6)
    assert stats[DetectionClass.CAR].bbox_area_frac_mean == pytest.approx(0.5, abs=1e-6)
    assert DetectionClass.BICYCLE not in stats


def test_summarize_result_handles_no_detections() -> None:
    assert summarize_result(FakeResult(FakeBoxes([], [], [])), width=64, height=48) == {}
    assert summarize_result(FakeResult(None), width=64, height=48) == {}
    assert summarize_result(object(), width=64, height=48) == {}


def test_bbox_fraction_is_clamped_into_the_contract_range() -> None:
    stats = summarize_boxes([0], [1.4], [[-50, -50, 500, 500]], width=100, height=100)
    person = stats[DetectionClass.PERSON]
    assert person.bbox_area_frac_mean is not None
    assert 0.0 <= person.bbox_area_frac_mean <= 1.0
    assert person.confidence_mean == 1.0


def test_summarize_boxes_accepts_torch_like_objects() -> None:
    class TensorLike:
        def __init__(self, values: object) -> None:
            self._values = np.asarray(values, dtype=np.float32)

        def cpu(self) -> TensorLike:
            return self

        def numpy(self) -> np.ndarray:
            return self._values

        def __len__(self) -> int:
            return len(self._values)

    stats = summarize_boxes(
        TensorLike([2.0, 5.0]),
        TensorLike([0.5, 0.5]),
        TensorLike([[0, 0, 10, 10], [0, 0, 10, 10]]),
        width=100,
        height=100,
    )
    assert stats[DetectionClass.CAR].count == 1
    assert stats[DetectionClass.BUS].count == 1


# -- decoding -------------------------------------------------------------


def test_decode_jpeg_returns_rgb_with_the_right_size() -> None:
    image = decode_jpeg(synthetic_jpeg(width=120, height=90))
    assert image.size == (120, 90)
    assert image.mode == "RGB"


def test_decode_jpeg_rejects_a_non_image_body() -> None:
    with pytest.raises(DetectionError) as excinfo:
        decode_jpeg(b"<html>404 not found</html>")
    assert "could not decode frame" in str(excinfo.value)


# -- YoloDetector without ultralytics -------------------------------------


def test_yolo_detector_constructs_without_importing_ultralytics() -> None:
    before = "ultralytics" in sys.modules
    detector = YoloDetector("yolo11n.pt", device="cpu")
    assert detector.model_name == "yolo11n.pt"
    assert detector.device == "cpu"
    assert detector.loaded is False
    assert ("ultralytics" in sys.modules) is before


@pytest.mark.skipif(
    importlib.util.find_spec("ultralytics") is not None,
    reason="ultralytics is installed; this asserts the message shown when it is not",
)
def test_yolo_detector_reports_a_useful_error_without_ultralytics() -> None:
    with pytest.raises(DetectionError) as excinfo:
        YoloDetector().detect(synthetic_jpeg())
    assert "ultralytics is not installed" in str(excinfo.value)


def test_yolo_detector_is_a_detector() -> None:
    assert isinstance(YoloDetector(), Detector)
    assert isinstance(FakeDetector(), Detector)


# -- FakeDetector ---------------------------------------------------------


def test_fake_detector_is_labelled_synthetic() -> None:
    detector = FakeDetector()
    assert detector.is_synthetic is True
    assert detector.model_name.startswith("fake:")
    result = detector.detect(synthetic_jpeg())
    assert result.model.startswith("fake:")
    assert result.stats(DetectionClass.PERSON).count == 2
    assert result.stats(DetectionClass.BICYCLE) == ClassStats(count=0)
    assert result.total_detections == 5


def test_detection_result_zero_stats_for_absent_class() -> None:
    result = DetectionResult(model="m", inference_ms=1.0, width=1, height=1, classes={})
    stats = result.stats(DetectionClass.TRUCK)
    assert stats.count == 0
    assert stats.confidence_mean is None


# -- the real model (skipped unless ultralytics is importable) ------------


@pytest.mark.slow
@pytest.mark.skipif(
    importlib.util.find_spec("ultralytics") is None,
    reason="ultralytics/torch are not installed in this environment",
)
def test_real_yolo_detector_runs_on_a_synthetic_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point NYC_VISION_MODEL at local weights to avoid the ultralytics download.

    Runs in a temp cwd so that a download, if it happens, does not land in the repo.
    """
    monkeypatch.chdir(tmp_path)
    detector = YoloDetector(os.environ.get("NYC_VISION_MODEL", "yolo11n.pt"), device="cpu")
    result = detector.detect(synthetic_jpeg(width=640, height=480))
    assert result.width == 640
    assert result.height == 480
    assert result.inference_ms > 0
    assert result.model.endswith("yolo11n.pt")
    for cls, stats in result.classes.items():
        assert cls in set(DetectionClass)
        assert stats.count > 0
        assert 0.0 <= (stats.confidence_mean or 0.0) <= 1.0
        assert 0.0 <= (stats.bbox_area_frac_mean or 0.0) <= 1.0
