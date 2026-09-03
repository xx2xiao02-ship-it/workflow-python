from __future__ import annotations

import unittest

from audit.coze_adapter_video_generate import FakeRequests, FakeResponse, run_both
from audit.equivalence import compare_outputs
from workflow_1256.video_generate import (
    VideoGenerateTransportRequired,
    run_video_generate,
    to_coze_node_output,
)


def valid_params(**overrides):
    value = {
        "prompt": "synthetic video prompt",
        "duration": 7,
        "first_frame_url": "",
        "generate_audio": False,
        "last_frame_url": "",
        "ratio": "9:16",
        "resolution": "480p",
        "watermark": False,
        "pre_check_wait": 0,
        "seed": -1,
        # 旧节点等价性断言显式固定 1.0；auto 的新行为由独立测试覆盖。
        "video_model": "seedance_1_0_pro",
    }
    value.update(overrides)
    return value


class VideoGenerateTests(unittest.TestCase):
    def test_missing_input_matches_original(self) -> None:
        original, migrated, original_client, migrated_client = run_both({}, [])
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertFalse(original["success"])
        self.assertEqual(original_client.post_calls, [])
        self.assertEqual(migrated_client.post_calls, [])

    def test_success_request_payload_and_declared_projection(self) -> None:
        event = FakeResponse({"id": "synthetic-video-task-001"}, status_code=201)
        original, migrated, original_client, migrated_client = run_both(valid_params(), [event])
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertTrue(original["success"])
        self.assertEqual(original["task_id"], "synthetic-video-task-001")
        self.assertEqual(len(original_client.post_calls), 1)
        payload = original_client.post_calls[0]["json"]
        self.assertEqual(payload["duration"], 7)
        self.assertEqual(payload["generate_audio"], False)
        self.assertEqual(payload["watermark"], False)
        self.assertEqual(payload["ratio"], "9:16")
        self.assertEqual(payload["resolution"], "480p")
        self.assertEqual(
            list(to_coze_node_output(original)),
            ["msg", "success", "task_id", "tip"],
        )
        self.assertEqual(to_coze_node_output(original), to_coze_node_output(migrated))

    def test_tos_owned_frames_are_forwarded_and_ratio_is_omitted(self) -> None:
        params = valid_params(
            first_frame_url="https://temp-video-seedance15p2.tos-cn-beijing.volces.com/a.png",
            last_frame_url="https://temp-video-seedance15p2.tos-cn-beijing.volces.com/b.png",
        )
        event = FakeResponse({"task_id": "synthetic-frame-task"}, status_code=200)
        original, migrated, original_client, _ = run_both(params, [event])
        self.assertEqual(compare_outputs(original, migrated), [])
        payload = original_client.post_calls[0]["json"]
        self.assertNotIn("ratio", payload)
        self.assertNotIn("resolution", payload)
        self.assertEqual(
            [item["role"] for item in payload["content"] if "role" in item],
            ["first_frame", "last_frame"],
        )

    def test_http_parameter_failure_matches_original(self) -> None:
        event = FakeResponse(
            {"error": {"code": "InvalidParameter", "message": "bad duration", "param": "duration"}},
            status_code=400,
        )
        original, migrated, _, _ = run_both(valid_params(), [event])
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertFalse(original["success"])

    def test_account_limit_switches_to_backup_auth(self) -> None:
        params = valid_params(pre_check_wait=0)
        client = FakeRequests([
            FakeResponse(
                {
                    "error": {
                        "code": "AccountInferenceLimitExceeded",
                        "message": "Your account has reached the set inference limit",
                    }
                },
                status_code=429,
            ),
            FakeResponse({"id": "backup-video-task"}, status_code=201),
        ])

        result = run_video_generate(params, requests_client=client, sleep=lambda _seconds: None)

        self.assertTrue(result["success"])
        self.assertEqual(result["task_id"], "backup-video-task")
        self.assertEqual(len(client.post_calls), 2)

    def test_auto_tries_all_1_5_auths_before_falling_back_to_1_0(self) -> None:
        from unittest.mock import patch
        from workflow_1256 import video_generate

        configs = [
            {
                "name": f"账号{i}",
                "api_key": f"key-{i}",
                "model_ep_1_5": f"model-1.5-{i}",
                "model_ep_1_0": f"model-1.0-{i}",
            }
            for i in range(1, 5)
        ]
        client = FakeRequests([
            FakeResponse({"error": {"code": "QuotaExceeded", "message": "quota exhausted"}}, status_code=429),
            FakeResponse({"error": {"code": "QuotaExceeded", "message": "quota exhausted"}}, status_code=429),
            FakeResponse({"error": {"code": "QuotaExceeded", "message": "quota exhausted"}}, status_code=429),
            FakeResponse({"id": "fallback-video-task"}, status_code=201),
        ])

        with patch.object(video_generate, "AUTH_CONFIGS", configs):
            result = run_video_generate(
                valid_params(video_model="auto"),
                requests_client=client,
                sleep=lambda _seconds: None,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["task_id"], "fallback-video-task")
        self.assertEqual(
            [call["json"]["model"] for call in client.post_calls],
            ["model-1.5-1", "model-1.5-2", "model-1.5-3", "model-1.5-4"],
        )

    def test_auto_falls_back_after_all_four_1_5_auths_fail(self) -> None:
        from unittest.mock import patch
        from workflow_1256 import video_generate

        configs = [
            {
                "name": f"账号{i}",
                "api_key": f"key-{i}",
                "model_ep_1_5": f"model-1.5-{i}",
                "model_ep_1_0": f"model-1.0-{i}",
            }
            for i in range(1, 5)
        ]
        events = [
            FakeResponse({"error": {"code": "QuotaExceeded", "message": "quota exhausted"}}, status_code=429)
            for _ in range(4)
        ] + [FakeResponse({"id": "fallback-video-task"}, status_code=201)]
        client = FakeRequests(events)

        with patch.object(video_generate, "AUTH_CONFIGS", configs):
            result = run_video_generate(
                valid_params(video_model="auto"),
                requests_client=client,
                sleep=lambda _seconds: None,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["task_id"], "fallback-video-task")
        self.assertEqual(
            [call["json"]["model"] for call in client.post_calls],
            [
                "model-1.5-1", "model-1.5-2", "model-1.5-3", "model-1.5-4",
                "model-1.0-1",
            ],
        )

    def test_timeout_retry_matches_original(self) -> None:
        events = [FakeRequests.exceptions.Timeout("synthetic timeout")] * 2
        original, migrated, _, _ = run_both(valid_params(), events)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertFalse(original["success"])

    def test_json_string_and_default_boundary_behavior(self) -> None:
        for params in ("not-json", {"_input": "not-json"}, {"prompt": ""}):
            with self.subTest(params=params):
                original, migrated, _, _ = run_both(params, [])
                self.assertEqual(compare_outputs(original, migrated), [])

    def test_network_is_blocked_without_explicit_transport(self) -> None:
        with self.assertRaises(VideoGenerateTransportRequired):
            run_video_generate(valid_params())


if __name__ == "__main__":
    unittest.main()
