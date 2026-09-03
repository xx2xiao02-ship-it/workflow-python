from __future__ import annotations

import json
from pathlib import Path
import unittest

from audit.coze_adapter_add_effects_second import run_contract_second, run_parser_both
from audit.equivalence_add_effects_second import compare_case


class AddEffectsSecondTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.input=json.loads((Path(__file__).parents[1]/"samples"/"synthetic"/"add_effects_second_input.json").read_text(encoding="utf-8"))

    def test_parser_and_contract(self) -> None:
        self.assertEqual(run_parser_both(self.input["effect_infos"])[0], run_parser_both(self.input["effect_infos"])[1])
        self.assertEqual(compare_case(self.input["effect_infos"]), [])
        out=run_contract_second(self.input, lambda url,payload:{"draft_url":url,"effect_ids":["e"],"segment_ids":["s"],"track_id":"t"})
        self.assertEqual(list(out), ["draft_url","effect_ids","segment_ids","track_id"])


if __name__ == "__main__": unittest.main()
