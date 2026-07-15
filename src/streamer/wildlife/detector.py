"""YOLO inference backends for wildlife detection."""

from __future__ import annotations

import json
import logging
import threading
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


class SharedHailoContext:
    """One Hailo VDevice shared by every wildlife model in this process."""

    def __init__(self, group_id: str = "ps20_wildlife") -> None:
        self._group_id = group_id
        self._device: Any = None
        # Production detection loops call ``detect`` from separate worker
        # threads. Serialize HailoRT activation/inference until the Python
        # API explicitly guarantees concurrent calls on one VDevice.
        self.inference_lock = threading.Lock()

    @property
    def device(self) -> Any:
        if self._device is None:
            from hailo_platform import VDevice  # noqa: PLC0415

            params = VDevice.create_params()
            params.group_id = self._group_id
            self._device = VDevice(params)
            logger.info(
                "Opened shared Hailo VDevice (group_id=%s)",
                self._group_id,
            )
        return self._device

    def close(self) -> None:
        device, self._device = self._device, None
        if device is None:
            return
        release = getattr(device, "release", None)
        if callable(release):
            release()
        logger.info("Closed shared Hailo VDevice")


class HailoWildlifeDetector(WildlifeDetectorBackend):
    """Hailo HEF inference via hailo_platform (Pi AI HAT+)."""

    def __init__(
        self,
        hef_path: Path,
        labels_path: Path | None = None,
        inference_size: tuple[int, int] = (640, 640),
        shared_context: SharedHailoContext | None = None,
    ) -> None:
        self._hef_path = hef_path
        self._labels_path = labels_path
        self._inference_size = inference_size
        self._context = shared_context or SharedHailoContext()
        self._labels: list[str] = []
        self._network_group: Any = None
        self._network_group_params: Any = None
        self._input_vstreams_params: Any = None
        self._output_vstreams_params: Any = None
        self._input_name = ""

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
            ConfigureParams,
            FormatType,
            HailoStreamInterface,
            InputVStreamParams,
            OutputVStreamParams,
        )

        hef = HEF(str(self._hef_path))
        params = ConfigureParams.create_from_hef(
            hef, interface=HailoStreamInterface.PCIe
        )
        network_groups = self._context.device.configure(hef, params)
        self._network_group = network_groups[0]
        self._network_group_params = self._network_group.create_params()
        self._input_name = hef.get_input_vstream_infos()[0].name
        self._input_vstreams_params = (
            InputVStreamParams.make_from_network_group(
                self._network_group,
                quantized=False,
                format_type=FormatType.FLOAT32,
            )
        )
        self._output_vstreams_params = (
            OutputVStreamParams.make_from_network_group(
                self._network_group,
                quantized=False,
                format_type=FormatType.FLOAT32,
            )
        )
        logger.info(
            "Loaded Hailo model %s (%d classes) on shared VDevice",
            self._hef_path.name,
            len(self._labels),
        )

    def detect(self, rgb: np.ndarray) -> list[Detection]:
        if self._network_group is None:
            return []

        from hailo_platform import InferVStreams  # noqa: PLC0415

        input_data = self._preprocess(rgb).astype(np.float32)
        with self._context.inference_lock:
            with self._network_group.activate(self._network_group_params):
                with InferVStreams(
                    self._network_group,
                    self._input_vstreams_params,
                    self._output_vstreams_params,
                    tf_nms_format=True,
                ) as infer_pipeline:
                    outputs = infer_pipeline.infer(
                        {self._input_name: input_data}
                    )
        return self._parse_yolo_outputs(outputs, rgb.shape[1], rgb.shape[0])

    def _preprocess(self, rgb: np.ndarray) -> np.ndarray:
        from PIL import Image  # noqa: PLC0415

        image = Image.fromarray(rgb, mode="RGB")
        image = image.resize(self._inference_size, Image.Resampling.BILINEAR)
        arr = np.asarray(image, dtype=np.uint8)
        return np.expand_dims(arr, axis=0)

    def _species_name(self, class_id: int) -> str:
        if class_id < len(self._labels):
            return self._labels[class_id]
        return f"class_{class_id}"

    def _parse_nms_output(self, tensor: np.ndarray) -> list[Detection]:
        """Parse Hailo NMS output from InferVStreams."""

        if tensor.ndim == 4:
            tensor = tensor[0]

        num_classes, _, num_detections = tensor.shape
        detections: list[Detection] = []
        for class_id in range(num_classes):
            for det_id in range(num_detections):
                ymin, xmin, ymax, xmax, confidence = (
                    tensor[class_id, :, det_id]
                )
                confidence = float(confidence)
                if confidence < 0.01:
                    continue
                detections.append(
                    Detection(
                        species=self._species_name(class_id),
                        confidence=confidence,
                        x_center=(float(xmin) + float(xmax)) / 2,
                        y_center=(float(ymin) + float(ymax)) / 2,
                        width=float(xmax) - float(xmin),
                        height=float(ymax) - float(ymin),
                    )
                )
        return detections

    def _parse_raw_yolo_output(
        self, tensor: np.ndarray
    ) -> list[Detection]:
        """Parse raw YOLO tensor layouts from custom HEF exports."""

        if tensor.ndim == 3 and tensor.shape[1] < tensor.shape[2]:
            tensor = np.transpose(tensor, (0, 2, 1))
        if tensor.ndim == 3:
            preds = tensor[0]
        elif tensor.ndim == 2:
            preds = tensor
        else:
            if tensor.ndim == 0:
                return []
            preds = tensor.reshape(-1, tensor.shape[-1])

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
            detections.append(
                Detection(
                    species=self._species_name(class_id),
                    confidence=confidence,
                    x_center=float(x),
                    y_center=float(y),
                    width=float(w),
                    height=float(h),
                )
            )
        return detections

    def _parse_yolo_outputs(
        self, outputs: dict[str, Any], width: int, height: int
    ) -> list[Detection]:
        del width, height  # HEF outputs use normalized 0..1 coordinates.
        if not outputs:
            return []
        tensor = np.asarray(next(iter(outputs.values())))
        nms_tensor = tensor[0] if tensor.ndim == 4 else tensor
        if nms_tensor.ndim == 3 and nms_tensor.shape[1] == 5:
            return self._parse_nms_output(tensor)
        return self._parse_raw_yolo_output(tensor)

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
    shared_hailo: SharedHailoContext | None = None,
) -> WildlifeDetectorBackend:
    path = Path(model_path)
    labels = Path(labels_path) if labels_path else None

    if path.suffix.lower() == ".hef":
        return HailoWildlifeDetector(
            path,
            labels,
            inference_size,
            shared_context=shared_hailo,
        )

    if not path.is_file():
        return MockWildlifeDetector(f"model not found: {path}")

    return MockWildlifeDetector(f"unsupported model type: {path.suffix}")
