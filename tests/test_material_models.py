from __future__ import annotations

import unittest

from audit.coze_adapter_video_generate import FakeRequests, FakeResponse
from workflow_1256.create_image_task import run_create_image_task
from workflow_1256.material_models import (
    IMAGE_MODEL_API_IDS,
    normalize_image_model,
    normalize_material_model_selection,
    normalize_video_model,
)
from workflow_1256.video_generate import run_video_generate
from workflow_1256.asset_generation_contracts import build_video_request, resolve_camera_fixed


class CaptureImageTransport:
    def __init__(self) -> None:
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return {"data": [{"task_id": "image-task-001"}]}


class MaterialModelSelectionTests(unittest.TestCase):
    def test_camera_policy_uses_short_and_low_dynamic_guards(self) -> None:
        self.assertTrue(resolve_camera_fixed({"duration_ms": 2500, "camera_motion": "跟拍"}))
        self.assertTrue(resolve_camera_fixed({"duration_ms": 4500, "dynamic_level": "low", "camera_motion": "环绕"}))
        self.assertFalse(resolve_camera_fixed({"duration_ms": 4500, "camera_motion": "跟拍"}))
        self.assertTrue(resolve_camera_fixed({"duration_ms": 4500, "camera_fixed": True, "camera_motion": "跟拍"}))
        self.assertFalse(build_video_request({"duration_s": 4.5, "camera_motion": [{"camera_motion": "环绕"}]})["camera_fixed"])

    def test_auto_and_aliases_resolve_to_the_supported_models(self) -> None:
        self.assertEqual(normalize_image_model("auto"), "image2")
        self.assertEqual(normalize_image_model("gpt-image-2"), "image2")
        self.assertEqual(normalize_video_model("Seedance1.5Pro"), "seedance_1_5_pro")
        self.assertEqual(normalize_video_model("Seedance1.0Pro"), "seedance_1_0_pro")
        self.assertEqual(
            normalize_material_model_selection({}),
            {"image_model": "image2", "video_model": "seedance_1_5_pro"},
        )

    def test_invalid_model_is_rejected_before_transport(self) -> None:
        image_transport = CaptureImageTransport()
        image_result = run_create_image_task(
            {"api_key": "synthetic-key", "prompt": "p", "image_model": "unknown"},
            transport=image_transport,
        )
        self.assertTrue(image_result["error"])
        self.assertEqual(image_transport.requests, [])

        video_client = FakeRequests([])
        video_result = run_video_generate(
            {"prompt": "p", "video_model": "unknown", "pre_check_wait": 0},
            requests_client=video_client,
            sleep=lambda _seconds: None,
        )
        self.assertFalse(video_result["success"])
        self.assertEqual(video_result["error_code"], "UNSUPPORTED_MODEL")
        self.assertEqual(video_client.post_calls, [])

    def test_image2_is_mapped_to_the_verified_api_model(self) -> None:
        transport = CaptureImageTransport()
        result = run_create_image_task(
            {
                "api_key": "synthetic-key",
                "prompt": "p",
                "image_model": "image2",
            },
            transport=transport,
        )
        self.assertEqual(result["task_id"], "image-task-001")
        self.assertEqual(transport.requests[0].image_model, "image2")
        self.assertEqual(IMAGE_MODEL_API_IDS[transport.requests[0].image_model], "gpt-image-2")

    def test_seedance_selection_is_optional_and_uses_auth_configured_endpoint(self) -> None:
        client = FakeRequests([FakeResponse({"id": "video-task-001"}, status_code=201)])
        result = run_video_generate(
            {
                "prompt": "p",
                "video_model": "seedance_1_0_pro",
                "pre_check_wait": 0,
            },
            requests_client=client,
            sleep=lambda _seconds: None,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["task_id"], "video-task-001")
        self.assertEqual(len(client.post_calls), 1)
        self.assertIn(client.post_calls[0]["json"]["model"], {
            "ep-20260816000749-hp8mq",
            "ep-20260816001250-lh9k5",
        })


if __name__ == "__main__":
    unittest.main()
