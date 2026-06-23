#!/usr/bin/env python3
"""List NABirds class descriptions to help build ps20_birds.txt."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="NABirds root (contains classes.txt)")
    parser.add_argument("--grep", type=str, default="", help="Filter descriptions")
    args = parser.parse_args()

    classes_path = args.source / "classes.txt"
    if not classes_path.is_file():
        raise SystemExit(f"Missing {classes_path}")

    pattern = re.compile(args.grep, re.IGNORECASE) if args.grep else None
    for line in classes_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        class_id, _, description = line.partition(" ")
        if pattern and not pattern.search(description):
            continue
        print(f"{class_id:>4}  {description}")


if __name__ == "__main__":
    main()
