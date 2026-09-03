from __future__ import annotations

import json
import unittest
from pathlib import Path

from audit.equivalence_digital_human import run_audit, summarize
from workflow_1256.digital_human import (
    DigitalHumanTransportRequired,
    DigitalHumanValidationError,
    derive_segment_timeline,
    normalize_video_infos,
    run_digital_human,
)


class DigitalHumanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.sample = json.loads((root / "digital_human_input.json").read_text(encoding="utf-8"))
        cls.expected = json.loads(
            (root / "digital_human_expected.json").read_text(encoding="utf-8")
        )

    def test_normalization_keeps_microsecond_order_and_fields(self) -> None:
        values = normalize_video_infos(self.sample["video_infos"])
        self.assertEqual(len(values), 3)
        self.assertEqual(
            list(values[0]),
            [
                "video_url",
                "width",
                "height",
                "start",
                "end",
                "duration",
                "mask",
                "transition",
                "transition_duration",
                "volume",
            ],
        )
        self.assertEqual(
            [(item["start"], item["end"]) for item in values],
            [(0, 4000000), (4000000, 8500000), (8500000, 12000000)],
        )
        self.assertTrue(all(item["transition"] == "胶片定格" for item in values))

    def test_executor_projection_preserves_yaml_and_nested_output_order(self) -> None:
        calls = []

        def executor(draft_url, video_infos, options):
            calls.append((draft_url, video_infos, dict(options)))
            scrambled = dict(self.expected)
            scrambled["segment_infos"] = [
                {"id": row["id"], "start": row["start"], "end": row["end"]}
                for row in self.expected["segment_infos"]
            ]
            return scrambled

        actual = run_digital_human(self.sample, executor=executor)
        self.assertEqual(actual, self.expected)
        self.assertEqual(
            list(actual),
            ["draft_url", "segment_ids", "segment_infos", "track_id", "video_ids"],
        )
        self.assertEqual(list(actual["segment_infos"][0]), ["end", "id", "start"])
        self.assertEqual(calls[0][0], "draft://synthetic-digital-human")
        self.assertEqual(calls[0][2], {})

    def test_optional_fields_are_forwarded_without_invention(self) -> None:
        calls = []

        def executor(_draft_url, _video_infos, options):
            calls.append(dict(options))
            return self.expected

        params = dict(
            self.sample,
            alpha=0.7,
            scale_x=1.2,
            scale_y=0.8,
            transform_x=10,
            transform_y=-20,
            scene_timelines=[{"start": 0, "end": 2000000}] * 3,
        )
        run_digital_human(params, executor=executor)
        self.assertEqual(calls[0]["alpha"], 0.7)
        self.assertEqual(calls[0]["scene_timelines"][0]["end"], 2000000)

    def test_json_string_node_input_is_supported(self) -> None:
        actual = run_digital_human(
            json.dumps(self.sample, ensure_ascii=False),
            executor=lambda *_: self.expected,
        )
        self.assertEqual(actual, self.expected)

    def test_empty_json_and_invalid_json_are_rejected(self) -> None:
        with self.assertRaises(DigitalHumanValidationError):
            run_digital_human(
                {"draft_url": "draft://x", "video_infos": "[]"},
                executor=lambda *_: self.expected,
            )
        with self.assertRaises(DigitalHumanValidationError):
            normalize_video_infos("{bad json")

    def test_missing_fields_and_invalid_time_are_rejected(self) -> None:
        with self.assertRaisesRegex(DigitalHumanValidationError, "draft_url"):
            run_digital_human(
                {"video_infos": self.sample["video_infos"]},
                executor=lambda *_: self.expected,
            )
        with self.assertRaisesRegex(DigitalHumanValidationError, "video_infos"):
            run_digital_human(
                {"draft_url": "draft://x"},
                executor=lambda *_: self.expected,
            )
        with self.assertRaises(DigitalHumanValidationError):
            normalize_video_infos(
                '[{"video_url":"https://synthetic.invalid/a.mp4","end":1}]'
            )
        with self.assertRaises(DigitalHumanValidationError):
            normalize_video_infos(
                '[{"video_url":"https://synthetic.invalid/a.mp4","start":2,"end":1}]'
            )

    def test_fractional_times_truncate_like_original(self) -> None:
        value = normalize_video_infos(
            '[{"video_url":"https://synthetic.invalid/a.mp4","start":0.9,"end":10.9}]'
        )
        self.assertEqual(
            (value[0]["start"], value[0]["end"], value[0]["duration"]),
            (0, 10, 10),
        )
        with self.assertRaisesRegex(DigitalHumanValidationError, "invalid end time"):
            normalize_video_infos(
                '[{"video_url":"https://synthetic.invalid/a.mp4","start":0.9,"end":0.95}]'
            )

    def test_invalid_volume_type_keeps_original_type_error(self) -> None:
        with self.assertRaises(TypeError):
            normalize_video_infos(
                '[{"video_url":"https://synthetic.invalid/a.mp4","start":0,"end":10,"volume":"loud"}]'
            )

    def test_invalid_scene_timeline_does_not_enable_continuity(self) -> None:
        raw = (
            '[{"video_url":"https://synthetic.invalid/a.mp4","start":0,"end":10},'
            '{"video_url":"https://synthetic.invalid/b.mp4","start":20,"end":30}]'
        )
        self.assertEqual(
            derive_segment_timeline(raw, [{"start": 1, "end": 1}]),
            [
                {"index": 0, "start": 0, "end": 10, "duration": 10},
                {"index": 1, "start": 20, "end": 30, "duration": 10},
            ],
        )

    def test_no_executor_blocks_external_side_effect(self) -> None:
        with self.assertRaises(DigitalHumanTransportRequired):
            run_digital_human(self.sample)

    def test_code_level_audit(self) -> None:
        summary = summarize(run_audit())
        self.assertEqual(summary["total"], 12)
        self.assertEqual(summary["passed"], 12)
        self.assertTrue(summary["contract_equivalent"])
        self.assertFalse(summary["runtime_side_effects_verified"])


if __name__ == "__main__":
    unittest.main()
