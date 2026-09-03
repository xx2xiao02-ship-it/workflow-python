from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from audit.coze_adapter_127095 import run_both
from audit.equivalence_127095 import audit_case, summarize
from workflow_1256.timeline_planning import main, run_timeline_planning


class TimelinePlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sample_dir = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.sample_input = json.loads(
            (sample_dir / "timeline_planning_input.json").read_text(encoding="utf-8")
        )
        cls.sample_expected = json.loads(
            (sample_dir / "timeline_planning_expected.json").read_text(encoding="utf-8")
        )

    def assert_old_and_new_equal(self, params) -> dict:
        original, migrated = run_both(params)
        self.assertEqual(original, migrated)
        return migrated

    def test_synthetic_sample_contract_and_expected_result(self) -> None:
        result = self.assert_old_and_new_equal(self.sample_input)

        self.assertEqual(list(result), ["shots", "clip_duration", "int_duration", "timelines"])
        self.assertEqual(result, self.sample_expected)
        self.assertEqual(len(result["shots"]), len(result["timelines"]))
        self.assertEqual(result["timelines"][-1]["end"], 12_300_000)

    def test_empty_and_missing_inputs_return_original_empty_result(self) -> None:
        expected = {"shots": [], "clip_duration": [], "int_duration": [], "timelines": []}

        for params in ({}, {"shots": [], "duration": 8, "timelines": {"start": 0, "end": 8_000_000}}):
            self.assertEqual(self.assert_old_and_new_equal(params), expected)

    def test_json_strings_and_nested_input_are_supported(self) -> None:
        params = {
            "_input": {
                "segments": "JSON 输入",
                "shots": json.dumps(self.sample_input["shots"], ensure_ascii=False),
                "duration": "12.3",
                "timelines": json.dumps([self.sample_input["timelines"]], ensure_ascii=False),
            }
        }

        result = self.assert_old_and_new_equal(params)
        self.assertEqual(result["clip_duration"], [7.9, 4.4])
        self.assertEqual(result["timelines"][-1]["end"], 12_300_000)

    def test_invalid_timeline_and_malformed_json_return_empty(self) -> None:
        cases = [
            {"shots": self.sample_input["shots"], "duration": 12.3, "timelines": {"start": 5, "end": 5}},
            {"shots": self.sample_input["shots"], "duration": 12.3, "timelines": "{bad json"},
            {"shots": self.sample_input["shots"], "duration": 12.3, "timelines": {"start": "bad", "end": 5}},
        ]

        for params in cases:
            result = self.assert_old_and_new_equal(params)
            self.assertEqual(result["shots"], [])

    def test_missing_shot_fields_and_non_object_shots_match_original(self) -> None:
        cases = [
            {"shots": [{"source_text": "有文本", "clip_role": "" , "story_beat": "有节拍"}], "duration": 8, "timelines": {"start": 0, "end": 8_000_000}},
            {"shots": ["not an object"], "duration": 8, "timelines": {"start": 0, "end": 8_000_000}},
            {"shots": [None], "duration": 8, "timelines": {"start": 0, "end": 8_000_000}},
        ]

        for params in cases:
            self.assertEqual(self.assert_old_and_new_equal(params)["shots"], [])

    def test_duration_bounds_rounding_and_timeline_order_match(self) -> None:
        cases = [
            {
                "shots": [self.sample_input["shots"][0]],
                "duration": 4.0,
                "timelines": {"start": 1_000_001, "end": 5_000_002},
            },
            {
                "shots": self.sample_input["shots"],
                "duration": 8.1,
                "timelines": {"start": 100, "end": 8_100_100},
            },
            {
                "shots": [self.sample_input["shots"][0]],
                "duration": 3.9,
                "timelines": {"start": 0, "end": 3_900_000},
            },
        ]

        for params in cases:
            result = self.assert_old_and_new_equal(params)
            if result["timelines"]:
                self.assertEqual(result["timelines"][-1]["end"], params["timelines"]["end"])

    def test_segments_are_input_only_and_async_entry_point_matches(self) -> None:
        params = dict(self.sample_input)
        params["segments"] = ["different", "segments"]

        direct = run_timeline_planning(params)
        async_result = asyncio.run(main(SimpleNamespace(params=params)))
        self.assertEqual(direct, async_result)
        self.assertEqual(direct, run_both(params)[0])

    def test_project_upgrade_can_cap_each_shot_at_six_seconds(self) -> None:
        shots = [
            {"source_text": f"语义锚点{i}", "clip_role": "状态推进", "story_beat": f"状态{i}成立"}
            for i in range(3)
        ]
        result = run_timeline_planning({
            "duration": 12.3,
            "shots": shots,
            "timelines": {"start": 0, "end": 12_300_000},
            "max_shot_seconds": 6.0,
        })
        self.assertEqual(result["timelines"][-1]["end"], 12_300_000)
        self.assertTrue(result["clip_duration"])
        self.assertLessEqual(max(result["clip_duration"]), 6.0)

    def test_live_semantic_pacing_uses_narration_fragments_and_allows_short_transition_shots(self) -> None:
        shots = [
            {"source_text": "同样的视觉锚点", "narration_text": "短", "clip_role": "状态建立", "story_beat": "第一步成立"},
            {"source_text": "同样的视觉锚点", "narration_text": "稍长一些", "clip_role": "压力升级", "story_beat": "第二步成立"},
            {"source_text": "同样的视觉锚点", "narration_text": "这是最长的完整语义片段", "clip_role": "结果落点", "story_beat": "第三步成立"},
        ]
        result = run_timeline_planning({
            "duration": 12.0,
            "shots": shots,
            "timelines": {"start": 0, "end": 12_000_000},
            "min_shot_seconds": 2.0,
            "max_shot_seconds": 6.0,
        })
        self.assertEqual(result["timelines"][-1]["end"], 12_000_000)
        self.assertEqual([item["narration_text"] for item in result["shots"]], ["短", "稍长一些", "这是最长的完整语义片段"])
        self.assertGreater(result["clip_duration"][2], result["clip_duration"][1])
        self.assertGreater(result["clip_duration"][1], result["clip_duration"][0])
        self.assertGreaterEqual(min(result["clip_duration"]), 2.0)

    def test_audit_summary_reports_all_cases_passed(self) -> None:
        cases = [
            ("synthetic", self.sample_input),
            ("empty", {}),
            ("missing_fields", {"duration": 8}),
            ("json_strings", {"shots": json.dumps(self.sample_input["shots"]), "duration": "12.3", "timelines": json.dumps([self.sample_input["timelines"]])}),
        ]
        results = [audit_case(name, params) for name, params in cases]
        self.assertEqual(summarize(results), {
            "total": 4,
            "passed": 4,
            "failed": 0,
            "failed_cases": [],
            "code_level_equivalent": True,
        })


if __name__ == "__main__":
    unittest.main()
