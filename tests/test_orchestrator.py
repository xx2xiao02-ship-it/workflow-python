from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_1256.orchestrator import run_daily_update_te


TEST_AUDIO_PATH = str(Path(__file__).resolve())


class FakeCapCut:
    def __init__(self):
        self.calls = []

    def create_draft(self, request):
        return {"draft_url": "synthetic://draft/one", "tip_url": "synthetic://tip"}

    def call(self, endpoint, payload):
        self.calls.append((endpoint, dict(payload)))
        if endpoint == "audio_timelines":
            return fake_timelines(payload)
        if endpoint == "audio_infos":
            return {"infos": "[{\"audio_url\":\"https://synthetic.example/audio.mp3\",\"start\":0,\"end\":2000000}]"}
        if endpoint == "caption_infos":
            return {"infos": "[{\"text\":\"text\",\"start\":0,\"end\":2000000}]"}
        if endpoint == "add_audios":
            return {"draft_url": "synthetic://draft/audio", "track_id": "track-a", "audio_ids": ["audio-1"]}
        if endpoint == "add_captions":
            return {
                "draft_url": "synthetic://draft/caption",
                "track_id": "track-c",
                "text_ids": ["text-1"],
                "segment_ids": ["segment-c"],
                "segment_infos": [{"id": "segment-c", "start": 0, "end": 2_000_000}],
            }
        raise AssertionError(endpoint)

    def add_audios(self, draft_url, audio_infos, _normalized=None):
        return self.call("add_audios", {"draft_url": draft_url, "audio_infos": audio_infos})

    def add_captions(self, draft_url, captions, **kwargs):
        return self.call("add_captions", {"draft_url": draft_url, "captions": captions, **kwargs})


class FakeCapCutWithTimelines(FakeCapCut):
    def call(self, endpoint, payload):
        if endpoint == "audio_timelines":
            assert payload == {"links": [TEST_AUDIO_PATH]}
        return super().call(endpoint, payload)


class RecordingDraftCapCut(FakeCapCut):
    def create_draft(self, request):
        self.calls.append(("create_draft", {
            "width": request.width,
            "height": request.height,
        }))
        return super().create_draft(request)


def fake_visual_intent(_params):
    return {
        "items": [{"visual_core": "core", "visual_story": "story"}],
        "music_cues": [],
        "reasoning_content": "reason",
    }


def fake_visual_intent_with_bgm(_params):
    return {
        "items": [{"visual_core": "core", "visual_story": "story"}],
        "music_cues": [{
            "emotion": "reflective",
            "energy": 3,
            "music_direction": "steady",
            "music_role": "推进",
            "transition_to_next": "crossfade",
        }],
        "reasoning_content": "reason",
    }


def fake_bgm_generation(_params):
    return {
        "code": 0,
        "data": {"SongDetail": {"AudioUrl": "https://synthetic.example/bgm.mp3"}},
    }


def fake_bgm_merge(params):
    return {
        "audio_url": "https://synthetic.example/bgm-merged.mp3",
        "audio_url_list": list(params["audio_urls"]),
        "duration": 2_000_000,
    }


def fake_speech(params):
    return {
        "code": 0,
        "data": {"link": TEST_AUDIO_PATH, "duration": 2.0},
        "log_id": "log",
        "msg": "ok",
    }


def fake_timelines(_params):
    timelines = [{"start": 0, "end": 2_000_000}]
    return {"timelines": timelines, "all_timelines": timelines}


class FakeSTT:
    def transcribe_groups(self, groups):
        captions = []
        timelines = []
        cursor = 0
        for group in groups:
            assert "start_us" not in group
            assert "end_us" not in group
            start = cursor
            end = start + 2_000_000
            cursor = end
            timelines.append({"start": start, "end": end})
            captions.append({
                "caption_id": f"{group['group_id']}.stt.001",
                "group_id": group["group_id"],
                "text": group["reference_text"],
                "start_us": start,
                "end_us": end,
                "reference_text": group["reference_text"],
                "source_node": "capcut_stt",
                "words": [],
            })
        return captions, {
            "status": "succeeded",
            "provider": "fake-capcut-stt",
            "group_count": len(groups),
            "caption_count": len(captions),
            "tasks": [],
            "group_timelines": timelines,
            "total_timeline": {"start": 0, "end": cursor},
            "timeline_unit": "microseconds",
        }


class FakeCorrection:
    def correct(self, captions):
        return [dict(item) for item in captions], {
            "status": "succeeded",
            "provider": "fake-mini",
            "caption_count": len(captions),
        }


