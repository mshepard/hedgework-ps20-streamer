#!/usr/bin/env python3
"""Side-by-side dual-camera Hailo preview in a single process.

rpicam-hello opens an exclusive Hailo VDevice per process, so two
instances cannot run inference at the same time (the second prints
"HailoRT not ready!"). This script opens both Pi Camera 3 modules with
picamera2, loads both HEF models on one shared VDevice, and displays a
combined preview window.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parents[1]
HAILO_MODELS_DIR = Path("/usr/share/hailo-models")

DEFAULT_CAM0_HEF = Path("/var/lib/streamer/models/bird_v1.hef")
DEFAULT_CAM1_HEF = Path("/var/lib/streamer/models/pollinator_v1.hef")
DEFAULT_CAM0_LABELS = Path("/var/lib/streamer/models/bird_v1.json")
DEFAULT_CAM1_LABELS = Path("/var/lib/streamer/models/pollinator_v1.json")

# Preinstalled zoo models from `hailo-all` / rpicam-apps demos.
# Prefer Hailo-8 variants first; fall back to Hailo-8L on AI HAT+ boards.
ZOO_CAM0_HEFS = ("yolov8s_h8.hef", "yolov8s_h8l.hef")
ZOO_CAM1_HEFS = ("yolov6n_h8.hef", "yolov6n_h8l.hef")

# COCO class names used by the stock zoo detection models.
COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic_light", "fire_hydrant", "stop_sign",
    "parking_meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports_ball", "kite", "baseball_bat", "baseball_glove", "skateboard",
    "surfboard", "tennis_racket", "bottle", "wine_glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot_dog", "pizza", "donut", "cake", "chair",
    "couch", "potted_plant", "bed", "dining_table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell_phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy_bear", "hair_drier", "toothbrush",
]


@dataclass(frozen=True)
class Detection:
    species: str
    confidence: float
    x_center: float
    y_center: float
    width: float
    height: float

    @property
    def display_name(self) -> str:
        return self.species.replace("_", " ").strip()


@dataclass
class ModelRunner:
    network_group: Any
    network_group_params: Any
    input_vstreams_params: Any
    output_vstreams_params: Any
    input_name: str
    labels: list[str]
    inference_size: tuple[int, int]
    model_name: str

    def detect(self, rgb: np.ndarray, threshold: float) -> list[Detection]:
        from hailo_platform import InferVStreams

        input_data = _preprocess(rgb, self.inference_size).astype(np.float32)
        with self.network_group.activate(self.network_group_params):
            with InferVStreams(
                self.network_group,
                self.input_vstreams_params,
                self.output_vstreams_params,
                tf_nms_format=True,
            ) as infer_pipeline:
                outputs = infer_pipeline.infer({self.input_name: input_data})
        detections = _parse_detection_outputs(
            outputs, self.labels, rgb.shape[1], rgb.shape[0]
        )
        return [d for d in detections if d.confidence >= threshold]


def _resolve_zoo_hef(*candidates: str) -> Path:
    for name in candidates:
        path = HAILO_MODELS_DIR / name
        if path.is_file():
            return path
    tried = ", ".join(candidates)
    raise FileNotFoundError(
        f"No zoo HEF found in {HAILO_MODELS_DIR}. Tried: {tried}"
    )


def _apply_zoo_defaults(args: argparse.Namespace) -> None:
    args.cam0_hef = _resolve_zoo_hef(*ZOO_CAM0_HEFS)
    args.cam1_hef = _resolve_zoo_hef(*ZOO_CAM1_HEFS)
    args.cam0_labels = Path()
    args.cam1_labels = Path()


def _load_labels(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "names" in data:
            names = data["names"]
            if isinstance(names, dict):
                return [names[str(i)] for i in range(len(names))]
            return list(names)
        if isinstance(data, dict) and "labels" in data:
            return list(data["labels"])
        if isinstance(data, list):
            return data
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _preprocess(rgb: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    image = Image.fromarray(rgb, mode="RGB")
    image = image.resize(size, Image.Resampling.BILINEAR)
    arr = np.asarray(image, dtype=np.uint8)
    return np.expand_dims(arr, axis=0)


def _species_name(labels: list[str], class_id: int) -> str:
    if class_id < len(labels):
        return labels[class_id]
    return f"class_{class_id}"


def _parse_nms_outputs(
    tensor: np.ndarray,
    labels: list[str],
) -> list[Detection]:
    """Parse Hailo NMS output from InferVStreams(tf_nms_format=True)."""

    if tensor.ndim == 4:
        tensor = tensor[0]
    if tensor.ndim != 3 or tensor.shape[1] != 5:
        return []

    num_classes, _, num_detections = tensor.shape
    detections: list[Detection] = []
    for class_id in range(num_classes):
        for det_id in range(num_detections):
            ymin, xmin, ymax, xmax, confidence = tensor[class_id, :, det_id]
            confidence = float(confidence)
            if confidence < 0.01:
                continue
            x_center = (float(xmin) + float(xmax)) / 2
            y_center = (float(ymin) + float(ymax)) / 2
            detections.append(
                Detection(
                    species=_species_name(labels, class_id),
                    confidence=confidence,
                    x_center=x_center,
                    y_center=y_center,
                    width=float(xmax) - float(xmin),
                    height=float(ymax) - float(ymin),
                )
            )
    return detections


def _parse_raw_yolo_outputs(
    tensor: np.ndarray,
    labels: list[str],
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
                species=_species_name(labels, class_id),
                confidence=confidence,
                x_center=float(x),
                y_center=float(y),
                width=float(w),
                height=float(h),
            )
        )
    return detections


def _parse_detection_outputs(
    outputs: dict[str, Any],
    labels: list[str],
    width: int,
    height: int,
) -> list[Detection]:
    del width, height  # boxes are already normalized 0..1
    if not outputs:
        return []

    tensor = np.asarray(next(iter(outputs.values())))
    nms_detections = _parse_nms_outputs(tensor, labels)
    if nms_detections:
        return nms_detections
    return _parse_raw_yolo_outputs(tensor, labels)


def _box_pixels(
    detection: Detection, frame_width: int, frame_height: int
) -> tuple[int, int, int, int]:
    x_center = detection.x_center * frame_width
    y_center = detection.y_center * frame_height
    box_w = detection.width * frame_width
    box_h = detection.height * frame_height
    x0 = int(max(0, x_center - box_w / 2))
    y0 = int(max(0, y_center - box_h / 2))
    x1 = int(min(frame_width, x_center + box_w / 2))
    y1 = int(min(frame_height, y_center + box_h / 2))
    return x0, y0, x1, y1


def annotate_frame(
    rgb: np.ndarray, detections: list[Detection], title: str
) -> Image.Image:
    image = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, image.width, 22], fill="#15312b")
    draw.text((6, 4), title, fill="#FAFBFC")
    for detection in detections:
        x0, y0, x1, y1 = _box_pixels(detection, image.width, image.height)
        draw.rectangle([x0, y0, x1, y1], outline="#ff8c44", width=3)
        label = f"{detection.display_name} {detection.confidence:.0%}"
        text_y = max(22, y0 - 18)
        draw.rectangle(
            [x0, text_y, x0 + 8 * len(label), text_y + 18], fill="#15312b"
        )
        draw.text((x0 + 4, text_y + 2), label, fill="#FAFBFC")
    return image


def _open_shared_vdevice() -> Any:
    from hailo_platform import VDevice

    params = VDevice.create_params()
    params.group_id = "SHARED"
    return VDevice(params)


def _load_model(
    vdevice: Any,
    hef_path: Path,
    labels_path: Path | None,
    inference_size: tuple[int, int],
) -> ModelRunner:
    from hailo_platform import (
        ConfigureParams,
        FormatType,
        HEF,
        HailoStreamInterface,
        InputVStreamParams,
        OutputVStreamParams,
    )

    if not hef_path.is_file():
        raise FileNotFoundError(f"HEF model not found: {hef_path}")

    labels = _load_labels(labels_path)
    if not labels:
        fallback = hef_path.with_suffix(".json")
        if fallback.is_file():
            labels = _load_labels(fallback)
    if not labels:
        labels = COCO_LABELS

    hef = HEF(str(hef_path))
    params = ConfigureParams.create_from_hef(
        hef, interface=HailoStreamInterface.PCIe
    )
    network_group = vdevice.configure(hef, params)[0]
    network_group_params = network_group.create_params()
    input_vstream_info = hef.get_input_vstream_infos()[0]
    input_vstreams_params = InputVStreamParams.make_from_network_group(
        network_group,
        quantized=False,
        format_type=FormatType.FLOAT32,
    )
    output_vstreams_params = OutputVStreamParams.make_from_network_group(
        network_group,
        quantized=False,
        format_type=FormatType.FLOAT32,
    )
    return ModelRunner(
        network_group=network_group,
        network_group_params=network_group_params,
        input_vstreams_params=input_vstreams_params,
        output_vstreams_params=output_vstreams_params,
        input_name=input_vstream_info.name,
        labels=labels,
        inference_size=inference_size,
        model_name=hef_path.stem,
    )


def _open_camera(
    camera_num: int, width: int, height: int, framerate: float
) -> Any:
    from picamera2 import Picamera2

    frame_duration_us = int(round(1_000_000 / framerate))
    picam2 = Picamera2(camera_num=camera_num)
    config = picam2.create_video_configuration(
        main={"size": (width, height), "format": "BGR888"},
        controls={"FrameDurationLimits": (frame_duration_us, frame_duration_us)},
        buffer_count=2,
    )
    picam2.configure(config)
    picam2.start()
    return picam2


def _streamer_running() -> bool:
    try:
        import subprocess

        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", "streamer"],
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--cam0-hef",
        type=Path,
        default=DEFAULT_CAM0_HEF,
        help=f"HEF for camera 0 (default: {DEFAULT_CAM0_HEF})",
    )
    parser.add_argument(
        "--cam1-hef",
        type=Path,
        default=DEFAULT_CAM1_HEF,
        help=f"HEF for camera 1 (default: {DEFAULT_CAM1_HEF})",
    )
    parser.add_argument(
        "--cam0-labels",
        type=Path,
        default=DEFAULT_CAM0_LABELS,
        help=f"Labels JSON for camera 0 (default: {DEFAULT_CAM0_LABELS})",
    )
    parser.add_argument(
        "--cam1-labels",
        type=Path,
        default=DEFAULT_CAM1_LABELS,
        help=f"Labels JSON for camera 1 (default: {DEFAULT_CAM1_LABELS})",
    )
    parser.add_argument(
        "--zoo",
        action="store_true",
        help=(
            "Use two different preinstalled zoo models: "
            "YOLOv8s on cam0, YOLOv6n on cam1"
        ),
    )
    parser.add_argument(
        "--stock",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.4,
        help="Default confidence threshold for both cameras",
    )
    parser.add_argument(
        "--threshold0",
        type=float,
        default=None,
        help="Confidence threshold for camera 0 (overrides --threshold)",
    )
    parser.add_argument(
        "--threshold1",
        type=float,
        default=None,
        help="Confidence threshold for camera 1 (overrides --threshold)",
    )
    parser.add_argument(
        "--inference-size",
        type=int,
        nargs=2,
        default=(640, 640),
        metavar=("W", "H"),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without a GUI (no Tkinter); save side-by-side frames to disk.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("./dual_preview_out"),
        help="Where to save annotated frames in --headless mode.",
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=10,
        help="In --headless mode, save one frame every N iterations.",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="In headless mode, stop after N frames (0 = run until Ctrl+C).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.stock:
        args.zoo = True
    if args.zoo:
        _apply_zoo_defaults(args)

    threshold0 = (
        args.threshold if args.threshold0 is None else args.threshold0
    )
    threshold1 = (
        args.threshold if args.threshold1 is None else args.threshold1
    )

    if _streamer_running():
        print(
            "ERROR: streamer service is running and holds the cameras.\n"
            "       Stop it first: sudo systemctl stop streamer",
            file=sys.stderr,
        )
        return 1

    inference_size = (args.inference_size[0], args.inference_size[1])
    print("Loading Hailo models on a shared VDevice...")
    vdevice = _open_shared_vdevice()
    model0 = _load_model(
        vdevice, args.cam0_hef, args.cam0_labels or None, inference_size
    )
    model1 = _load_model(
        vdevice, args.cam1_hef, args.cam1_labels or None, inference_size
    )

    print(f"Camera 0 model: {model0.model_name} ({args.cam0_hef})")
    print(f"Camera 1 model: {model1.model_name} ({args.cam1_hef})")
    print("Opening cameras...")
    cam0 = _open_camera(0, args.width, args.height, args.fps)
    cam1 = _open_camera(1, args.width, args.height, args.fps)

    headless = args.headless or not bool(os.environ.get("DISPLAY"))

    def shutdown(_signum: int | None = None, _frame: Any | None = None) -> None:
        nonlocal running
        running = False

    running = True
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    target_interval = 1.0 / max(args.fps, 0.1)

    if headless:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        print(
            "Running headless (no $DISPLAY). Saving annotated side-by-side frames to:",
            args.out_dir,
        )

        i = 0
        try:
            while running:
                cycle_start = time.monotonic()
                frame0 = cam0.capture_array()
                frame1 = cam1.capture_array()

                dets0 = model0.detect(frame0, threshold0)
                dets1 = model1.detect(frame1, threshold1)

                left = annotate_frame(
                    frame0, dets0, f"Camera 0 ({model0.model_name})"
                )
                right = annotate_frame(
                    frame1, dets1, f"Camera 1 ({model1.model_name})"
                )
                combined = Image.new("RGB", (left.width + right.width, left.height))
                combined.paste(left, (0, 0))
                combined.paste(right, (left.width, 0))

                if args.save_every > 0 and (i % args.save_every == 0):
                    ts = time.strftime("%Y%m%d-%H%M%S")
                    out_path = args.out_dir / f"frame-{i:06d}-{ts}.jpg"
                    combined.save(out_path, format="JPEG", quality=85)

                # Keep stdout reasonably quiet; print only if we got detections.
                if dets0 or dets1:
                    top0 = max(dets0, key=lambda d: d.confidence) if dets0 else None
                    top1 = max(dets1, key=lambda d: d.confidence) if dets1 else None
                    msg = []
                    if top0 is not None:
                        msg.append(f"cam0={top0.display_name}:{top0.confidence:.2f}")
                    if top1 is not None:
                        msg.append(f"cam1={top1.display_name}:{top1.confidence:.2f}")
                    print("detections:", " ".join(msg))

                i += 1
                if args.max_frames and i >= args.max_frames:
                    break

                elapsed = time.monotonic() - cycle_start
                remaining = target_interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            cam0.stop()
            cam1.stop()
            cam0.close()
            cam1.close()
        return 0

    # GUI mode
    import tkinter as tk
    from PIL import ImageTk

    root = tk.Tk()
    root.title("Dual Hailo Preview")
    label = tk.Label(root)
    label.pack()
    photo: ImageTk.PhotoImage | None = None

    def tick() -> None:
        nonlocal photo
        if not running:
            try:
                root.destroy()
            except Exception:
                pass
            return

        cycle_start = time.monotonic()
        try:
            frame0 = cam0.capture_array()
            frame1 = cam1.capture_array()
            dets0 = model0.detect(frame0, threshold0)
            dets1 = model1.detect(frame1, threshold1)
            left = annotate_frame(
                frame0, dets0, f"Camera 0 ({model0.model_name})"
            )
            right = annotate_frame(
                frame1, dets1, f"Camera 1 ({model1.model_name})"
            )
            combined = Image.new("RGB", (left.width + right.width, left.height))
            combined.paste(left, (0, 0))
            combined.paste(right, (left.width, 0))
            photo = ImageTk.PhotoImage(combined)
            label.configure(image=photo)
        except Exception:
            shutdown()
            raise

        elapsed = time.monotonic() - cycle_start
        delay_ms = max(1, int((target_interval - elapsed) * 1000))
        root.after(delay_ms, tick)

    root.protocol("WM_DELETE_WINDOW", shutdown)
    root.after(0, tick)
    try:
        root.mainloop()
    finally:
        cam0.stop()
        cam1.stop()
        cam0.close()
        cam1.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
