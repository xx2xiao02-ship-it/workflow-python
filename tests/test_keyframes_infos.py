from __future__ import annotations

import json
import unittest
from pathlib import Path

from audit.coze_adapter_keyframes_infos import run_both
from audit.equivalence_keyframes_infos import compare_case, load_cases, run_audit, summarize
from audit.real_fixture_keyframes_infos import run_real_structural_audit
from src.workflow_1256.capcut_node_adapters import CapCutNodeAdapter
from workflow_1256.keyframes_infos import keyframes_infos, run_keyframes_infos


class KeyframesInfosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.input = json.loads((root / "keyframes_infos_input.json").read_text(encoding="utf-8"))
        cls.expected = json.loads((root / "keyframes_infos_expected.json").read_text(encoding="utf-8"))

    def test_synthetic_output_exact_and_ordered(self) -> None:
        original, migrated = run_both(self.input)
        self.assertEqual(original, migrated)
        self.assertEqual(migrated, self.expected)
        items = json.loads(migrated["keyframes_infos"])
        self.assertEqual(len(items), 6)
        self.assertEqual(list(items[0]), ["offset", "property", "segment_id", "value"])
        self.assertEqual([item["segment_id"] for item in items], ["seg-a", "seg-a", "seg-b", "seg-b", "seg-c", "seg-c"])
        self.assertEqual([item["offset"] for item in items], [0, 6906732, 0, 6706536, 0, 2802732])
        self.assertEqual([item["value"] for item in items], [1.0, 1.15, 1.0, 1.15, 1.0, 1.15])

    def test_all_synthetic_boundary_cases_match_original(self) -> None:
        results = run_audit()
        summary = summarize(results)
        self.assertEqual(summary["total"], 13)
        self.assertEqual(summary["failed"], 0, results)
        self.assertTrue(summary["code_level_equivalent"])

    def test_empty_array_and_json_string(self) -> None:
        self.assertEqual(json.loads(keyframes_infos("UNIFORM_SCALE", "0|100", "1|1.15", [])), [])
        case_map = {case.name: case for case in load_cases()}
        original, migrated, differences = compare_case(case_map["json_string_input"].params)
        self.assertFalse(differences)
        self.assertEqual(original, migrated)

    def test_missing_field_and_invalid_timeline_preserve_exception(self) -> None:
        case_map = {case.name: case for case in load_cases()}
        for name in ("missing_required_field", "missing_segment_field", "invalid_timeline_arithmetic"):
            with self.subTest(name=name):
                original, migrated, differences = compare_case(case_map[name].params)
                self.assertFalse(differences)
                self.assertEqual(original["kind"], "exception")
                self.assertEqual(original, migrated)

    def test_position_normalization_and_relative_offset(self) -> None:
        output = json.loads(run_keyframes_infos({
            "ctype": "KFTypePositionX",
            "offsets": "0|50|100",
            "values": "0|960|1920",
            "width": 1920,
            "segment_infos": [{"id": "x", "start": 1000000, "end": 5000000}],
        })["keyframes_infos"])
        self.assertEqual([item["offset"] for item in output], [0, 2000000, 4000000])
        self.assertEqual([item["value"] for item in output], [0.0, 0.5, 1.0])

    def test_real_coze_structural_fixture(self) -> None:
        observed = run_real_structural_audit()
        self.assertEqual(observed["input_segment_count"], 18)
        self.assertEqual(observed["output_keyframe_count"], 36)
        self.assertEqual(observed["output_field_order"], ["offset", "property", "segment_id", "value"])
        self.assertEqual(observed["last_offset"], 2256000)
        self.assertTrue(observed["real_dynamic_ids_redacted"])

    def test_capcut_node_adapter_preserves_info_string(self) -> None:
        class FakeClient:
            def call(self, endpoint, params):
                self.observed = (endpoint, params)
                return {"infos": "[]"}

        client = FakeClient()
        result = CapCutNodeAdapter(client).keyframes_infos({"ctype": "UNIFORM_SCALE"})
        self.assertEqual(result, {"infos": "[]"})
        self.assertEqual(client.observed, ("keyframes_infos", {"ctype": "UNIFORM_SCALE"}))


if __name__ == "__main__":
    unittest.main()
