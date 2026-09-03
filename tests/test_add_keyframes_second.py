from __future__ import annotations

import json
from pathlib import Path
import unittest

from audit.coze_adapter_add_keyframes_second import run_contract, run_migrated, run_original
from audit.equivalence_add_keyframes_second import compare_case
from audit.real_fixture_add_keyframes_second import run_real_structural_audit
from workflow_1256.add_keyframes_second import AddKeyframesTransportRequired


class AddKeyframesSecondTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).parents[1] / "samples" / "synthetic" / "add_keyframes_second_input.json"
        cls.input = json.loads(path.read_text(encoding="utf-8"))

    def test_parser_matches_original(self) -> None:
        self.assertEqual(run_original(self.input), run_migrated(self.input))

    def test_contract_projects_only_draft_url(self) -> None:
        def executor(draft_url: str, keyframes: str) -> dict[str, str]:
            self.assertEqual(len(json.loads(keyframes)), 4)
            return {"draft_url": draft_url, "keyframes_added": 4}

        self.assertEqual(run_contract(self.input, executor), {"draft_url": self.input["draft_url"]})

    def test_no_executor_blocks_without_faking_write(self) -> None:
        from workflow_1256.add_keyframes_second import run_add_keyframes
        with self.assertRaises(AddKeyframesTransportRequired):
            run_add_keyframes(self.input)

    def test_real_17_segment_structural_input(self) -> None:
        observed = run_real_structural_audit()
        self.assertEqual(observed["input_keyframe_count"], 34)
        self.assertEqual(observed["executor_call_count"], 1)
        self.assertEqual(observed["output_field_order"], ["draft_url"])
        self.assertFalse(observed["remote_write_executed"])

    def test_invalid_input_matches_original(self) -> None:
        invalid = {"draft_url": self.input["draft_url"], "keyframes": "not-json"}
        self.assertEqual(compare_case(invalid), [])


if __name__ == "__main__":
    unittest.main()
