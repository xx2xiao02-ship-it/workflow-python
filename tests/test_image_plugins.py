from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from audit.coze_adapter_image_plugins import (
    FakeClock,
    make_http_error,
    run_migrated_create,
    run_migrated_poll,
    run_original_create,
    run_original_poll,
)
from audit.equivalence_image_plugins import (
    audit_create_case,
    audit_poll_case,
    compare_outcomes,
    summarize,
)
from workflow_1256.create_image_task import (
    CreateImageTaskRequest,
    CreateImageTaskTransportRequired,
    build_request as build_create_request,
    run_create_image_task,
    to_coze_node_output as create_coze_output,
)
from workflow_1256.get_task_result import (
    ActiveCrashError,
    GetTaskResultTransportRequired,
    build_request as build_poll_request,
    run_get_task_result,
    to_coze_node_output as poll_coze_output,
)


SAMPLE_DIR = Path(__file__).parents[1] / "samples" / "synthetic"


def processing_result(task_id: str = "synthetic-task-001") -> dict:
    return {"data": [{"task_id": task_id}]}


def completed_result(url: str = "synthetic://image/001") -> dict:
    return {"data": {"status": "completed", "result": {"images": [{"url": [url]}]}}}


def processing_poll_result() -> dict:
    return {"data": {"status": "processing"}}


class ImagePluginAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sample = json.loads(
            (SAMPLE_DIR / "image_plugins_input.json").read_text(encoding="utf-8")
        )
        cls.expected = json.loads(
            (SAMPLE_DIR / "image_plugins_expected.json").read_text(encoding="utf-8")
        )

    def test_create_synthetic_output_request_and_field_order(self) -> None:
        result = audit_create_case(
            "synthetic_create",
            self.sample["create_image_task"],
            [processing_result()],
        )
        self.assertTrue(result["passed"], result["mismatches"])
        self.assertEqual(result["original"].value, self.expected["create_image_task"])

        original, opener = run_original_create(
            self.sample["create_image_task"], [processing_result()]
        )
        self.assertFalse(original.raised)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0].full_url, "https://api.aishuch.com/v1/images/generations")
        payload = json.loads(opener.calls[0].data.decode("utf-8"))
        self.assertEqual(
            list(payload),
            ["model", "prompt", "n", "size", "resolution", "official_fallback", "image_urls"],
        )
        self.assertEqual(payload["model"], "gpt-image-2")
        self.assertEqual(payload["size"], "9:16")
        self.assertEqual(payload["image_urls"], ["synthetic://reference/1"])

        self.assertEqual(
            create_coze_output(result["original"].value),
            {
                "code": 202,
                "image_url": None,
                "message": "任务创建成功！已转交下游处理",
                "status": "processing",
                "task_id": "synthetic-task-001",
            },
        )

    def test_create_defaults_and_json_string_are_original_behavior(self) -> None:
        params = {"api_key": "synthetic-key", "prompt": "p"}
        original, _ = run_original_create(params, [processing_result()])
        migrated, transport = run_migrated_create(params, [processing_result()])
        self.assertFalse(compare_outcomes(original, migrated), (original, migrated))
        self.assertEqual(transport.calls[0].n, 1)
        self.assertEqual(transport.calls[0].size, "auto")
        self.assertEqual(transport.calls[0].resolution, "1k")
        self.assertEqual(transport.calls[0].image_urls, [])

        encoded = json.dumps(params)
        self.assertEqual(
            run_original_create(encoded, [processing_result()])[0].value,
            {"error": True, "message": "缺失必填参数: api_key"},
        )
        self.assertEqual(
            run_create_image_task(encoded, transport=lambda _: processing_result()),
            {"error": True, "message": "缺失必填参数: api_key"},
        )

    def test_create_missing_fields_and_n_digit_rule(self) -> None:
        for params, expected in (
            ({}, {"error": True, "message": "缺失必填参数: api_key"}),
            ({"api_key": "k"}, {"error": True, "message": "提示词不能为空"}),
            ({"api_key": "k", "prompt": "p", "n": "003"}, None),
            ({"api_key": "k", "prompt": "p", "n": "2.0"}, None),
        ):
            with self.subTest(params=params):
                original, _ = run_original_create(params, [processing_result()])
                migrated, _ = run_migrated_create(params, [processing_result()])
                self.assertFalse(compare_outcomes(original, migrated))
                if expected is not None:
                    self.assertEqual(original.value, expected)

        result, transport = run_migrated_create(
            {"api_key": "k", "prompt": "p", "n": "003"},
            [processing_result()],
        )
        self.assertEqual(result.value["task_id"], "synthetic-task-001")
        self.assertEqual(transport.calls[0].n, 3)

    def test_create_empty_task_id_retries_then_success(self) -> None:
        events = [{"data": []}, processing_result("task-after-retry")]
        original, _ = run_original_create({"api_key": "k", "prompt": "p"}, events)
        sleeps: list[float] = []
        migrated, transport = run_migrated_create(
            {"api_key": "k", "prompt": "p"}, events, sleeps=sleeps
        )
        self.assertFalse(compare_outcomes(original, migrated))
        self.assertEqual(migrated.value["attempted"], 2)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(len(transport.calls), 2)

    def test_create_http_fatal_and_retryable_errors(self) -> None:
        for code in (400, 401, 403, 404):
            with self.subTest(code=code):
                result = audit_create_case(
                    f"create_http_{code}",
                    {"api_key": "k", "prompt": "p"},
                    [make_http_error(code, f"body-{code}")],
                )
                self.assertTrue(result["passed"], result["mismatches"])

        result = audit_create_case(
            "create_429_then_success",
            {"api_key": "k", "prompt": "p"},
            [make_http_error(429), processing_result("retry-success")],
        )
        self.assertTrue(result["passed"], result["mismatches"])

        result = audit_create_case(
            "create_500_final",
            {"api_key": "k", "prompt": "p"},
            [make_http_error(500), make_http_error(500), make_http_error(500)],
        )
        self.assertTrue(result["passed"], result["mismatches"])

    def test_create_invalid_json_and_network_exception(self) -> None:
        invalid_json = json.JSONDecodeError("invalid", "{", 1)
        result = audit_create_case(
            "create_invalid_json",
            {"api_key": "k", "prompt": "p"},
            [invalid_json, invalid_json, invalid_json],
        )
        self.assertTrue(result["passed"], result["mismatches"])

        result = audit_create_case(
            "create_network_failure",
            {"api_key": "k", "prompt": "p"},
            [TimeoutError("synthetic timeout")] * 3,
        )
        self.assertTrue(result["passed"], result["mismatches"])

    def test_poll_synthetic_processing_then_completed(self) -> None:
        result = audit_poll_case(
            "synthetic_poll",
            self.sample["get_task_result"],
            [processing_poll_result(), completed_result()],
        )
        self.assertTrue(result["passed"], result["mismatches"])
        self.assertEqual(result["original"].value, self.expected["get_task_result"])

        original, _, original_clock = run_original_poll(
            self.sample["get_task_result"], [processing_poll_result(), completed_result()]
        )
        migrated, _, migrated_clock = run_migrated_poll(
            self.sample["get_task_result"], [processing_poll_result(), completed_result()]
        )
        self.assertFalse(compare_outcomes(original, migrated))
        self.assertEqual(original_clock.sleeps, [30.0])
        self.assertEqual(migrated_clock.sleeps, [30.0])
        self.assertEqual(
            poll_coze_output(result["original"].value),
            {
                "code": 200,
                "errorBody": None,
                "image_url": "synthetic://image/001",
                "isSuccess": True,
                "message": None,
                "status": "completed",
            },
        )

    def test_poll_missing_fields_json_string_and_failed(self) -> None:
        for params, expected in (
            ({}, {"error": True, "message": "缺失 task_id"}),
            ({"task_id": "t"}, {"error": True, "message": "缺失 api_key"}),
            (json.dumps({"task_id": "t", "api_key": "k"}), {"error": True, "message": "缺失 task_id"}),
        ):
            with self.subTest(params=params):
                result = audit_poll_case("poll_invalid_input", params, [])
                self.assertTrue(result["passed"], result["mismatches"])
                self.assertEqual(result["original"].value, expected)

        result = audit_poll_case(
            "poll_failed",
            {"task_id": "t", "api_key": "k"},
            [{"data": {"status": "failed", "error": {"message": "synthetic failure"}}}],
        )
        self.assertTrue(result["passed"], result["mismatches"])

    def test_poll_missing_image_path_returns_empty_url(self) -> None:
        result = audit_poll_case(
            "poll_completed_missing_image",
            {"task_id": "t", "api_key": "k"},
            [{"data": {"status": "completed", "result": {}}}],
        )
        self.assertTrue(result["passed"], result["mismatches"])
        self.assertEqual(result["original"].value["image_url"], "")

    def test_poll_http_error_and_invalid_json(self) -> None:
        result = audit_poll_case(
            "poll_http_401",
            {"task_id": "t", "api_key": "k"},
            [make_http_error(401, "unauthorized")],
        )
        self.assertTrue(result["passed"], result["mismatches"])

        invalid_json = json.JSONDecodeError("invalid", "{", 1)
        result = audit_poll_case(
            "poll_invalid_json",
            {"task_id": "t", "api_key": "k"},
            [invalid_json],
        )
        self.assertTrue(result["passed"], result["mismatches"])
        self.assertTrue(result["original"].raised)
        self.assertEqual(result["original"].exception_type, "ActiveCrashError")

    def test_poll_absolute_deadline_after_final_processing_query(self) -> None:
        events = [processing_poll_result()] * 7
        result = audit_poll_case(
            "poll_172_second_fuse",
            {"task_id": "t", "api_key": "k"},
            events,
        )
        self.assertTrue(result["passed"], result["mismatches"])
        self.assertEqual(result["original"].exception_type, "ActiveCrashError")
        self.assertIn("最后一次查询仍未完成", result["original"].exception_message or "")

    def test_no_transport_protects_external_services(self) -> None:
        with self.assertRaises(CreateImageTaskTransportRequired):
            run_create_image_task(self.sample["create_image_task"])
        with self.assertRaises(GetTaskResultTransportRequired):
            run_get_task_result(self.sample["get_task_result"])

    def test_build_request_preserves_input_shapes(self) -> None:
        create_request = build_create_request(
            SimpleNamespace(api_key="k", prompt="p", size="9:16")
        )
        self.assertIsInstance(create_request, CreateImageTaskRequest)
        poll_request = build_poll_request(SimpleNamespace(task_id="t", api_key="k"))
        self.assertEqual(poll_request.task_id, "t")
        self.assertEqual(poll_request.api_key, "Bearer k")


if __name__ == "__main__":
    unittest.main()