def fake_shot_director(params):
    return {
        "shots": [{
            "source_text": params["segments"],
            "clip_role": "观点落点",
            "story_beat": "建立",
        }],
        "reasoning_content": "reason",
    }


def fake_shot_visual(params):
    timeline = params["Code_list"][0]["timelines"][0]
    return {
        "prompt": ["prompt"],
        "ref_image": [{"ref_image": list(params["ref_image"])}],
        "motion_seed": ["seed"],
        "timelines": [timeline],
        "int_duration": [2],
        "error": "",
        "debug": {},
    }


def fake_host_selector(params):
    candidates = params["host_llm_input"]["candidates"]
    return {
        "host_idxs": [candidates[0]["idx"]] if candidates else [],
        "reasoning_content": "reason",
    }


def fake_directors(request):
    return {
        "director_plan": {
            "arc": ["hook"],
            "core": "core",
            "director_type": "view_dir",
            "emo": "emo",
            "expression_domains": ["work"],
            "goal": "goal",
            "open": "open",
            "rule": "rule",
            "spine": "spine",
            "tone": "tone",
            "variation_focus": ["viewpoint"],
        },
        "ok": True,
        "segment_beats": [
            {
                "beats": [
                    {"expression_need": "reality", "relation": "new", "route_candidates": ["scene"]}
                ],
                "rhythm": "hook",
                "segment_goal": "goal",
                "segment_index": 0,
                "segment_text": "text",
            }
        ],
        "segments": ["text"],
    }


def fake_story_transport(_system_prompt, _user_prompt):
    return {
        "movie_outline": {
            "protagonist": "test protagonist",
            "protagonist_goal": "test goal",
            "central_conflict": "test conflict",
            "character_arc": "test arc",
            "ending_hook": "test hook",
        },
        "source_semantic_anchor": {
            "core_thesis": "测试观点",
            "old_system": "旧方法难以覆盖新变化",
            "new_capability": "新工具帮助重组信息",
            "governance_choice": "围绕真实需求调整表达",
            "systemic_cost": "效率提升伴随新的资源成本",
        },
        "story_anchor": {
            "premise": "测试主角在截止前完成关键交付",
            "protagonist_want": "按时交付并理解真实需求",
            "opposing_force": "时间压力和旧经验同时阻碍他",
            "stakes_if_fail": "团队会错过这次机会",
            "point_of_no_return": "倒计时进入最后阶段",
            "decisive_choice": "主角用新工具重组杂乱信息",
            "transformation": "从依赖旧经验转向理解真实需求",
            "final_image_hook": "远处机房灯光仍然亮着",
        },
        "supporting_characters": [
            {
                "name": "协作同事",
                "role": "交付催促者",
                "own_goal": "按时拿到可用结果",
                "relationship_to_protagonist": "协作伙伴",
                "conflict_or_alliance": "催促让主角不得不改变方法",
                "decisive_interaction": "递来最后一份关键资料",
            },
            {
                "name": "客户",
                "role": "需求参照者",
                "own_goal": "快速解决真实问题",
                "relationship_to_protagonist": "服务对象",
                "conflict_or_alliance": "需求打破主角的专业自嗨",
                "decisive_interaction": "留下一个未解决的问题",
            },
        ],
        "conflict_chain": {
            "external": "交付倒计时不断逼近",
            "relationship": "同事催促与主角迟疑交织",
            "internal": "主角不敢放下旧方法",
            "escalation": "资料越堆越高，时间越来越少",
        },
        "silent_story_text": "test silent story",
        "scene_groups": [{
            "scene_id": "scene_01",
            "group_ids": ["g01"],
            "scene_purpose": "STORY_TO_FIRST_FRAME_SCENE",
            "state_before": "before",
            "visible_conflict": "STORY_TO_FIRST_FRAME_CONFLICT",
            "turn": "turn",
            "state_after": "after",
        }],
        "narration_mappings": [{
            "group_id": "g01",
            "scene_id": "scene_01",
            "source_claim": "测试观点",
            "story_range": "截止前的资料整理",
            "trailer_highlight": "倒计时前的最后一次整理",
            "semantic_mapping": "STORY_TO_FIRST_FRAME_MAPPING",
            "conflict": "时间压力迫使主角改变",
            "choice_or_action": "主角重组资料",
            "consequence": "出现可交付的秩序",
            "trailer_hook": "还有一页资料未被解决",
            "silent_action": "STORY_TO_FIRST_FRAME_ACTION",
            "metaphor": "STORY_TO_FIRST_FRAME_METAPHOR",
            "state_before": "before",
            "state_after": "after",
            "bridge_to_next": "bridge",
        }],
    }


