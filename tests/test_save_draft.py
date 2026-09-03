from __future__ import annotations

import unittest

from workflow_1256.save_draft import (
    SaveDraftTransportRequired,
    SaveDraftValidationError,
    run_save_draft,
)


class SaveDraftTests(unittest.TestCase):
    def test_contract_and_field_order(self) -> None:
        result = run_save_draft(
            {"draft_url": "http://draft/one"},
            transport=lambda request: {"draft_url": request.draft_url, "message": "saved"},
        )
        self.assertEqual(result, {"draft_url": "http://draft/one", "message": "saved"})
        self.assertEqual(list(result), ["draft_url", "message"])

    def test_missing_transport_and_invalid_response_are_rejected(self) -> None:
        with self.assertRaises(SaveDraftTransportRequired):
            run_save_draft({"draft_url": "http://draft/one"})
        with self.assertRaises(SaveDraftValidationError):
            run_save_draft({"draft_url": "http://draft/one"}, transport=lambda _: {"message": "saved"})


if __name__ == "__main__":
    unittest.main()
