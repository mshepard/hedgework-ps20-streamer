#!/usr/bin/env python3
"""Export class names from data.yaml to Pi labels JSON for Hailo deploy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_names_from_yaml(path: Path) -> list[str]:
    names: list[str] = []
    in_names = False
    for line in path.read_text(encoding="utf-8").splitlines():
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
        raise ValueError(f"No names found in {path}")
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_yaml", type=Path)
    parser.add_argument("output_json", type=Path)
    args = parser.parse_args()

    names = load_names_from_yaml(args.data_yaml)
    payload = {"names": names}
    args.output_json.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(names)} labels to {args.output_json}")


if __name__ == "__main__":
    main()
