"""Detection filtering and deduplication."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from streamer.wildlife.types import Detection


@dataclass
class DetectionFilter:
    """Suppress low-confidence and rapid repeat detections."""

    confidence_threshold: float
    cooldown_seconds: float
    allowed_classes: frozenset[str] = frozenset()
    _last_saved: dict[tuple[int, str], float] = field(
        default_factory=dict, repr=False
    )

    def accept(self, camera_num: int, detection: Detection) -> bool:
        if detection.confidence < self.confidence_threshold:
            return False
        if self.allowed_classes and detection.species not in self.allowed_classes:
            return False
        key = (camera_num, detection.species)
        now = time.time()
        last = self._last_saved.get(key)
        if last is not None and (now - last) < self.cooldown_seconds:
            return False
        self._last_saved[key] = now
        return True
