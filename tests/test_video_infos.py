from __future__ import annotations

import json
import unittest
from pathlib import Path

from audit.coze_adapter_video_infos import run_both
from audit.equivalence_video_infos import load_cases, run_audit, summarize
from workflow_1256.video_infos import run_video_infos, video_infos


class VideoInfosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.input = json.loads((root / "video_infos_input.json").read_text(encoding="utf-8"))
        cls.expected = json.loads((root / "video_infos_expected.json").read_text(encoding="utf-8"))

    def test_synthetic_output_contract_and_exact_json(self) -> None:
        original, migrated = run_both(self.input)
        self.assertEqual(original, migrated)
        self.assertEqual(migrated, self.expected)
        infos = json.loads(migrated["infos"])
        self.assertEqual(len(infos), 3)
        self.assertEqual(list(infos[0]), ["video_url", "start", "end", "duration", "transition", "transition_duration", "volume"])
        self.assertEqual([[item["start"], item["end"]] for item in infos], [[0, 4000000], [4000000, 8500000], [8500000, 12000000]])
        self.assertEqual([item["duration"] for item in infos], [4000000, 4500000, 3500000])

    def test_all_boundary_cases_match_original(self) -> None:
        results = run_audit()
        summary = summarize(results)
        self.assertEqual(summary["total"], 10)
        self.assertEqual(summary["failed"], 0, results)
        self.assertTrue(summary["code_level_equivalent"])

    def test_empty_array_and_mismatched_arrays(self) -> None:
        self.assertEqual(
            json.loads(video_infos([], [], volume=0)),
            [],
        )
        output = json.loads(video_infos(["u1"], [{"start": 0, "end": 2}, {"start": 2, "end": 4}], volume=0))
        self.assertEqual(len(output), 1)

    def test_json_string_and_nested_input(self) -> None:
        case_map = {case.name: case for case in load_cases()}
        for name in ("json_string_input", "nested_input_wrapper"):
            with self.subTest(name=name):
                original, migrated = run_both(case_map[name].params)
                self.assertEqual(original, migrated)
                self.assertEqual(len(json.loads(migrated["infos"])), 1)

    def test_missing_fields_and_invalid_timeline_keep_original_exceptions(self) -> None:
        case_map = {case.name: case for case in load_cases()}
        for name in ("missing_required_field", "missing_timeline_field", "invalid_timeline_arithmetic"):
            with self.subTest(name=name):
                original, migrated, _ = __import__("audit.equivalence_video_infos", fromlist=["compare_case"]).compare_case(case_map[name].params)
                self.assertEqual(original["kind"], "exception")
                self.assertEqual(original, migrated)

    def test_optional_field_insertion_order(self) -> None:
        result = run_video_infos({
            "video_urls": ["u"],
            "timelines": [{"start": 1, "end": 3}],
            "width": 1080,
            "height": 1920,
            "mask": "圆形",
            "transition": "漫画撕纸",
            "transition_duration": 1000000,
            "volume": 0,
        })
        self.assertEqual(
            list(json.loads(result["infos"])[0]),
            ["video_url", "start", "end", "duration", "width", "height", "mask", "transition", "transition_duration", "volume"],
        )


if __name__ == "__main__":
    unittest.main()
