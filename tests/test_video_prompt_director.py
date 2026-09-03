from __future__ import annotations

import json
import unittest
from pathlib import Path

from audit.equivalence import compare_outputs
from audit.equivalence_video_prompt_director import audit_reference_response
from workflow_1256.video_prompt_director import (
    VideoPromptDirectorRequest,
    VideoPromptDirectorTransportRequired,
    VideoPromptDirectorValidationError,
    build_request,
    normalize_response,
    render_user_prompt,
    run_video_prompt_batch,
    run_video_prompt_director,
)


class FixedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: VideoPromptDirectorRequest):
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("synthetic response exhausted")
        item = self.responses.pop(0)
        return item(request) if callable(item) else item


class VideoPromptDirectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.input = json.loads((root / "video_prompt_director_input.json").read_text(encoding="utf-8"))
        cls.response = json.loads((root / "video_prompt_director_response.json").read_text(encoding="utf-8"))
        cls.expected = json.loads((root / "video_prompt_director_expected.json").read_text(encoding="utf-8"))

    def test_request_contract_and_prompt_template(self) -> None:
        request = build_request({
            "items": self.input["items"][0],
            "ref_image": self.input["ref_image"][0]["ref_image"],
            "motion_seed": self.input["motion_seed"][0],
            "clip_duration": self.input["clip_duration"][0],
        })
        self.assertIsInstance(request, VideoPromptDirectorRequest)
        prompt = render_user_prompt(request)
        self.assertIn("建立AI协作场景", prompt)
        self.assertIn("动作一", prompt)
        self.assertIn("5.0", prompt)

    def test_fixed_response_normalization_and_field_order(self) -> None:
        request_params = {
            "items": self.input["items"][0],
            "ref_image": self.input["ref_image"][0]["ref_image"],
            "motion_seed": self.input["motion_seed"][0],
            "clip_duration": self.input["clip_duration"][0],
        }
        actual = run_video_prompt_director(
            request_params,
            transport=FixedTransport([self.response]),
        )
        self.assertEqual(actual, self.expected)
        self.assertEqual(list(actual), ["plans", "reasoning_content", "visual_lock"])
        audit = audit_reference_response(self.expected, actual)
        self.assertTrue(audit["passed"], audit["mismatches"])
        self.assertFalse(audit["code_level_equivalent"])

    def test_batch_preserves_group_and_plan_order(self) -> None:
        response_one = dict(self.response)
        response_one["plans"] = self.response["plans"][:2]
        response_two = dict(self.response)
        response_two["plans"] = self.response["plans"][1:]
        transport = FixedTransport([response_one, response_two])
        actual = run_video_prompt_batch(self.input, transport=transport)
        self.assertEqual(list(actual), ["plan_out_list"])
        self.assertEqual(len(actual["plan_out_list"]), 2)
        self.assertEqual(len(actual["plan_out_list"][0]["plans"]), 2)
        self.assertEqual(len(actual["plan_out_list"][1]["plans"]), 1)
        self.assertEqual(len(transport.requests[0].ref_image), 2)
        self.assertEqual(len(transport.requests[1].ref_image), 1)

    def test_json_wrappers_and_boundary_rejections(self) -> None:
        encoded = json.dumps({
            "items": {"visual_core": "c"},
            "ref_image": ["frame"],
            "motion_seed": {"motion_seed": ["seed"]},
            "clip_duration": {"clip_duration": [2]},
        }, ensure_ascii=False)
        request = build_request(encoded)
        self.assertEqual(request.ref_image, ["frame"])

        for params in (
            {"items": {}, "ref_image": [1], "motion_seed": {"motion_seed": []}, "clip_duration": {"clip_duration": []}},
            {"items": {}, "ref_image": [], "motion_seed": {}, "clip_duration": {"clip_duration": [True]}},
        ):
            with self.subTest(params=params):
                with self.assertRaises(VideoPromptDirectorValidationError):
                    build_request(params)

    def test_response_missing_field_and_no_transport(self) -> None:
        with self.assertRaises(VideoPromptDirectorValidationError):
            normalize_response({
                "visual_lock": "lock",
                "plans": [{"shot_index": 0, "duration": 2, "stages": [{"action": "a", "camera_motion": "固定机位"}]}],
                "reasoning_content": "r",
            })
        with self.assertRaises(VideoPromptDirectorTransportRequired):
            run_video_prompt_director({
                "items": self.input["items"][0],
                "ref_image": self.input["ref_image"][0]["ref_image"],
                "motion_seed": self.input["motion_seed"][0],
                "clip_duration": self.input["clip_duration"][0],
            })

    def test_real_page_shape_fixture_keeps_nine_groups_and_seventeen_plans(self) -> None:
        root = Path(__file__).parents[1] / "samples" / "real"
        params = json.loads(
            (root / "run-7668196429877870619-170263-structural-input.json").read_text(
                encoding="utf-8"
            )
        )
        shot_counts = [len(group["clip_duration"]) for group in params["Code_list"]]
        batch_params = {
            "items": params["items"],
            "motion_seed": [],
            "ref_image": [],
            "clip_duration": params["Code_list"],
        }
        frame_cursor = 0
        seed_cursor = 0
        for count in shot_counts:
            batch_params["motion_seed"].append({
                "motion_seed": params["motion_seed"][seed_cursor:seed_cursor + count]
            })
            seed_cursor += count
            frames = params["image_url_list"][frame_cursor:frame_cursor + count]
            frame_cursor += count
            batch_params["ref_image"].append({"ref_image": frames})

        def response_for(request: VideoPromptDirectorRequest):
            durations = request.clip_duration["clip_duration"]
            return {
                "visual_lock": "redacted-real-visual-lock",
                "reasoning_content": "redacted-real-reasoning",
                "plans": [
                    {
                        "shot_index": index,
                        "duration": int(duration),
                        "stages": [
                            {
                                "time_range": "0.0-1.0s",
                                "action": "redacted-real-action",
                                "camera_motion": "固定机位",
                            }
                        ],
                    }
                    for index, duration in enumerate(durations)
                ],
            }

        transport = FixedTransport([response_for] * 9)
        result = run_video_prompt_batch(batch_params, transport=transport)
        self.assertEqual(len(result["plan_out_list"]), 9)
        self.assertEqual(
            [len(item["plans"]) for item in result["plan_out_list"]],
            [3, 2, 3, 2, 2, 2, 1, 1, 1],
        )


if __name__ == "__main__":
    unittest.main()
