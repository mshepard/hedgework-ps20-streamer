"""YOLO inference backends for wildlife detection."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

from streamer.wildlife.types import Detection

logger = logging.getLogger("streamer.wildlife.detector")


class WildlifeDetectorBackend(ABC):
    @abstractmethod
    def load(self) -> None:
        """Load model weights. Raises if unavailable."""

    @abstractmethod
    def detect(self, rgb: np.ndarray) -> list[Detection]:
        """Run inference on an RGB frame."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        ...


class MockWildlifeDetector(WildlifeDetectorBackend):
    """No-op backend for bench/dev when no model is present."""

    def __init__(self, reason: str = "no model configured") -> None:
        self._reason = reason

    def load(self) -> None:
        logger.warning("Wildlife detector running in mock mode: %s", self._reason)

    def detect(self, rgb: np.ndarray) -> list[Detection]:
        return []

    @property
    def backend_name(self) -> str:
        return "mock"


class HailoWildlifeDetector(WildlifeDetectorBackend):
    """Hailo HEF inference via hailo_platform (Pi AI HAT+)."""

    def __init__(
        self,
        hef_path: Path,
        labels_path: Path | None = None,
        inference_size: tuple[int, int] = (640, 640),
    ) -> None:
        self._hef_path = hef_path
        self._labels_path = labels_path
        self._inference_size = inference_size
        self._labels: list[str] = []
        self._device: Any = None
        self._network_group: Any = None
        self._input_vstreams: Any = None
        self._output_vstreams: Any = None

    def load(self) -> None:
        if not self._hef_path.is_file():
            raise FileNotFoundError(f"HEF model not found: {self._hef_path}")

        if self._labels_path and self._labels_path.is_file():
            self._labels = self._load_labels(self._labels_path)
        else:
            labels_json = self._hef_path.with_suffix(".json")
            if labels_json.is_file():
                self._labels = self._load_labels(labels_json)

        from hailo_platform import (  # noqa: PLC0415
            HEF,
            InferVStreams,
            VDevice,
            ConfigureParams,
            HailoStreamInterface,
        )

        hef = HEF(str(self._hef_path))
        params = ConfigureParams.create_from_hef(
            hef, interface=HailoStreamInterface.PCIe
        )
        self._device = VDevice()
        network_groups = self._device.configure(hef, params)
        self._network_group = network_groups[0]
        self._network_group_params = self._network_group.create_params()
        self._infer_pipeline = InferVStreams(
            self._network_group,
            self._network_group_params,
        )
        logger.info(
            "Loaded Hailo model %s (%d classes)",
            self._hef_path.name,
            len(self._labels),
        )

    def detect(self, rgb: np.ndarray) -> list[Detection]:
        if self._network_group is None:
            return []

        input_data = self._preprocess(rgb)
        with self._infer_pipeline as pipeline:
            bindings = self._network_group.create_bindings(
                self._network_group_params
            )
            bindings.input().set_buffer(input_data)
            self._network_group.run(bindings)
            outputs = {
                name: np.array(bindings.output(name).get_buffer())
                for name in bindings._output_names
            }
        return self._parse_yolo_outputs(outputs, rgb.shape[1], rgb.shape[0])

    def _preprocess(self, rgb: np.ndarray) -> np.ndarray:
        from PIL import Image  # noqa: PLC0415

        image = Image.fromarray(rgb, mode="RGB")
        image = image.resize(self._inference_size, Image.Resampling.BILINEAR)
        arr = np.asarray(image, dtype=np.uint8)
        return np.expand_dims(arr, axis=0)

    def _parse_yolo_outputs(
        self, outputs: dict[str, np.ndarray], width: int, height: int
    ) -> list[Detection]:
        # Hailo post-processing varies by compiled graph. This parser
        # handles the common YOLOv8 HEF layout: a single tensor shaped
        # (1, 4+nc, N) or (1, N, 4+nc). Tune when the first real HEF
        # is compiled from the PS 20 training run.
        if not outputs:
            return []
        tensor = next(iter(outputs.values()))
        if tensor.ndim == 3 and tensor.shape[1] < tensor.shape[2]:
            tensor = np.transpose(tensor, (0, 2, 1))
        preds = tensor[0]
        detections: list[Detection] = []
        for row in preds:
            if row.shape[0] < 5:
                continue
            x, y, w, h = row[0:4]
            scores = row[4:]
            if scores.size == 0:
                continue
            class_id = int(np.argmax(scores))
            confidence = float(scores[class_id])
            if confidence < 0.01:
                continue
            species = (
                self._labels[class_id]
                if class_id < len(self._labels)
                else f"class_{class_id}"
            )
            detections.append(
                Detection(
                    species=species,
                    confidence=confidence,
                    x_center=float(x),
                    y_center=float(y),
                    width=float(w),
                    height=float(h),
                )
            )
        return detections

    @staticmethod
    def _load_labels(path: Path) -> list[str]:
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "names" in data:
                names = data["names"]
                if isinstance(names, dict):
                    return [names[str(i)] for i in range(len(names))]
                return list(names)
            if isinstance(data, list):
                return data
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return lines

    @property
    def backend_name(self) -> str:
        return "hailo"


def build_detector(
    model_path: str,
    labels_path: str,
    inference_size: tuple[int, int],
) -> WildlifeDetectorBackend:
    path = Path(model_path)
    labels = Path(labels_path) if labels_path else None

    if path.suffix.lower() == ".hef":
        try:
            return HailoWildlifeDetector(path, labels, inference_size)
        except ImportError:
            return MockWildlifeDetector("hailo_platform not installed")

    if not path.is_file():
        return MockWildlifeDetector(f"model not found: {path}")

    return MockWildlifeDetector(f"unsupported model type: {path.suffix}")
