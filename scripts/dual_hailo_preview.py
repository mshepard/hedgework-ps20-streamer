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

DEFAULT_CAM0_HEF = Path("/var/lib/streamer/models/bird_v1.hef")
DEFAULT_CAM1_HEF = Path("/var/lib/streamer/models/pollinator_v1.hef")
DEFAULT_CAM0_LABELS = Path("/var/lib/streamer/models/bird_v1.json")
DEFAULT_CAM1_LABELS = Path("/var/lib/streamer/models/pollinator_v1.json")
STOCK_YOLOV8_HEF = Path("/usr/share/hailo-models/yolov8s_h8l.hef")


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
    infer_pipeline: Any
    labels: list[str]
    inference_size: tuple[int, int]

    def detect(self, rgb: np.ndarray, threshold: float) -> list[Detection]:
        input_data = _preprocess(rgb, self.inference_size)
        with self.infer_pipeline as _pipeline:
            bindings = self.network_group.create_bindings(
                self.network_group_params
            )
            bindings.input().set_buffer(input_data)
            self.network_group.run(bindings)
            outputs = {
                name: np.array(bindings.output(name).get_buffer())
                for name in bindings._output_names
            }
        detections = _parse_yolo_outputs(
            outputs, self.labels, rgb.shape[1], rgb.shape[0]
        )
        return [d for d in detections if d.confidence >= threshold]


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


def _parse_yolo_outputs(
    outputs: dict[str, np.ndarray],
    labels: list[str],
    width: int,
    height: int,
) -> list[Detection]:
    if not outputs:
        return []
    tensor = next(iter(outputs.values()))
    # Some HEFs expose outputs as Python lists (or lists-of-arrays)
    # instead of a single ndarray. Normalize to an ndarray early.
    tensor = np.asarray(tensor)

    # Typical YOLOv8 inference layouts (examples):
    #   (1, N, 4+nc)
    #   (1, 4+nc, N)
    if tensor.ndim == 3 and tensor.shape[1] < tensor.shape[2]:
        tensor = np.transpose(tensor, (0, 2, 1))

    if tensor.ndim == 3:
        # (1, N, 4+nc) -> (N, 4+nc)
        preds = tensor[0]
    elif tensor.ndim == 2:
        # (N, 4+nc)
        preds = tensor
    else:
        # Best-effort fallback: collapse everything except the last dim
        # and treat it as (N, 4+nc).
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
        species = (
            labels[class_id]
            if class_id < len(labels)
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
        HEF,
        HailoStreamInterface,
        InferVStreams,
    )

    if not hef_path.is_file():
        raise FileNotFoundError(f"HEF model not found: {hef_path}")

    labels = _load_labels(labels_path)
    if not labels:
        fallback = hef_path.with_suffix(".json")
        if fallback.is_file():
            labels = _load_labels(fallback)

    hef = HEF(str(hef_path))
    params = ConfigureParams.create_from_hef(
        hef, interface=HailoStreamInterface.PCIe
    )
    network_group = vdevice.configure(hef, params)[0]
    network_group_params = network_group.create_params()
    infer_pipeline = InferVStreams(network_group, network_group_params)
    return ModelRunner(
        network_group=network_group,
        network_group_params=network_group_params,
        infer_pipeline=infer_pipeline,
        labels=labels,
        inference_size=inference_size,
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
        "--stock",
        action="store_true",
        help="Use the stock YOLOv8 demo HEF on both cameras",
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--threshold", type=float, default=0.55)
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
        args.cam0_hef = STOCK_YOLOV8_HEF
        args.cam1_hef = STOCK_YOLOV8_HEF
        args.cam0_labels = Path()
        args.cam1_labels = Path()

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

                dets0 = model0.detect(frame0, args.threshold)
                dets1 = model1.detect(frame1, args.threshold)

                left = annotate_frame(frame0, dets0, "Camera 0")
                right = annotate_frame(frame1, dets1, "Camera 1")
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
            dets0 = model0.detect(frame0, args.threshold)
            dets1 = model1.detect(frame1, args.threshold)
            left = annotate_frame(frame0, dets0, "Camera 0")
            right = annotate_frame(frame1, dets1, "Camera 1")
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