def fake_shot_visual_with_story(params):
    assert params["story_context"]["narration_mappings"][0]["semantic_mapping"] == "STORY_TO_FIRST_FRAME_MAPPING"
    assert params["aspect_ratio"] == "16:9"
    result = fake_shot_visual(params)
    result["prompt"] = ["STORY_TO_FIRST_FRAME_PROMPT"]
    result["story_context"] = [{
        "scene_purpose": "STORY_TO_FIRST_FRAME_SCENE",
        "visible_conflict": "STORY_TO_FIRST_FRAME_CONFLICT",
        "silent_action": "STORY_TO_FIRST_FRAME_ACTION",
        "metaphor": "STORY_TO_FIRST_FRAME_METAPHOR",
        "state_before": "before",
        "turn": "turn",
        "state_after": "after",
        "bridge_to_next": "bridge",
    }]
    return result


class OrchestratorTests(unittest.TestCase):
    def test_director_aspect_ratio_is_normalized_before_draft_and_context(self) -> None:
        client = RecordingDraftCapCut()
        result = run_daily_update_te(
            {"text": "text", "aspect_ratio": "16:9"},
            capcut_client=client,
            directors_transport=fake_directors,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(client.calls[0], ("create_draft", {"height": 1080, "width": 1920}))
        self.assertEqual(result.outputs["canvas"]["aspect_ratio"], "16:9")

    def test_tts_and_bgm_chain_preserves_order_and_latest_draft_url(self) -> None:
        client = FakeCapCut()
        result = run_daily_update_te(
            {"text": "text"},
            capcut_client=client,
            directors_transport=fake_directors,
            node_runners={
                "102833": fake_visual_intent_with_bgm,
                "159953": fake_speech,
                "165901": fake_timelines,
                "capcut_stt": FakeSTT(),
                "subtitle_correction": FakeCorrection(),
                "186546": fake_bgm_generation,
                "165818": fake_bgm_merge,
            },
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocked_nodes[0]["node_id"], "197742")
        self.assertEqual(
            result.completed_nodes,
            [
                "182422", "129109", "102833", "159953", "178142", "165901", "105880",
                "192311", "141288", "186546", "165818", "191683", "111882",
                "116592", "135313", "110576", "179989",
            ],
        )
        writes = [
            payload["draft_url"]
            for endpoint, payload in client.calls
            if endpoint in {"add_audios", "add_captions"}
        ]
        self.assertEqual(
            writes,
            ["synthetic://draft/one", "synthetic://draft/audio", "synthetic://draft/audio"],
        )
        self.assertEqual(result.outputs["165818"]["audio_url_list"], ["https://synthetic.example/bgm.mp3"])

    def test_bgm_merge_missing_blocks_before_audio_track_writes(self) -> None:
        client = FakeCapCut()
        result = run_daily_update_te(
            {"text": "text"},
            capcut_client=client,
            directors_transport=fake_directors,
            node_runners={
                "102833": fake_visual_intent_with_bgm,
                "159953": fake_speech,
                "165901": fake_timelines,
                "capcut_stt": FakeSTT(),
                "subtitle_correction": FakeCorrection(),
                "186546": fake_bgm_generation,
            },
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocked_nodes[0]["node_id"], "165818")
        self.assertEqual([endpoint for endpoint, _payload in client.calls], [])

    def test_runs_draft_and_directors_then_reports_real_blockers(self) -> None:
        result = run_daily_update_te(
            {"text": "text"},
            capcut_client=FakeCapCut(),
            directors_transport=fake_directors,
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.completed_nodes, ["182422", "129109"])
        self.assertEqual([item["node_id"] for item in result.blocked_nodes], ["102833", "159953"])
        self.assertEqual(result.outputs["182422"]["draft_url"], "synthetic://draft/one")
        self.assertFalse(result.ok)

    def test_injected_branch_runners_progress_to_first_missing_internal_transport(self) -> None:
        result = run_daily_update_te(
            {"text": "text"},
            capcut_client=FakeCapCut(),
            directors_transport=fake_directors,
            node_runners={
                "102833": fake_visual_intent,
                "159953": fake_speech,
                "165901": fake_timelines,
                "capcut_stt": FakeSTT(),
                "subtitle_correction": FakeCorrection(),
            },
        )

        self.assertEqual(
            result.completed_nodes,
            [
                "182422", "129109", "102833", "159953", "178142", "165901", "105880",
                "192311", "141288", "191683", "111882", "135313", "179989",
            ],
        )
        self.assertEqual(
            [item["node_id"] for item in result.blocked_nodes], ["197742", "103964"]
        )
        self.assertEqual(result.outputs["178142"]["link_list"], [TEST_AUDIO_PATH])
        self.assertEqual(result.outputs["105880"]["subtitle_source"], "capcut_stt_doubao_mini")
        self.assertEqual(result.outputs["105880"]["shot_slot_plan"]["groups"][0]["group_id"], "g01")

    def test_legacy_105880_runner_is_rejected_before_any_caption_write(self) -> None:
        client = FakeCapCut()
        result = run_daily_update_te(
            {"text": "text"},
            capcut_client=client,
            directors_transport=fake_directors,
            node_runners={
                "102833": fake_visual_intent,
                "159953": fake_speech,
                "165901": fake_timelines,
                "105880": lambda _params: {},
                "capcut_stt": FakeSTT(),
                "subtitle_correction": FakeCorrection(),
            },
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_node, "105880")
        self.assertIn("旧字幕生成 runner 已废弃", result.error)
        self.assertEqual(client.calls, [])

    def test_capcut_stt_timeline_is_used_without_explicit_165901_runner(self) -> None:
        result = run_daily_update_te(
            {"text": "text"},
            capcut_client=FakeCapCut(),
            directors_transport=fake_directors,
            node_runners={
                "102833": fake_visual_intent,
                "159953": fake_speech,
                "capcut_stt": FakeSTT(),
                "subtitle_correction": FakeCorrection(),
            },
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("165901", result.completed_nodes)
        self.assertEqual(
            result.outputs["165901"]["source"],
            "capcut_stt_upload_duration_and_utterances",
        )
        self.assertEqual(result.blocked_nodes[0]["node_id"], "197742")

    def test_missing_capcut_is_blocked_before_side_effects(self) -> None:
        with patch.dict(os.environ, {"CAPCUT_MATE_BASE_URL": ""}):
            result = run_daily_update_te({"text": "text"}, directors_transport=fake_directors)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.completed_nodes, [])
        self.assertEqual(result.blocked_nodes[0]["node_id"], "182422")

    def test_injected_model_prefix_reaches_first_infinite_talk_plugin(self) -> None:
        base_input = {
            "text": "text",
            "aspect_ratio": "16:9",
            "ref_images": ["https://example.invalid/ref.jpg"],
        }
        runners = {
            "102833": fake_visual_intent,
            "159953": fake_speech,
            "165901": fake_timelines,
            "capcut_stt": FakeSTT(),
            "subtitle_correction": FakeCorrection(),
            "197742": fake_shot_director,
            "116616": fake_shot_visual_with_story,
            "139488": fake_host_selector,
        }
        draft_result = run_daily_update_te(
            base_input,
            capcut_client=FakeCapCut(),
            directors_transport=fake_directors,
            story_transport=fake_story_transport,
            node_runners=runners,
        )

        self.assertEqual(draft_result.status, "blocked")
        self.assertEqual(draft_result.blocked_nodes[0]["node_id"], "STORY_REVIEW")
        self.assertEqual(draft_result.outputs["story_draft"]["handoff_state"], "WAITING_FOR_USER_APPROVAL")

        approved_input = dict(base_input)
        approved_input["story_review"] = {
            "status": "approved",
            "draft": draft_result.outputs["story_draft"],
        }
        result = run_daily_update_te(
            approved_input,
            capcut_client=FakeCapCut(),
            directors_transport=fake_directors,
            node_runners=runners,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(
            result.completed_nodes,
            [
                "182422", "129109", "102833", "159953", "178142",
                "165901", "105880", "192311", "141288", "191683", "111882",
                "135313", "179989", "197742", "127095", "103964",
                "116616", "156199", "139488", "117861",
            ],
        )
        self.assertEqual(result.blocked_nodes, [{
            "node_id": "175652",
            "reason": "host_audio_split 真实插件 transport 尚未接入",
        }])
        self.assertEqual(result.outputs["director_lock"]["status"], "DIRECTOR_LOCKED")
        self.assertEqual(len(result.outputs["director_lock"]["groups"]), 1)
        self.assertEqual(len(result.outputs["director_lock"]["shots"]), 1)
        self.assertIn("host_llm_input", result.outputs["156199"])
        self.assertEqual(result.outputs["117861"]["error"], "")

    def test_invalid_start_input_is_failed(self) -> None:
        result = run_daily_update_te({}, capcut_client=FakeCapCut(), directors_transport=fake_directors)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_node, "100001")


if __name__ == "__main__":
    unittest.main()
