#!/usr/bin/env python3
"""Filter a YOLO-format dataset (e.g. Roboflow export) to a class allowlist.

Roboflow exports typically look like:

  pollinators/
    train/images, train/labels
    valid/images, valid/labels   # note: valid, not val
    test/images, test/labels
    data.yaml

Example:

  python training/scripts/filter_yolo_dataset.py \\
      --source ~/data/pollinators \\
      --allowlist training/ps20_pollinators.txt \\
      --output training/datasets/pollinators
"""

from __future__ import annotations

import argparse
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


def load_class_names(source: Path) -> list[str]:
    classes_txt = source / "classes.txt"
    if classes_txt.is_file():
        return [
            line.strip()
            for line in classes_txt.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    data_yaml = source / "data.yaml"
    if not data_yaml.is_file():
        raise FileNotFoundError(f"No classes.txt or data.yaml in {source}")

    names: list[str] = []
    in_names = False
    for line in data_yaml.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("names:"):
            in_names = True
            continue
        if in_names:
            if stripped.startswith("- "):
                names.append(stripped[2:].strip().strip("'\""))
            elif stripped and not stripped.startswith("#"):
                break
    if not names:
        raise ValueError(f"Could not parse class names from {data_yaml}")
    return names


def filter_split(
    source_split: Path,
    dest_split: Path,
    old_to_new: dict[int, int],
) -> int:
    src_images = source_split / "images"
    src_labels = source_split / "labels"
    if not src_labels.is_dir():
        return 0
    dst_images = dest_split / "images"
    dst_labels = dest_split / "labels"
    dst_images.mkdir(parents=True, exist_ok=True)
    dst_labels.mkdir(parents=True, exist_ok=True)

    kept = 0
    for label_path in sorted(src_labels.glob("*.txt")):
        new_lines: list[str] = []
        for line in label_path.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) != 5:
                continue
            old_id = int(parts[0])
            if old_id not in old_to_new:
                continue
            parts[0] = str(old_to_new[old_id])
            new_lines.append(" ".join(parts))
        if not new_lines:
            continue
        stem = label_path.stem
        image_candidates = list(src_images.glob(stem + ".*"))
        if not image_candidates:
            continue
        shutil.copy2(image_candidates[0], dst_images / image_candidates[0].name)
        (dst_labels / label_path.name).write_text(
            "\n".join(new_lines) + "\n", encoding="utf-8"
        )
        kept += 1
    return kept


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


def resolve_splits(source: Path) -> list[tuple[str, str]]:
    """Return (source_dir_name, dest_dir_name) pairs."""

    pairs: list[tuple[str, str]] = []
    if (source / "train").is_dir():
        pairs.append(("train", "train"))
    if (source / "valid").is_dir():
        pairs.append(("valid", "val"))
    elif (source / "val").is_dir():
        pairs.append(("val", "val"))
    if (source / "test").is_dir():
        pairs.append(("test", "val"))  # fold test into val if no valid
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    allowlist = load_allowlist(args.allowlist)
    class_names = load_class_names(args.source)
    class_index = {name: idx for idx, name in enumerate(class_names)}
    missing = [name for name in allowlist if name not in class_index]
    if missing:
        print("Warning: allowlist classes not in dataset:", ", ".join(missing))
        print("Available classes:", ", ".join(class_names))

    selected = [name for name in allowlist if name in class_index]
    if not selected:
        raise SystemExit("No allowlist classes matched the dataset")
    old_to_new = {class_index[name]: idx for idx, name in enumerate(selected)}

    if args.output.exists():
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True)

    totals: dict[str, int] = {}
    for src_name, dst_name in resolve_splits(args.source):
        count = filter_split(
            args.source / src_name,
            args.output / dst_name if dst_name != "val" else args.output / "val",
            old_to_new,
        )
        totals[src_name] = count

    write_data_yaml(args.output, selected)
    print(f"Wrote {len(selected)} classes to {args.output / 'data.yaml'}")
    for split, count in totals.items():
        print(f"  {split}: {count} images")


if __name__ == "__main__":
    main()
