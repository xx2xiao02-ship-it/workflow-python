from __future__ import annotations

import json
from pathlib import Path
import unittest

from audit.coze_adapter_add_effects import run_contract, run_migrated_parser, run_original_parser
from audit.equivalence_add_effects import compare_case
from workflow_1256.add_effects import AddEffectsTransportRequired, run_add_effects


class AddEffectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.input = json.loads((root / "add_effects_input.json").read_text(encoding="utf-8"))
        cls.boundary = json.loads((root / "add_effects_boundary_cases.json").read_text(encoding="utf-8"))

    def test_parser_matches_original(self) -> None:
        self.assertEqual(run_original_parser(self.input["effect_infos"]), run_migrated_parser(self.input["effect_infos"]))
        for case in self.boundary:
            with self.subTest(case=case["name"]): self.assertEqual(compare_case(case["effect_infos"]), [])

    def test_contract_projection(self) -> None:
        def executor(url: str, payload: str) -> dict[str, object]:
            self.assertEqual(len(json.loads(payload)), 1)
            return {"draft_url": url, "effect_ids":["e1"], "segment_ids":["s1"], "track_id":"t1"}
        self.assertEqual(run_contract(self.input, executor), {"draft_url":self.input["draft_url"],"effect_ids":["e1"],"segment_ids":["s1"],"track_id":"t1"})

    def test_no_executor_blocks(self) -> None:
        with self.assertRaises(AddEffectsTransportRequired): run_add_effects(self.input)


if __name__ == "__main__": unittest.main()
