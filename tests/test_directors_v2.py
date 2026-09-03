from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from workflow_1256.directors_v2 import (
    DirectorsV2Request,
    DirectorsV2TransportRequired,
    DirectorsV2ValidationError,
    build_request,
    main,
    normalize_response,
    run_directors_v2,
)
from audit.equivalence_directors_v2 import compare_responses
from audit.coze_adapter_directors_v2 import (
    run_migrated,
    run_migrated_with_model_responses,
    run_original,
    run_original_with_model_responses,
)


class DirectorsV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sample_dir = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.sample_input = json.loads(
            (sample_dir / "directors_v2_input.json").read_text(encoding="utf-8")
        )
        cls.sample_expected = json.loads(
            (sample_dir / "directors_v2_expected.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def synthetic_transport(request: DirectorsV2Request) -> dict:
        assert request == DirectorsV2Request(
            text="第一段 synthetic 文案。\n第二段 synthetic 文案。"
        )
        return DirectorsV2Tests.sample_expected

    def test_synthetic_contract_and_array_order(self) -> None:
        result = run_directors_v2(self.sample_input, transport=self.synthetic_transport)
        self.assertEqual(result, self.sample_expected)
        self.assertEqual(
            list(result), ["director_plan", "ok", "segment_beats", "segments"]
        )
        self.assertEqual([item["segment_index"] for item in result["segment_beats"]], [0, 1])
        self.assertEqual(result["segments"], ["第一段 synthetic 文案。", "第二段 synthetic 文案。"])

    def test_json_string_and_nested_wrappers(self) -> None:
        encoded = json.dumps(self.sample_input, ensure_ascii=False)
        self.assertEqual(
            build_request(encoded),
            DirectorsV2Request(text=self.sample_input["text"]),
        )
        self.assertEqual(
            build_request({"params": {"_input": self.sample_input}}),
            DirectorsV2Request(text=self.sample_input["text"]),
        )

    def test_missing_and_invalid_inputs_are_rejected(self) -> None:
        for value in ({}, [], "{bad json", {"text": 1}, {"text": None}):
            with self.subTest(value=value):
                with self.assertRaises(DirectorsV2ValidationError):
                    build_request(value)

    def test_empty_text_is_preserved_as_a_string(self) -> None:
        self.assertEqual(build_request({"text": ""}), DirectorsV2Request(text=""))

    def test_missing_response_fields_and_wrong_nested_types_are_rejected(self) -> None:
        with self.assertRaises(DirectorsV2ValidationError):
            normalize_response({"ok": True, "segments": [], "segment_beats": []})
        broken = dict(self.sample_expected)
        broken["segment_beats"] = [{"beats": [], "rhythm": "x"}]
        with self.assertRaises(DirectorsV2ValidationError):
            normalize_response(broken)

    def test_no_transport_never_claims_real_plugin_execution(self) -> None:
        with self.assertRaises(DirectorsV2TransportRequired):
            run_directors_v2(self.sample_input)

    def test_async_entry_point_uses_same_contract(self) -> None:
        result = asyncio.run(main(self.sample_input, transport=self.synthetic_transport))
        self.assertEqual(result, self.sample_expected)

    def test_captured_coze_fixture_preserves_full_shape_and_order(self) -> None:
        real_dir = Path(__file__).parents[1] / "samples" / "real"
        real_input = json.loads(
            (real_dir / "run-7668196429877870619-129109-directors-v2-input.json")
            .read_text(encoding="utf-8")
        )
        real_output = json.loads(
            (real_dir / "run-7668196429877870619-129109-directors-v2-output.json")
            .read_text(encoding="utf-8")
        )

        result = run_directors_v2(real_input, transport=lambda _: real_output)
        self.assertEqual(compare_responses(real_output, result), [])
        self.assertEqual(len(result["segments"]), 9)
        self.assertEqual(len(result["segment_beats"]), 9)
        self.assertEqual(
            [item["segment_index"] for item in result["segment_beats"]],
            list(range(9)),
        )
        self.assertTrue(result["ok"])

    def test_sanitized_original_and_migrated_logic_match_with_same_model_responses(self) -> None:
        real_dir = Path(__file__).parents[1] / "samples" / "real"
        real_input = json.loads(
            (real_dir / "run-7668196429877870619-129109-directors-v2-input.json")
            .read_text(encoding="utf-8")
        )
        real_output = json.loads(
            (real_dir / "run-7668196429877870619-129109-directors-v2-output.json")
            .read_text(encoding="utf-8")
        )

        original = run_original(real_input, real_output)
        migrated = run_migrated(real_input, real_output)
        self.assertEqual(compare_responses(original, migrated), [])
        self.assertEqual(original, migrated)

    def test_original_and_migrated_reject_the_same_malformed_model_cases(self) -> None:
        real_dir = Path(__file__).parents[1] / "samples" / "real"
        real_input = json.loads(
            (real_dir / "run-7668196429877870619-129109-directors-v2-input.json")
            .read_text(encoding="utf-8")
        )
        cases = [
            ("bad_pick_json", "{bad", "{}"),
            ("bad_director_json", '{"dir":"view_dir"}', "{bad"),
            ("missing_d", '{"dir":"view_dir"}', '{"seg": []}'),
            ("missing_seg", '{"dir":"view_dir"}', '{"d": {}}'),
        ]
        for name, pick_raw, director_raw in cases:
            with self.subTest(name=name):
                with self.assertRaises(Exception):
                    run_original_with_model_responses(real_input, pick_raw, director_raw)
                with self.assertRaises(Exception):
                    run_migrated_with_model_responses(real_input, pick_raw, director_raw)


if __name__ == "__main__":
    unittest.main()
