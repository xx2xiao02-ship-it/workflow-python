from __future__ import annotations

import json
import unittest
from pathlib import Path

from audit.coze_adapter_add_keyframes import run_migrated_contract, run_migrated_parser, run_original_contract, run_original_parser
from audit.equivalence_add_keyframes import compare_parser_case, load_cases, run_audit, summarize
from audit.real_fixture_add_keyframes import run_real_structural_audit
from workflow_1256.add_keyframes import AddKeyframesTransportRequired, run_add_keyframes


class AddKeyframesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.input = json.loads((root / "add_keyframes_input.json").read_text(encoding="utf-8"))
        cls.expected = json.loads((root / "add_keyframes_expected.json").read_text(encoding="utf-8"))

    def test_parser_matches_original_for_all_cases(self) -> None:
        results = run_audit()
        summary = summarize(results)
        self.assertEqual(summary["total"], 12)
        self.assertEqual(summary["failed"], 0, results)
        self.assertTrue(summary["parser_equivalent"])

    def test_successful_contract_projects_only_draft_url(self) -> None:
        def executor(draft_url: str, keyframes: str) -> dict[str, str]:
            self.assertEqual(len(json.loads(keyframes)), 4)
            return {"draft_url": draft_url, "keyframes_added": 4, "affected_segments": ["seg-a", "seg-b"]}

        self.assertEqual(run_migrated_contract(self.input, executor), self.expected)
        self.assertEqual(run_original_contract(self.input, executor), self.expected)

    def test_no_executor_never_fakes_write_result(self) -> None:
        with self.assertRaises(AddKeyframesTransportRequired):
            run_add_keyframes(self.input)

    def test_json_string_and_nested_input(self) -> None:
        cases = {case.name: case for case in load_cases()}
        for name in ("json_string_wrapped_input", "nested_input_wrapper"):
            with self.subTest(name=name):
                params = cases[name].params
                parsed = json.loads(params) if isinstance(params, str) else params
                inner = parsed.get("input", parsed)
                self.assertEqual(run_migrated_parser(inner["keyframes"]), run_original_parser(inner["keyframes"]))

    def test_real_coze_structural_fixture(self) -> None:
        observed = run_real_structural_audit()
        self.assertEqual(observed["input_keyframe_count"], 36)
        self.assertEqual(observed["executor_call_count"], 1)
        self.assertEqual(observed["output_field_order"], ["draft_url"])
        self.assertFalse(observed["remote_write_executed"])


if __name__ == "__main__":
    unittest.main()
