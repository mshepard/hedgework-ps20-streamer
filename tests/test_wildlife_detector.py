from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from streamer.wildlife.detector import (
    HailoWildlifeDetector,
    SharedHailoContext,
    build_detector,
)


class FakeNetworkGroup:
    def create_params(self) -> object:
        return object()


class FakeVDevice:
    instances: list["FakeVDevice"] = []

    @staticmethod
    def create_params() -> types.SimpleNamespace:
        return types.SimpleNamespace(group_id=None)

    def __init__(self, params: types.SimpleNamespace) -> None:
        self.params = params
        self.configured: list[object] = []
        self.released = False
        self.instances.append(self)

    def configure(self, hef: object, params: object) -> list[FakeNetworkGroup]:
        del params
        self.configured.append(hef)
        return [FakeNetworkGroup()]

    def release(self) -> None:
        self.released = True


class FakeHEF:
    def __init__(self, path: str) -> None:
        self.path = path

    def get_input_vstream_infos(self) -> list[types.SimpleNamespace]:
        return [types.SimpleNamespace(name="input")]


class FakeConfigureParams:
    @staticmethod
    def create_from_hef(hef: object, interface: object) -> object:
        del hef, interface
        return object()


class FakeVStreamParams:
    @staticmethod
    def make_from_network_group(
        network_group: object,
        *,
        quantized: bool,
        format_type: object,
    ) -> object:
        del network_group, quantized, format_type
        return object()


def fake_hailo_module() -> types.ModuleType:
    module = types.ModuleType("hailo_platform")
    module.VDevice = FakeVDevice
    module.HEF = FakeHEF
    module.ConfigureParams = FakeConfigureParams
    module.FormatType = types.SimpleNamespace(FLOAT32="float32")
    module.HailoStreamInterface = types.SimpleNamespace(PCIe="pcie")
    module.InputVStreamParams = FakeVStreamParams
    module.OutputVStreamParams = FakeVStreamParams
    return module


class SharedHailoContextTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeVDevice.instances.clear()

    def test_context_opens_one_grouped_device_and_releases_it(self) -> None:
        with patch.dict(sys.modules, {"hailo_platform": fake_hailo_module()}):
            context = SharedHailoContext(group_id="test_group")
            first = context.device
            second = context.device

            self.assertIs(first, second)
            self.assertEqual(1, len(FakeVDevice.instances))
            self.assertEqual("test_group", first.params.group_id)

            context.close()
            self.assertTrue(first.released)

    def test_two_detectors_configure_the_same_device(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bird_hef = root / "bird.hef"
            pollinator_hef = root / "pollinator.hef"
            bird_hef.write_bytes(b"bird")
            pollinator_hef.write_bytes(b"pollinator")
            (root / "bird.json").write_text(
                json.dumps({"names": ["Blue Jay"]}),
                encoding="utf-8",
            )
            (root / "pollinator.json").write_text(
                json.dumps({"names": ["Bumble Bee"]}),
                encoding="utf-8",
            )

            with patch.dict(
                sys.modules, {"hailo_platform": fake_hailo_module()}
            ):
                context = SharedHailoContext()
                bird = HailoWildlifeDetector(
                    bird_hef, shared_context=context
                )
                pollinator = HailoWildlifeDetector(
                    pollinator_hef, shared_context=context
                )
                bird.load()
                pollinator.load()

                self.assertEqual(1, len(FakeVDevice.instances))
                self.assertEqual(2, len(context.device.configured))

    def test_build_detector_preserves_injected_context(self) -> None:
        context = SharedHailoContext()
        detector = build_detector(
            "bird.hef",
            "bird.json",
            (640, 640),
            shared_hailo=context,
        )

        self.assertIsInstance(detector, HailoWildlifeDetector)
        self.assertIs(context, detector._context)


class HailoOutputParserTests(unittest.TestCase):
    def test_empty_nms_output_does_not_fall_back_to_raw_parser(self) -> None:
        detector = HailoWildlifeDetector(Path("bird.hef"))
        tensor = np.zeros((1, 20, 5, 100), dtype=np.float32)
        tensor[0, 0, 0, :] = 0.25

        detections = detector._parse_yolo_outputs(
            {"nms": tensor}, width=1280, height=720
        )

        self.assertEqual([], detections)

    def test_parses_tf_nms_output_with_labels(self) -> None:
        detector = HailoWildlifeDetector(Path("bird.hef"))
        detector._labels = ["Blue Jay", "Eastern Bluebird"]
        tensor = np.zeros((1, 2, 5, 3), dtype=np.float32)
        tensor[0, 1, :, 0] = [0.1, 0.2, 0.5, 0.6, 0.9]

        detections = detector._parse_yolo_outputs(
            {"nms": tensor}, width=1280, height=720
        )

        self.assertEqual(1, len(detections))
        detection = detections[0]
        self.assertEqual("Eastern Bluebird", detection.species)
        self.assertAlmostEqual(0.9, detection.confidence)
        self.assertAlmostEqual(0.4, detection.x_center)
        self.assertAlmostEqual(0.3, detection.y_center)
        self.assertAlmostEqual(0.4, detection.width)
        self.assertAlmostEqual(0.4, detection.height)


if __name__ == "__main__":
    unittest.main()
