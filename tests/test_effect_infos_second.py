from __future__ import annotations

import json
from pathlib import Path
import unittest

from audit.coze_adapter_effect_infos_second import run_both_second
from audit.equivalence_effect_infos_second import compare_case


class EffectInfosSecondTests(unittest.TestCase):
    def test_second_effect_old_new(self) -> None:
        params = json.loads((Path(__file__).parents[1]/"samples"/"synthetic"/"effect_infos_second_input.json").read_text(encoding="utf-8"))
        original, migrated = run_both_second(params)
        self.assertEqual(original, migrated)
        self.assertEqual(compare_case(params), [])
        self.assertEqual(json.loads(migrated["infos"])[0]["effect_title"], "蓝色丝印")


if __name__ == "__main__": unittest.main()
