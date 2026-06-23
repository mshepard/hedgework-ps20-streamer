#!/usr/bin/env python3
"""Build a PS 20 bird YOLO dataset from NABirds.

NABirds is a classification dataset with one bounding box per image.
Download from https://dl.allaboutbirds.org/nabirds (research use) and
extract to e.g. ~/data/nabirds/:

  nabirds/
    images/
    classes.txt
    images.txt
    bounding_boxes.txt
    image_class_labels.txt
    train_test_split.txt

The allowlist (``training/ps20_birds.txt``) lists species names as
substrings of NABirds class descriptions. All matching plumages merge
into one YOLO class per allowlist line.

Example:

  python training/scripts/prepare_nabirds.py \\
      --source ~/data/nabirds \\
      --allowlist training/ps20_birds.txt \\
      --output training/datasets/birds
"""

from __future__ import annotations

import argparse
import random
import shutil
from pathlib import Path


def load_allowlist(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line)
    return names


def normalize(text: str) -> str:
    return " ".join(text.lower().replace("_", " ").replace(".", " ").split())


def load_nabirds_classes(source: Path) -> dict[int, str]:
    classes: dict[int, str] = {}
    for line in source.joinpath("classes.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        class_id_str, _, description = line.partition(" ")
        classes[int(class_id_str)] = description.strip()
    return classes


def map_allowlist_to_class_ids(
    allowlist: list[str], nabirds_classes: dict[int, str]
) -> dict[int, int]:
    """Map each NABirds class_id to a contiguous YOLO class index."""

    nabirds_to_yolo: dict[int, int] = {}
    for yolo_idx, needle in enumerate(allowlist):
        n_needle = normalize(needle)
        matched = False
        for class_id, description in nabirds_classes.items():
            if n_needle in normalize(description):
                nabirds_to_yolo[class_id] = yolo_idx
                matched = True
        if not matched:
            print(f"Warning: no NABirds classes matched allowlist entry '{needle}'")
    return nabirds_to_yolo


def load_metadata(
    source: Path,
) -> tuple[
    dict[int, str],
    dict[int, int],
    dict[int, tuple[int, int, int, int]],
    dict[int, bool],
]:
    image_paths: dict[int, str] = {}
    for line in source.joinpath("images.txt").read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            image_paths[int(parts[0])] = parts[1]

    image_labels: dict[int, int] = {}
    for line in source.joinpath("image_class_labels.txt").read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            image_labels[int(parts[0])] = int(parts[1])

    boxes: dict[int, tuple[int, int, int, int]] = {}
    for line in source.joinpath("bounding_boxes.txt").read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) == 5:
            image_id = int(parts[0])
            boxes[image_id] = (int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))

    is_train: dict[int, bool] = {}
    for line in source.joinpath("train_test_split.txt").read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            is_train[int(parts[0])] = parts[1] == "1"

    return image_paths, image_labels, boxes, is_train


def bbox_to_yolo(
    x: int, y: int, w: int, h: int, img_w: int, img_h: int
) -> tuple[float, float, float, float]:
    x_center = (x + w / 2) / img_w
    y_center = (y + h / 2) / img_h
    return x_center, y_center, w / img_w, h / img_h


def write_data_yaml(output: Path, names: list[str]) -> None:
    yaml_path = output / "data.yaml"
    lines = [
        f"path: {output.resolve()}",
        "train: images/train",
        "val: images/val",
        f"nc: {len(names)}",
        "names:",
    ]
    lines.extend(f"  - {name}" for name in names)
    yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--val-fraction",
        type=float,
        default=0.1,
        help="Fraction of training images held out as val when NABirds marks is_train=1",
    )
    args = parser.parse_args()

    allowlist = load_allowlist(args.allowlist)
    nabirds_classes = load_nabirds_classes(args.source)
    nabirds_to_yolo = map_allowlist_to_class_ids(allowlist, nabirds_classes)
    if not nabirds_to_yolo:
        raise SystemExit("No NABirds classes matched the allowlist; check ps20_birds.txt")

    image_paths, image_labels, boxes, is_train = load_metadata(args.source)

    if args.output.exists():
        shutil.rmtree(args.output)

    train_dir = args.output / "images" / "train"
    val_dir = args.output / "images" / "val"
    train_labels = args.output / "labels" / "train"
    val_labels = args.output / "labels" / "val"
    for d in (train_dir, val_dir, train_labels, val_labels):
        d.mkdir(parents=True)

    # Pillow only if we need image dimensions
    from PIL import Image

    train_ids: list[int] = []
    val_ids: list[int] = []
    for image_id, class_id in image_labels.items():
        if class_id not in nabirds_to_yolo:
            continue
        if is_train.get(image_id, True):
            train_ids.append(image_id)
        else:
            val_ids.append(image_id)

    # Optional extra val holdout from train pool
    random.seed(42)
    if args.val_fraction > 0 and train_ids:
        n_val = max(1, int(len(train_ids) * args.val_fraction))
        extra_val = set(random.sample(train_ids, min(n_val, len(train_ids))))
        for image_id in extra_val:
            train_ids.remove(image_id)
            val_ids.append(image_id)

    counts = {"train": 0, "val": 0}

    def export_one(image_id: int, split: str) -> None:
        rel_path = image_paths.get(image_id)
        if not rel_path:
            return
        src = args.source / "images" / rel_path
        if not src.is_file():
            return
        class_id = image_labels[image_id]
        yolo_class = nabirds_to_yolo[class_id]
        with Image.open(src) as img:
            img_w, img_h = img.size
        if image_id in boxes:
            x, y, w, h = boxes[image_id]
            xc, yc, bw, bh = bbox_to_yolo(x, y, w, h, img_w, img_h)
        else:
            xc, yc, bw, bh = 0.5, 0.5, 1.0, 1.0

        stem = f"{image_id:06d}"
        if split == "train":
            img_dst = train_dir / f"{stem}.jpg"
            lbl_dst = train_labels / f"{stem}.txt"
        else:
            img_dst = val_dir / f"{stem}.jpg"
            lbl_dst = val_labels / f"{stem}.txt"
        shutil.copy2(src, img_dst)
        lbl_dst.write_text(
            f"{yolo_class} {xc:.6f} {yc:.6f} {bw:.6f} {bh:.6f}\n",
            encoding="utf-8",
        )
        counts[split] += 1

    for image_id in train_ids:
        export_one(image_id, "train")
    for image_id in val_ids:
        export_one(image_id, "val")

    write_data_yaml(args.output, allowlist)
    print(f"Wrote {len(allowlist)} bird classes to {args.output / 'data.yaml'}")
    print(f"  train: {counts['train']} images")
    print(f"  val: {counts['val']} images")
    print(f"  NABirds source class_ids used: {len(nabirds_to_yolo)}")


if __name__ == "__main__":
    main()
