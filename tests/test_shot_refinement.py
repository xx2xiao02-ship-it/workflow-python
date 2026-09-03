from __future__ import annotations

import json
import unittest
from pathlib import Path

from audit.equivalence_shot_refinement import compare_results
from workflow_1256.shot_refinement import (
    ShotRefinementItem,
    ShotRefinementTransportRequired,
    ShotRefinementValidationError,
    run_shot_refinement,
)


class ShotRefinementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sample_dir = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.sample_input = json.loads((sample_dir / "shot_refinement_input.json").read_text(encoding="utf-8"))
        cls.sample_expected = json.loads((sample_dir / "shot_refinement_expected.json").read_text(encoding="utf-8"))

    @staticmethod
    def llm_transport(item: ShotRefinementItem) -> dict:
        index = 0 if item.segment.startswith("第一") else 1
        return ShotRefinementTests.sample_expected["LLM_list"][index]

    @staticmethod
    def code_transport(item: ShotRefinementItem, _llm: dict) -> dict:
        index = 0 if item.segment.startswith("第一") else 1
        return ShotRefinementTests.sample_expected["Code_list"][index]

    def test_batch_preserves_order_and_output_contract(self) -> None:
        result = run_shot_refinement(
            self.sample_input,
            llm_transport=self.llm_transport,
            code_transport=self.code_transport,
        )
        self.assertEqual(compare_results(self.sample_expected, result), [])
        self.assertEqual(list(result), ["LLM_list", "Code_list"])
        self.assertEqual(len(result["LLM_list"]), 2)
        self.assertEqual(len(result["Code_list"]), 2)

    def test_json_wrapper_and_mismatched_arrays(self) -> None:
        encoded = json.dumps(self.sample_input, ensure_ascii=False)
        result = run_shot_refinement(encoded, llm_transport=self.llm_transport, code_transport=self.code_transport)
        self.assertEqual(len(result["LLM_list"]), 2)
        broken = dict(self.sample_input)
        broken["segments"] = broken["segments"][:1]
        with self.assertRaises(ShotRefinementValidationError):
            run_shot_refinement(broken, llm_transport=self.llm_transport, code_transport=self.code_transport)

    def test_invalid_json_and_missing_transport(self) -> None:
        with self.assertRaises(ShotRefinementValidationError):
            run_shot_refinement("{bad json")
        with self.assertRaises(ShotRefinementTransportRequired):
            run_shot_refinement(self.sample_input)

    def test_invalid_nested_response_is_rejected(self) -> None:
        def bad_llm(_item: ShotRefinementItem) -> dict:
            return {"reasoning_content": "x", "shots": [{"source_text": "missing fields"}]}

        with self.assertRaises(ShotRefinementValidationError):
            run_shot_refinement(self.sample_input, llm_transport=bad_llm, code_transport=self.code_transport)

    def test_narration_text_is_preserved_for_timeline_and_downstream_delivery(self) -> None:
        sample = {
            "duration": [8.0],
            "items": [{"source_text": "原旁白"}],
            "segments": ["原旁白分为两句"],
            "timelines": [{"start": 0, "end": 8_000_000}],
        }
        result = run_shot_refinement(
            sample,
            llm_transport=lambda _item: {"shots": [
                {"narration_text": "原旁白", "source_text": "提出事实", "clip_role": "现象建立", "story_beat": "事实被提出"},
                {"narration_text": "分为两句", "source_text": "形成疑问", "clip_role": "信息触发", "story_beat": "疑问成立"},
            ]},
            code_transport=lambda _item, llm: {
                "shots": [{**shot, "clip_duration": 4.0} for shot in llm["shots"]],
                "clip_duration": [4.0, 4.0],
                "int_duration": [4, 4],
                "timelines": [{"start": 0, "end": 4_000_000}, {"start": 4_000_000, "end": 8_000_000}],
            },
        )
        self.assertEqual(
            [item["narration_text"] for item in result["Code_list"][0]["shots"]],
            ["原旁白", "分为两句"],
        )

    def test_empty_batch_and_invalid_timeline_are_explicit(self) -> None:
        empty = {
            "duration": [],
            "items": [],
            "segments": [],
            "timelines": [],
        }
        result = run_shot_refinement(
            empty,
            llm_transport=self.llm_transport,
            code_transport=self.code_transport,
        )
        self.assertEqual(result, {"LLM_list": [], "Code_list": []})

        broken = json.loads(json.dumps(self.sample_input, ensure_ascii=False))
        broken["timelines"][0] = {"start": 0}
        with self.assertRaises(ShotRefinementValidationError):
            run_shot_refinement(
                broken,
                llm_transport=self.llm_transport,
                code_transport=self.code_transport,
            )

    def test_real_success_batch_preserves_full_contract_and_order(self) -> None:
        real_dir = Path(__file__).parents[1] / "samples" / "real"
        real_input = json.loads(
            (real_dir / "run-7668196429877870619-103964-shot-refinement-input.json")
            .read_text(encoding="utf-8")
        )
        real_output = json.loads(
            (real_dir / "run-7668196429877870619-103964-shot-refinement-output.json")
            .read_text(encoding="utf-8")
        )
        index_by_segment = {
            segment: index for index, segment in enumerate(real_input["segments"])
        }

        def real_llm_transport(item: ShotRefinementItem) -> dict:
            return real_output["LLM_list"][index_by_segment[item.segment]]

        def real_code_transport(item: ShotRefinementItem, _llm: dict) -> dict:
            return real_output["Code_list"][index_by_segment[item.segment]]

        result = run_shot_refinement(
            real_input,
            llm_transport=real_llm_transport,
            code_transport=real_code_transport,
        )
        self.assertEqual(compare_results(real_output, result), [])
        self.assertEqual(list(result), ["LLM_list", "Code_list"])
        self.assertEqual(len(result["LLM_list"]), 9)
        self.assertEqual(len(result["Code_list"]), 9)
        self.assertEqual(
            [item["start"] for item in real_input["timelines"]],
            [0, 16416000, 30840000, 53592000, 66960000, 81168000, 92112000, 100656000, 109416000],
        )
        self.assertEqual(real_input["timelines"][-1]["end"], 111672000)
        self.assertEqual(
            [len(item["shots"]) for item in result["LLM_list"]],
            [3, 2, 3, 2, 2, 2, 1, 1, 1],
        )


if __name__ == "__main__":
    unittest.main()
