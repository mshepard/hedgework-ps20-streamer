"""Tests for wildlife frame tiling and NMS merge."""

from __future__ import annotations

import unittest

import numpy as np

from streamer.wildlife.tiling import (
    Tile,
    iter_tiles,
    nms_merge,
    remap_detection,
)
from streamer.wildlife.types import Detection


class TileSplitTests(unittest.TestCase):
    def test_grid_covers_full_frame(self) -> None:
        rgb = np.zeros((1296, 2304, 3), dtype=np.uint8)
        tiles = iter_tiles(rgb, grid=(3, 2), tile_size=(640, 640), overlap=0.15)
        self.assertEqual(len(tiles), 6)
        # Mark each tile uniquely and reconstruct a coverage mask.
        mask = np.zeros((1296, 2304), dtype=np.uint8)
        for i, tile in enumerate(tiles, start=1):
            mask[
                tile.y0 : tile.y0 + tile.height,
                tile.x0 : tile.x0 + tile.width,
            ] = 1
            self.assertEqual(tile.rgb.shape[0], tile.height)
            self.assertEqual(tile.rgb.shape[1], tile.width)
            self.assertGreater(i, 0)
        self.assertTrue(np.all(mask == 1))

    def test_neighbors_overlap(self) -> None:
        rgb = np.zeros((1000, 1000, 3), dtype=np.uint8)
        tiles = iter_tiles(rgb, grid=(2, 2), tile_size=(640, 640), overlap=0.2)
        self.assertEqual(len(tiles), 4)
        left, right = tiles[0], tiles[1]
        overlap_w = (left.x0 + left.width) - right.x0
        self.assertGreater(overlap_w, 0)


class RemapAndNmsTests(unittest.TestCase):
    def test_remap_to_full_frame(self) -> None:
        tile = Tile(
            rgb=np.zeros((100, 100, 3), dtype=np.uint8),
            x0=100,
            y0=50,
            width=100,
            height=100,
        )
        det = Detection(
            species="Bee",
            confidence=0.9,
            x_center=0.5,
            y_center=0.5,
            width=0.2,
            height=0.4,
        )
        mapped = remap_detection(det, tile, full_width=400, full_height=200)
        self.assertAlmostEqual(mapped.x_center, 150 / 400)
        self.assertAlmostEqual(mapped.y_center, 100 / 200)
        self.assertAlmostEqual(mapped.width, 20 / 400)
        self.assertAlmostEqual(mapped.height, 40 / 200)

    def test_nms_keeps_higher_confidence_duplicate(self) -> None:
        a = Detection("Bee", 0.9, 0.5, 0.5, 0.2, 0.2)
        b = Detection("Bee", 0.6, 0.52, 0.5, 0.2, 0.2)
        c = Detection("Fly", 0.8, 0.5, 0.5, 0.2, 0.2)
        merged = nms_merge([a, b, c], iou_threshold=0.5)
        species = {d.species for d in merged}
        self.assertEqual(species, {"Bee", "Fly"})
        bee = next(d for d in merged if d.species == "Bee")
        self.assertAlmostEqual(bee.confidence, 0.9)


if __name__ == "__main__":
    unittest.main()
