from __future__ import annotations

import json
from pathlib import Path
import unittest

from audit.coze_adapter_keyframes_infos_second import run_both
from audit.equivalence_keyframes_infos_second import compare_case, run_real_structural_audit
from workflow_1256.keyframes_infos_second import run_keyframes_infos


class KeyframesInfosSecondTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.input = json.loads((root / "keyframes_infos_second_input.json").read_text(encoding="utf-8"))
        cls.boundary = json.loads((root / "keyframes_infos_second_boundary_cases.json").read_text(encoding="utf-8"))

    def test_synthetic_old_new_exact_and_ordered(self) -> None:
        original, migrated = run_both(self.input)
        self.assertEqual(original, migrated)
        items = json.loads(migrated["keyframes_infos"])
        self.assertEqual(len(items), 4)
        self.assertEqual([item["segment_id"] for item in items], ["second-seg-a", "second-seg-a", "second-seg-b", "second-seg-b"])
        self.assertEqual([item["offset"] for item in items], [0, 6906732, 0, 6706536])

    def test_boundary_cases_match_original(self) -> None:
        for case in self.boundary:
            with self.subTest(case=case["name"]):
                _, _, differences = compare_case(case["params"])
                self.assertEqual(differences, [])

    def test_real_17_segments_match_original(self) -> None:
        observed = run_real_structural_audit()
        self.assertTrue(observed["passed"], observed["differences"])
        self.assertEqual(observed["input_segment_count"], 17)
        self.assertEqual(observed["original_keyframe_count"], 34)
        self.assertEqual(observed["migrated_keyframe_count"], 34)
        self.assertEqual(observed["migrated_shape"]["offsets"][-1], 2256000)

    def test_json_string_input_direct(self) -> None:
        result = run_keyframes_infos(json.dumps(self.input))
        self.assertEqual(len(json.loads(result["keyframes_infos"])), 4)


if __name__ == "__main__":
    unittest.main()
