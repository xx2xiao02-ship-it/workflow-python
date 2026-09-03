from __future__ import annotations

import json
import unittest
from pathlib import Path

from audit.equivalence_aigc_animation import run_audit, summarize
from workflow_1256.aigc_animation import (
    AIGCAnimationTransportRequired,
    AIGCAnimationValidationError,
    derive_segment_timeline,
    normalize_video_infos,
    run_aigc_animation,
)


class AIGCAnimationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.sample = json.loads((root / "aigc_animation_input.json").read_text(encoding="utf-8"))
        cls.expected = json.loads((root / "aigc_animation_expected.json").read_text(encoding="utf-8"))

    def test_normalization_keeps_microsecond_order_and_fields(self) -> None:
        values = normalize_video_infos(self.sample["video_infos"])
        self.assertEqual(len(values), 3)
        self.assertEqual(list(values[0]), ["video_url", "width", "height", "start", "end", "duration", "mask", "transition", "transition_duration", "volume"])
        self.assertEqual([(item["start"], item["end"]) for item in values], [(0, 4000000), (4000000, 8500000), (8500000, 12000000)])

    def test_executor_projection_preserves_yaml_output_order(self) -> None:
        calls = []

        def executor(draft_url, video_infos, options):
            calls.append((draft_url, video_infos, dict(options)))
            return self.expected

        actual = run_aigc_animation(self.sample, executor=executor)
        self.assertEqual(actual, self.expected)
        self.assertEqual(list(actual), ["draft_url", "segment_ids", "segment_infos", "track_id", "video_ids"])
        self.assertEqual(calls[0][0], "draft://synthetic-aigc-animation")
        self.assertEqual(calls[0][2], {})

    def test_optional_fields_are_forwarded_without_invention(self) -> None:
        calls = []

        def executor(_draft_url, _video_infos, options):
            calls.append(dict(options))
            return self.expected

        params = dict(self.sample, alpha=0.7, scale_x=1.2, scale_y=0.8, transform_x=10, transform_y=-20, scene_timelines=[{"start": 0, "end": 2000000}] * 3)
        run_aigc_animation(params, executor=executor)
        self.assertEqual(calls[0]["alpha"], 0.7)
        self.assertEqual(calls[0]["scene_timelines"][0]["end"], 2000000)

    def test_empty_json_and_invalid_json_are_rejected(self) -> None:
        with self.assertRaises(AIGCAnimationValidationError):
            run_aigc_animation({"draft_url": "draft://x", "video_infos": "[]"}, executor=lambda *_: self.expected)
        with self.assertRaises(AIGCAnimationValidationError):
            normalize_video_infos("{bad json")

    def test_missing_fields_and_invalid_time_are_rejected(self) -> None:
        with self.assertRaises(AIGCAnimationValidationError):
            normalize_video_infos('[{"video_url":"https://synthetic.invalid/a.mp4","end":1}]')
        with self.assertRaises(AIGCAnimationValidationError):
            normalize_video_infos('[{"video_url":"https://synthetic.invalid/a.mp4","start":2,"end":1}]')

    def test_fractional_times_truncate_like_original(self) -> None:
        value = normalize_video_infos('[{"video_url":"https://synthetic.invalid/a.mp4","start":0.9,"end":10.9}]')
        self.assertEqual((value[0]["start"], value[0]["end"], value[0]["duration"]), (0, 10, 10))

    def test_invalid_scene_timeline_does_not_enable_continuity(self) -> None:
        raw = '[{"video_url":"https://synthetic.invalid/a.mp4","start":0,"end":10},{"video_url":"https://synthetic.invalid/b.mp4","start":20,"end":30}]'
        self.assertEqual(derive_segment_timeline(raw, [{"start": 1, "end": 1}]), [{"index": 0, "start": 0, "end": 10, "duration": 10}, {"index": 1, "start": 20, "end": 30, "duration": 10}])

    def test_no_executor_blocks_external_side_effect(self) -> None:
        with self.assertRaises(AIGCAnimationTransportRequired):
            run_aigc_animation(self.sample)

    def test_code_level_audit(self) -> None:
        summary = summarize(run_audit())
        self.assertEqual(summary["total"], 8)
        self.assertTrue(summary["contract_equivalent"])
        self.assertFalse(summary["runtime_side_effects_verified"])


if __name__ == "__main__":
    unittest.main()
