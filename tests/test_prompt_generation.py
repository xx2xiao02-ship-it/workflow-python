from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from audit.coze_adapter_113743 import run_both
from audit.equivalence import compare_outputs, json_type
from audit.equivalence_prompt_generation import load_cases, run_audit, summarize
from workflow_1256.prompt_generation import main, run_prompt_generation


class PromptGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.input = json.loads((root / "prompt_generation_input.json").read_text(encoding="utf-8"))
        cls.expected = json.loads((root / "prompt_generation_expected.json").read_text(encoding="utf-8"))

    def test_synthetic_output_and_field_contract(self) -> None:
        original, migrated = run_both(self.input)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertEqual(migrated, self.expected)
        self.assertEqual(
            list(migrated),
            ["video_prompt", "int_duration", "ref_image_f", "ref_image_e", "camera_fixed", "error"],
        )
        self.assertEqual(
            {key: json_type(value) for key, value in migrated.items()},
            {
                "video_prompt": "array",
                "int_duration": "array",
                "ref_image_f": "array",
                "ref_image_e": "array",
                "camera_fixed": "array",
                "error": "string",
            },
        )

    def test_array_order_timeline_duration_and_frames(self) -> None:
        result = run_prompt_generation(self.input)
        self.assertEqual(len(result["video_prompt"]), 4)
        self.assertEqual(result["int_duration"], [5, 4, 12, 6])
        self.assertEqual(
            result["ref_image_f"],
            [
                "synthetic://frame/1",
                "synthetic://frame/2",
                "synthetic://frame/3",
                "synthetic://frame/4",
            ],
        )
        self.assertEqual(
            result["ref_image_e"],
            ["synthetic://frame/2", "synthetic://frame/3", "", ""],
        )
        self.assertIn("0.0-2.0秒", result["video_prompt"][0])
        self.assertIn("2.0-5.0秒", result["video_prompt"][0])
        self.assertEqual(result["camera_fixed"], [True, False, False, True])

    def test_all_synthetic_and_boundary_cases_match(self) -> None:
        results = run_audit()
        summary = summarize(results)
        self.assertEqual(summary["total"], 9)
        self.assertEqual(summary["failed"], 0, results)
        self.assertTrue(summary["code_level_equivalent"])

    def test_json_string_empty_missing_and_exceptional_inputs(self) -> None:
        by_name = {case.name: case for case in load_cases()}
        for name in ("json_string_wrappers", "empty_arrays", "missing_fields", "exceptional_top_level_inputs"):
            with self.subTest(name=name):
                original, migrated = run_both(by_name[name].params)
                self.assertEqual(compare_outputs(original, migrated), [])
        self.assertEqual(run_both(by_name["empty_arrays"].params)[1]["video_prompt"], [])
        self.assertEqual(run_both(by_name["exceptional_top_level_inputs"].params)[1]["error"], "plan_out_list、ref_image、clip_duration 均为空或格式不正确")

    def test_invalid_timeline_missing_stage_and_count_mismatch(self) -> None:
        by_name = {case.name: case for case in load_cases()}
        for name in ("invalid_timeline_and_missing_stage_fields", "count_mismatch", "invalid_indexes_and_duration_bounds", "nested_output_wrapper"):
            with self.subTest(name=name):
                original, migrated = run_both(by_name[name].params)
                self.assertEqual(compare_outputs(original, migrated), [])

    def test_real_topology_redacted_structural_fixture(self) -> None:
        root = Path(__file__).parents[1] / "samples" / "real"
        upstream = json.loads(
            (root / "run-7668196429877870619-170263-structural-input.json").read_text(
                encoding="utf-8"
            )
        )
        plan_groups = []
        ref_groups = []
        duration_groups = []
        seed_cursor = 0
        image_cursor = 0
        for code_group in upstream["Code_list"]:
            durations = list(code_group["clip_duration"])
            images = upstream["image_url_list"][image_cursor:image_cursor + len(durations)]
            image_cursor += len(durations)
            seed_cursor += len(durations)
            plan_groups.append({
                "plans": [
                    {
                        "shot_index": index,
                        "duration": int(duration),
                        "stages": [{
                            "time_range": f"0-{duration}s",
                            "action": "redacted structural action",
                            "camera_motion": "固定机位",
                        }],
                    }
                    for index, duration in enumerate(durations)
                ]
            })
            ref_groups.append({"ref_image": images})
            duration_groups.append({"clip_duration": durations})
        params = {
            "plan_out_list": plan_groups,
            "ref_image": ref_groups,
            "clip_duration": duration_groups,
        }
        original, migrated = run_both(params)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertEqual(len(migrated["video_prompt"]), 17)
        self.assertEqual(len(migrated["int_duration"]), 17)
        self.assertEqual(len(migrated["ref_image_f"]), 17)
        self.assertEqual(len(migrated["camera_fixed"]), 17)

    def test_async_entry_point_matches(self) -> None:
        result = asyncio.run(main(SimpleNamespace(params=self.input)))
        self.assertEqual(result, self.expected)


    def test_story_context_changes_video_material_prompt(self) -> None:
        params = json.loads(json.dumps(self.input, ensure_ascii=False))
        params["plan_out_list"] = [{"plans": [params["plan_out_list"][0]["plans"][0]]}]
        params["ref_image"] = [{"ref_image": [params["ref_image"][0]["ref_image"][0]]}]
        params["clip_duration"] = [{"clip_duration": [params["clip_duration"][0]["clip_duration"][0]]}]
        params["story_context"] = [
            {
                "semantic_mapping": "UNIQUE_VIDEO_MAPPING",
                "silent_action": "UNIQUE_VIDEO_ACTION",
                "metaphor": "UNIQUE_VIDEO_METAPHOR",
                "state_before": "UNIQUE_VIDEO_BEFORE",
                "turn": "UNIQUE_VIDEO_TURN",
                "state_after": "UNIQUE_VIDEO_AFTER",
                "bridge_to_next": "UNIQUE_VIDEO_BRIDGE",
            }
        ]
        result = run_prompt_generation(params)
        self.assertIn("UNIQUE_VIDEO_MAPPING", result["video_prompt"][0])
        self.assertIn("UNIQUE_VIDEO_ACTION", result["video_prompt"][0])
        self.assertIn("UNIQUE_VIDEO_METAPHOR", result["video_prompt"][0])

    def test_frame_continuity_can_be_disabled_at_material_layer(self) -> None:
        params = json.loads(json.dumps(self.input, ensure_ascii=False))
        params["use_frame_continuity"] = False
        result = run_prompt_generation(params)
        self.assertEqual(result["ref_image_e"], ["", "", "", ""])
        self.assertEqual(len(result["video_prompt"]), 4)

    def test_frame_continuity_can_be_explicitly_enabled(self) -> None:
        params = json.loads(json.dumps(self.input, ensure_ascii=False))
        params["use_frame_continuity"] = True
        result = run_prompt_generation(params)
        self.assertEqual(
            result["ref_image_e"],
            ["synthetic://frame/2", "synthetic://frame/3", "", ""],
        )


if __name__ == "__main__":
    unittest.main()
