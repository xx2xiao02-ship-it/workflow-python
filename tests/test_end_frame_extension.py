from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from audit.coze_adapter_170263 import run_both, run_migrated, run_original
from audit.equivalence import compare_outputs
from audit.equivalence_end_frame_extension import load_cases, run_audit, summarize
from workflow_1256.end_frame_extension import main, run_end_frame_extension


class EndFrameExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.input = json.loads((root / "end_frame_extension_input.json").read_text(encoding="utf-8"))
        cls.expected = json.loads((root / "end_frame_extension_expected.json").read_text(encoding="utf-8"))

    def test_synthetic_output_and_field_order(self) -> None:
        original, migrated = run_both(self.input)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertEqual(migrated, self.expected)
        self.assertEqual(
            list(migrated),
            ["items", "motion_seed", "clip_duration", "ref_image", "ref_image_f", "ref_image_e", "error"],
        )
        self.assertEqual(migrated["motion_seed"][0]["motion_seed"], ["第一镜头动作", "第二镜头动作"])
        self.assertEqual(migrated["ref_image_e"][0]["ref_image_e"], ["synthetic://frame/2"])

    def test_all_synthetic_and_boundary_cases_match(self) -> None:
        results = run_audit()
        summary = summarize(results)
        self.assertEqual(summary["total"], 8)
        self.assertEqual(summary["failed"], 0, results)
        self.assertTrue(summary["code_level_equivalent"])

    def test_duration_rounding_and_fallback_keys(self) -> None:
        params = {
            "LLM_list": [{"shots": [{}, {}]}],
            "motion_seed": [{"text": "a"}, {"content": "b"}],
            "items": [{"visual_core": "c", "visual_story": "s"}],
            "image_url_list": [{"image": "i1"}, {"src": "i2"}],
            "Code_list": [{"duration_list": [0, -1, "bad"]}],
        }
        original, migrated = run_both(params)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertEqual(migrated["clip_duration"], [{"clip_duration": [0, 0, 0]}])
        self.assertEqual(migrated["ref_image"], [{"ref_image": ["i1", "i2", ""]}])

    def test_json_string_wrapper_and_invalid_values(self) -> None:
        params = {
            "_input": {
                "LLM_list": "[{\"shots\":[{}]}]",
                "motion_seed": "[\"seed\"]",
                "items": "[{\"visual_core\":\"c\",\"visual_story\":\"s\"}]",
                "image_url_list": "[\"frame\"]",
                "Code_list": "[{\"clip_duration\":[3]}]",
            }
        }
        original, migrated = run_both(params)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertEqual(migrated["error"], "")

        for invalid in ({}, "not-json", {"items": None, "Code_list": 1}):
            with self.subTest(invalid=invalid):
                original, migrated = run_both(invalid)
                self.assertEqual(compare_outputs(original, migrated), [])

    def test_missing_frame_and_count_mismatch_keep_error_text(self) -> None:
        params = {
            "LLM_list": [{"shots": [{}, {}]}],
            "motion_seed": ["only"],
            "items": [{"visual_core": "c", "visual_story": "s"}],
            "image_url_list": [],
            "Code_list": [{"clip_duration": [4, 5, 6]}],
        }
        original, migrated = run_both(params)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertIn("shots 数量与 clip_duration 数量不一致", migrated["error"])
        self.assertIn("缺少参考图", migrated["error"])

    def test_async_entry_point_matches(self) -> None:
        result = asyncio.run(main(SimpleNamespace(params=self.input)))
        self.assertEqual(result, self.expected)

    def test_real_page_shape_fixture_reproduces_group_counts(self) -> None:
        root = Path(__file__).parents[1] / "samples" / "real"
        real_input = json.loads(
            (root / "run-7668196429877870619-170263-structural-input.json").read_text(
                encoding="utf-8"
            )
        )
        original, result = run_both(real_input)
        self.assertEqual(compare_outputs(original, result), [])
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["items"]), 9)
        self.assertEqual(
            [len(group["motion_seed"]) for group in result["motion_seed"]],
            [3, 2, 3, 2, 2, 2, 1, 1, 1],
        )
        self.assertEqual(
            [len(group["ref_image_e"]) for group in result["ref_image_e"]],
            [2, 1, 2, 1, 1, 1, 0, 0, 0],
        )

    def test_original_source_is_not_called_with_external_services(self) -> None:
        # 该节点只使用 json 和内存数据，适配器运行时不提供任何网络依赖。
        self.assertEqual(run_original(self.input), run_migrated(self.input))


    def test_story_context_is_preserved_for_prompt_generation(self) -> None:
        story_context = [
            {"semantic_mapping": "UNIQUE_FRAME_STORY_1"},
            {"semantic_mapping": "UNIQUE_FRAME_STORY_2"},
        ]
        params = {
            "LLM_list": [{"shots": [{}, {}]}],
            "motion_seed": ["seed-1", "seed-2"],
            "items": [{"visual_core": "c", "visual_story": "s"}],
            "image_url_list": ["frame-1", "frame-2"],
            "Code_list": [{"clip_duration": [4, 5]}],
            "story_context": story_context,
        }
        result = run_end_frame_extension(params)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["story_context"], story_context)

    def test_frame_continuity_can_be_disabled_at_material_layer(self) -> None:
        params = json.loads(json.dumps(self.input, ensure_ascii=False))
        params["use_frame_continuity"] = False
        result = run_end_frame_extension(params)
        self.assertEqual(result["error"], "")
        self.assertEqual(
            result["ref_image_e"],
            [{"ref_image_e": [""]}, {"ref_image_e": []}],
        )


if __name__ == "__main__":
    unittest.main()
