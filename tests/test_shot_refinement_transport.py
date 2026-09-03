from __future__ import annotations

import json
import unittest

from workflow_1256.shot_refinement import ShotRefinementItem, run_shot_refinement
from workflow_1256.shot_refinement_transport import (
    DEFAULT_API_URL,
    DEFAULT_MODEL,
    Seed21TurboShotRefinementTransport,
    ShotRefinementTransportError,
    ShotRefinementTransportConfigError,
    load_system_prompt,
    preferred_shot_count,
)


class ShotRefinementTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.item = ShotRefinementItem(
            duration=8.0,
            item={"visual_core": "状态建立", "visual_story": "任务刚开始"},
            segment="synthetic segment",
            timeline={"start": 0, "end": 8000000},
        )

    def test_missing_key_is_rejected_without_network(self) -> None:
        with self.assertRaises(ShotRefinementTransportConfigError):
            Seed21TurboShotRefinementTransport(api_key="")

    def test_default_prompt_is_the_extracted_197742_prompt(self) -> None:
        prompt = load_system_prompt()
        self.assertIn("镜头精细化导演", prompt)
        self.assertIn("最少镜头数", prompt)
        self.assertIn("不得输出或提前决定", prompt)
        self.assertIn("2--5 秒", prompt)

    def test_preferred_shot_count_uses_two_to_five_second_soft_window(self) -> None:
        self.assertEqual(preferred_shot_count(2.2), (1, 1, 1))
        self.assertEqual(preferred_shot_count(3.0), (1, 1, 1))
        self.assertEqual(preferred_shot_count(6.0), (2, 3, 2))
        self.assertEqual(preferred_shot_count(8.0), (2, 4, 2))
        self.assertEqual(preferred_shot_count(12.0), (3, 6, 3))

    def test_official_ark_payload_uses_seed_21_turbo_and_disabled_thinking(self) -> None:
        calls = []

        def requester(url, headers, payload, timeout):
            calls.append((url, dict(headers), dict(payload), timeout))
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "shots": [
                                        {
                                            "source_text": "任务刚开始",
                                            "clip_role": "状态建立",
                                            "story_beat": "任务已经开始",
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        transport = Seed21TurboShotRefinementTransport(
            api_key="synthetic-key",
            requester=requester,
        )
        result = transport(self.item)

        self.assertEqual(result["shots"][0]["source_text"], "任务刚开始")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], DEFAULT_API_URL)
        self.assertEqual(calls[0][1]["Authorization"], "Bearer synthetic-key")
        self.assertEqual(calls[0][2]["model"], DEFAULT_MODEL)
        self.assertEqual(calls[0][2]["thinking"], {"type": "disabled"})
        self.assertEqual(calls[0][2]["messages"][0]["role"], "system")
        self.assertEqual(calls[0][2]["messages"][1]["role"], "user")
        prompt = calls[0][2]["messages"][1]["content"]
        self.assertIn("OUTPUT_CONSTRAINT: output exactly 2 shots", prompt)
        self.assertIn("PREFERRED_SHOT_DURATION: 2..5 seconds", prompt)

    def test_global_required_count_overrides_per_group_recommendation_within_legal_range(self) -> None:
        item = ShotRefinementItem(
            duration=15.48,
            item={"required_shot_count": 4, "narrative_role": "wrong"},
            segment="需要快节奏的高压反转段",
            timeline={"start": 0, "end": 15_480_000},
        )
        prompt = Seed21TurboShotRefinementTransport._user_prompt(item)
        self.assertIn("output exactly 4 shots", prompt)
        self.assertIn("whole video's 4.5-second average budget", prompt)

    def test_locked_slots_override_model_side_narration_splitting(self) -> None:
        item = ShotRefinementItem(
            duration=8.0,
            item={
                "required_shot_count": 2,
                "locked_shot_slots": [
                    {"narration_text": "先说前半句", "timeline": {"start": 0, "end": 3_000_000}},
                    {"narration_text": "再说后半句", "timeline": {"start": 3_000_000, "end": 8_000_000}},
                ],
            },
            segment="先说前半句。再说后半句。",
            timeline={"start": 0, "end": 8_000_000},
        )
        prompt = Seed21TurboShotRefinementTransport._user_prompt(item)
        self.assertIn("LOCKED_SLOT_PLAN", prompt)
        self.assertIn("NARRATION_AUTHORITY", prompt)
        self.assertIn("not deciding where the shot begins or ends", prompt)

    def test_primary_failure_falls_back_to_backup_auth_in_order(self) -> None:
        calls = []

        def requester(_url, headers, _payload, _timeout):
            calls.append(headers["Authorization"])
            if len(calls) == 1:
                from workflow_1256.shot_refinement_transport import ShotRefinementTransportError

                raise ShotRefinementTransportError("synthetic primary failure")
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"shots":[]}',
                        }
                    }
                ]
            }

        transport = Seed21TurboShotRefinementTransport(
            api_key="primary-key",
            backup_api_key="backup-key",
            max_attempts_per_auth=1,
            requester=requester,
        )
        result = transport(self.item)

        self.assertEqual(result, {"shots": [], "reasoning_content": ""})
        self.assertEqual(calls, ["Bearer primary-key", "Bearer backup-key"])

    def test_response_contract_failure_immediately_falls_back_to_next_model(self) -> None:
        calls = []

        def requester(_url, _headers, payload, _timeout):
            calls.append(payload["model"])
            if payload["model"] == "model-one":
                raise ShotRefinementTransportError("方舟响应缺少 shots 数组")
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"shots":[]}',
                        }
                    }
                ]
            }

        transport = Seed21TurboShotRefinementTransport(
            api_key="primary-key",
            model="model-one",
            fallback_models=("model-two", "model-one"),
            max_attempts_per_auth=3,
            requester=requester,
        )

        result = transport(self.item)

        self.assertEqual(result, {"shots": [], "reasoning_content": ""})
        self.assertEqual(calls, ["model-one", "model-two"])

    def test_transport_can_be_injected_into_existing_batch_contract(self) -> None:
        def requester(_url, _headers, _payload, _timeout):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"shots":[{"source_text":"任务刚开始","clip_role":"状态建立","story_beat":"任务已经开始"}]}'
                        }
                    }
                ]
            }

        transport = Seed21TurboShotRefinementTransport(
            api_key="synthetic-key",
            requester=requester,
        )
        result = run_shot_refinement(
            {
                "duration": [8.0],
                "items": [{"visual_core": "状态建立", "visual_story": "任务刚开始"}],
                "segments": ["synthetic segment"],
                "timelines": [{"start": 0, "end": 8000000}],
            },
            llm_transport=transport,
            code_transport=lambda _item, llm: {
                "shots": [
                    {
                        **llm["shots"][0],
                        "clip_duration": 8.0,
                    }
                ],
                "clip_duration": [8.0],
                "int_duration": [8],
                "timelines": [{"start": 0, "end": 8000000}],
            },
        )
        self.assertEqual(len(result["LLM_list"]), 1)
        self.assertEqual(len(result["Code_list"]), 1)


if __name__ == "__main__":
    unittest.main()
