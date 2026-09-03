from __future__ import annotations

import json
from pathlib import Path
import unittest

from audit.coze_adapter_save_draft import run_migrated, run_original_contract
from audit.equivalence_save_draft import compare_case
from workflow_1256.save_draft import SaveDraftTransportRequired, run_save_draft


class SaveDraftNodeAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.input=json.loads((Path(__file__).parents[1]/"samples"/"synthetic"/"save_draft_input.json").read_text(encoding="utf-8"))

    def test_old_new_contract_and_order(self) -> None:
        transport=lambda request:{"draft_url":request.draft_url,"message":"草稿保存成功"}
        self.assertEqual(run_original_contract(self.input, transport), run_migrated(self.input, transport))
        self.assertEqual(compare_case(self.input), [])
        self.assertEqual(list(run_migrated(self.input, transport)), ["draft_url","message"])

    def test_json_string_and_missing_transport(self) -> None:
        transport=lambda request:{"draft_url":request.draft_url,"message":"saved"}
        self.assertEqual(run_migrated(json.dumps(self.input), transport)["draft_url"], self.input["draft_url"])
        with self.assertRaises(SaveDraftTransportRequired): run_save_draft(self.input)


if __name__ == "__main__": unittest.main()
