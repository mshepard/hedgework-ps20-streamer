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


def validate_allowlist(allowlist: list[str]) -> None:
    """Reject entries that would silently create duplicate YOLO classes."""

    seen: dict[str, str] = {}
    duplicates: list[tuple[str, str]] = []
    for name in allowlist:
        normalized = normalize(name)
        if normalized in seen:
            duplicates.append((seen[normalized], name))
        else:
            seen[normalized] = name

    if duplicates:
        details = "; ".join(
            f"{first!r} and {duplicate!r}"
            for first, duplicate in duplicates
        )
        raise ValueError(f"Duplicate normalized allowlist entries: {details}")


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
    owners: dict[int, str] = {}
    for yolo_idx, needle in enumerate(allowlist):
        n_needle = normalize(needle)
        matched = False
        for class_id, description in nabirds_classes.items():
            if n_needle in normalize(description):
                previous = owners.get(class_id)
                if previous is not None:
                    raise ValueError(
                        "Overlapping allowlist entries "
                        f"{previous!r} and {needle!r} both match NABirds "
                        f"class {class_id}: {description!r}"
                    )
                nabirds_to_yolo[class_id] = yolo_idx
                owners[class_id] = needle
                matched = True
        if not matched:
            print(f"Warning: no NABirds classes matched allowlist entry '{needle}'")
    return nabirds_to_yolo


def load_metadata(
    source: Path,
) -> tuple[
    dict[str, str],
    dict[str, int],
    dict[str, tuple[int, int, int, int]],
    dict[str, bool],
    dict[str, tuple[int, int]],
]:
    image_paths: dict[str, str] = {}
    for line in source.joinpath("images.txt").read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            image_paths[parts[0]] = parts[1]

    image_labels: dict[str, int] = {}
    for line in source.joinpath("image_class_labels.txt").read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            image_labels[parts[0]] = int(parts[1])

    boxes: dict[str, tuple[int, int, int, int]] = {}
    for line in source.joinpath("bounding_boxes.txt").read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) == 5:
            image_id = parts[0]
            boxes[image_id] = (int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))

    is_train: dict[str, bool] = {}
    for line in source.joinpath("train_test_split.txt").read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            is_train[parts[0]] = parts[1] == "1"

    sizes: dict[str, tuple[int, int]] = {}
    sizes_path = source / "sizes.txt"
    if sizes_path.is_file():
        for line in sizes_path.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) == 3:
                sizes[parts[0]] = (int(parts[1]), int(parts[2]))

    return image_paths, image_labels, boxes, is_train, sizes


def bbox_to_yolo(
    x: int, y: int, w: int, h: int, img_w: int, img_h: int
) -> tuple[float, float, float, float]:
    # A small number of NABirds source boxes extend a few pixels beyond
    # the declared image dimensions. Clip them before normalization so
    # generated YOLO boxes always remain inside [0, 1].
    x0 = max(0, min(x, img_w))
    y0 = max(0, min(y, img_h))
    x1 = max(x0, min(x + w, img_w))
    y1 = max(y0, min(y + h, img_h))
    clipped_w = x1 - x0
    clipped_h = y1 - y0
    if clipped_w == 0 or clipped_h == 0:
        raise ValueError(
            f"Bounding box {(x, y, w, h)} is outside image "
            f"{(img_w, img_h)}"
        )
    x_center = (x0 + clipped_w / 2) / img_w
    y_center = (y0 + clipped_h / 2) / img_h
    return x_center, y_center, clipped_w / img_w, clipped_h / img_h


def write_data_yaml(output: Path, names: list[str]) -> None:
    yaml_path = output / "data.yaml"
    lines = [
        # Omitting ``path`` makes Ultralytics use data.yaml's directory
        # as the dataset root, so the generated dataset remains portable
        # when copied from this Mac to an Ubuntu training machine.
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
    validate_allowlist(allowlist)
    nabirds_classes = load_nabirds_classes(args.source)
    nabirds_to_yolo = map_allowlist_to_class_ids(allowlist, nabirds_classes)
    if not nabirds_to_yolo:
        raise SystemExit("No NABirds classes matched the allowlist; check ps20_birds.txt")

    image_paths, image_labels, boxes, is_train, sizes = load_metadata(args.source)

    if args.output.exists():
        shutil.rmtree(args.output)

    train_dir = args.output / "images" / "train"
    val_dir = args.output / "images" / "val"
    train_labels = args.output / "labels" / "train"
    val_labels = args.output / "labels" / "val"
    for d in (train_dir, val_dir, train_labels, val_labels):
        d.mkdir(parents=True)

    train_ids: list[str] = []
    val_ids: list[str] = []
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

    def export_one(image_id: str, split: str) -> None:
        rel_path = image_paths.get(image_id)
        if not rel_path:
            return
        src = args.source / "images" / rel_path
        if not src.is_file():
            return
        class_id = image_labels[image_id]
        yolo_class = nabirds_to_yolo[class_id]
        if image_id in sizes:
            img_w, img_h = sizes[image_id]
        else:
            from PIL import Image

            with Image.open(src) as img:
                img_w, img_h = img.size
        if image_id in boxes:
            x, y, w, h = boxes[image_id]
            xc, yc, bw, bh = bbox_to_yolo(x, y, w, h, img_w, img_h)
        else:
            xc, yc, bw, bh = 0.5, 0.5, 1.0, 1.0

        stem = image_id.replace("-", "")
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
