from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from workflow_1256.create_draft import (
    CreateDraftRequest,
    CreateDraftTransportRequired,
    CreateDraftValidationError,
    build_request,
    main,
    run_create_draft,
)


class CreateDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sample_dir = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.sample_input = json.loads(
            (sample_dir / "create_draft_input.json").read_text(encoding="utf-8")
        )
        cls.sample_expected = json.loads(
            (sample_dir / "create_draft_expected.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def synthetic_transport(request: CreateDraftRequest) -> dict[str, str]:
        assert request == CreateDraftRequest(height=1920, width=1080)
        return {
            "draft_url": "synthetic://capcut-mate/drafts/demo-draft",
            "tip_url": "synthetic://capcut-mate/help/draft",
        }

    def test_workflow_contract_and_field_order(self) -> None:
        result = run_create_draft(self.sample_input, transport=self.synthetic_transport)
        self.assertEqual(result, self.sample_expected)
        self.assertEqual(list(result), ["draft_url", "tip_url"])
        self.assertIsInstance(result["draft_url"], str)
        self.assertIsInstance(result["tip_url"], str)

    def test_missing_input_uses_plugin_schema_defaults(self) -> None:
        self.assertEqual(build_request({}), CreateDraftRequest(height=1080, width=1920))
        self.assertEqual(
            run_create_draft({}, transport=lambda _: self.sample_expected),
            self.sample_expected,
        )

    def test_json_object_input_and_nested_params_are_supported_by_adapter(self) -> None:
        encoded = json.dumps(self.sample_input, ensure_ascii=False)
        self.assertEqual(build_request(encoded), CreateDraftRequest(height=1920, width=1080))
        self.assertEqual(
            build_request({"params": {"_input": self.sample_input}}),
            CreateDraftRequest(height=1920, width=1080),
        )

    def test_empty_json_and_invalid_json_are_distinguished(self) -> None:
        self.assertEqual(build_request(""), CreateDraftRequest(height=1080, width=1920))
        with self.assertRaises(CreateDraftValidationError):
            build_request("{bad json")

    def test_invalid_dimensions_and_non_object_input_are_rejected(self) -> None:
        for params in (
            {"width": 0, "height": 1920},
            {"width": 1080, "height": 0},
            {"width": True, "height": 1920},
            [],
        ):
            with self.subTest(params=params):
                with self.assertRaises(CreateDraftValidationError):
                    build_request(params)

    def test_no_transport_never_claims_real_creation(self) -> None:
        with self.assertRaises(CreateDraftTransportRequired):
            run_create_draft(self.sample_input)

    def test_async_entry_point_uses_same_contract(self) -> None:
        result = asyncio.run(main(self.sample_input, transport=self.synthetic_transport))
        self.assertEqual(result, self.sample_expected)


if __name__ == "__main__":
    unittest.main()
