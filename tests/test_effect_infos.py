from __future__ import annotations

import json
from pathlib import Path
import unittest

from audit.coze_adapter_effect_infos import run_both
from audit.equivalence_effect_infos import compare_case
from workflow_1256.effect_infos import run_effect_infos


class EffectInfosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.input = json.loads((root / "effect_infos_input.json").read_text(encoding="utf-8"))
        cls.boundary = json.loads((root / "effect_infos_boundary_cases.json").read_text(encoding="utf-8"))

    def test_old_new_exact(self) -> None:
        original, migrated = run_both(self.input)
        self.assertEqual(original, migrated)
        items = json.loads(migrated["infos"])
        self.assertEqual(list(items[0]), ["effect_title", "start", "end"])
        self.assertEqual([item["start"] for item in items], [0, 6906732])

    def test_boundaries_match_original(self) -> None:
        for case in self.boundary:
            with self.subTest(case=case["name"]): self.assertEqual(compare_case(case["params"]), [])

    def test_length_mismatch_uses_shorter_array(self) -> None:
        output = json.loads(run_effect_infos({"effects":["暗角","暗角"],"timelines":[{"start":0,"end":1}]})["infos"])
        self.assertEqual(len(output), 1)


if __name__ == "__main__": unittest.main()
