from __future__ import annotations

import json
from pathlib import Path
import unittest

from audit.coze_adapter_video_query import FakeResponse, run_both
from audit.equivalence import compare_outputs
from workflow_1256.video_query import VideoQueryTransportRequired, run_video_query, to_coze_node_output


def query_params(**overrides):
    value = {
        "task_id": "synthetic-task-001",
        "max_wait": 175,
        "check_interval": 10,
        "enable_tos_upload": False,
        "tos_path_prefix": "synthetic-videos",
    }
    value.update(overrides)
    return value


def success_events():
    return [
        FakeResponse({}, 200),
        FakeResponse({"status": "succeeded", "output": [{"video_url": "https://video.example/synthetic.mp4"}]}, 200),
        FakeResponse({}, 200, b"x" * 60000),
    ]


class VideoQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.sample = json.loads((root / "video_query_input.json").read_text(encoding="utf-8"))
        cls.expected = json.loads((root / "video_query_expected.json").read_text(encoding="utf-8"))

    def test_missing_task_id_and_json_string_match_original(self) -> None:
        for params in ({}, "not-json", {"_input": "not-json"}):
            with self.subTest(params=params):
                original, migrated, original_client, migrated_client, _, _ = run_both(params, [])
                self.assertEqual(compare_outputs(original, migrated), [])
                self.assertEqual(original_client.get_calls, [])
                self.assertEqual(migrated_client.get_calls, [])

    def test_processing_success_download_and_declared_projection(self) -> None:
        original, migrated, original_client, migrated_client, original_clock, migrated_clock = run_both(
            query_params(), success_events()
        )
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertTrue(original["success"])
        self.assertEqual(original["public_video_url"], "https://video.example/synthetic.mp4")
        self.assertEqual(original, self.expected)
        self.assertEqual(original_clock.sleeps, [])
        self.assertEqual(migrated_clock.sleeps, [])
        self.assertEqual(len(original_client.get_calls), 3)
        self.assertEqual(to_coze_node_output(original), to_coze_node_output(migrated))
        self.assertEqual(
            list(to_coze_node_output(original)),
            ["errorBody", "file_size_mb", "isSuccess", "msg", "public_video_url", "status", "success", "tip"],
        )

    def test_processing_then_success_preserves_poll_interval(self) -> None:
        events = [
            FakeResponse({}, 200),
            FakeResponse({"status": "running"}, 200),
            FakeResponse({"status": "completed", "data": {"video_url": "https://video.example/a.mp4"}}, 200),
            FakeResponse({}, 200, b"y" * 60000),
        ]
        original, migrated, _, _, original_clock, migrated_clock = run_both(query_params(), events)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertEqual(original_clock.sleeps, [10])
        self.assertEqual(migrated_clock.sleeps, [10])
        self.assertTrue(original["success"])

    def test_failed_task_matches_original(self) -> None:
        events = [
            FakeResponse({}, 200),
            FakeResponse({"status": "failed", "error": {"message": "synthetic failure"}}, 200),
        ]
        original, migrated, _, _, _, _ = run_both(query_params(), events)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertFalse(original["success"])

    def test_auth_failure_matches_original(self) -> None:
        events = [
            FakeResponse({"error": {"code": "Unauthorized", "message": "unauthorized"}}, 401),
            FakeResponse({"error": {"code": "Unauthorized", "message": "unauthorized"}}, 401),
        ]
        original, migrated, _, _, _, _ = run_both(query_params(), events)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertFalse(original["success"])

    def test_timeout_and_small_download_match_original(self) -> None:
        events = [
            FakeResponse({}, 200),
            FakeResponse({"status": "completed", "data": {"video_url": "https://video.example/small.mp4"}}, 200),
            FakeResponse({}, 200, b"too-small"),
        ]
        original, migrated, _, _, _, _ = run_both(query_params(), events)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertFalse(original["success"])

        events = [FakeResponse({}, 200), FakeResponse({"status": "running"}, 200)]
        original, migrated, _, _, _, _ = run_both(query_params(max_wait=0), events)
        self.assertEqual(compare_outputs(original, migrated), [])
        self.assertFalse(original["success"])

    def test_network_is_blocked_without_explicit_transport(self) -> None:
        with self.assertRaises(VideoQueryTransportRequired):
            run_video_query(query_params())


if __name__ == "__main__":
    unittest.main()
