from __future__ import annotations

import unittest

from workflow_1256.canvas import CanvasValidationError, resolve_canvas


class CanvasTests(unittest.TestCase):
    def test_presets_and_default(self) -> None:
        self.assertEqual(resolve_canvas().to_dict()["aspect_ratio"], "9:16")
        self.assertEqual(resolve_canvas("16:9").to_dict()["width"], 1920)
        self.assertEqual(resolve_canvas("1:1").to_dict()["height"], 1080)
        self.assertEqual(resolve_canvas("3:2").to_dict()["width"], 1620)
        self.assertEqual(resolve_canvas("2:3").to_dict()["height"], 1620)

    def test_rejects_unknown_or_mismatched_dimensions(self) -> None:
        with self.assertRaises(CanvasValidationError):
            resolve_canvas("4:3")
        with self.assertRaises(CanvasValidationError):
            resolve_canvas({"aspect_ratio": "16:9", "width": 1080, "height": 1920})


if __name__ == "__main__":
    unittest.main()
