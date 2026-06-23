"""Shared types for the wildlife detection pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Detection:
    species: str
    confidence: float
    # Normalized YOLO box: center x/y and width/height in 0..1.
    x_center: float
    y_center: float
    width: float
    height: float

    @property
    def display_name(self) -> str:
        return species_to_display(self.species)


def species_to_display(species: str) -> str:
    """Turn ``Red_winged_Blackbird`` into ``Red-winged Blackbird``."""

    return species.replace("_", " ").strip()
