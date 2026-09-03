from __future__ import annotations

import json
from pathlib import Path
import unittest

from audit.coze_adapter_workflow_end import run_migrated, run_original_contract
from audit.equivalence_workflow_end import compare_case


class WorkflowEndAuditTests(unittest.TestCase):
    def test_final_draft_url_is_projected_as_content(self) -> None:
        params=json.loads((Path(__file__).parents[1]/"samples"/"synthetic"/"workflow_end_input.json").read_text(encoding="utf-8"))
        self.assertEqual(run_original_contract(params), run_migrated(params))
        self.assertEqual(compare_case(params), [])
        self.assertEqual(run_migrated(params)["content"], params["draft_url"])


if __name__ == "__main__": unittest.main()
