from __future__ import annotations

from copy import deepcopy
import unittest

from workflow_1256.governance import GovernanceContractError, validate_director_convergence
from workflow_1256.layers import approve_cinematic_story, lock_director_output, run_cinematic_director_lock


def _artifacts() -> dict:
    return {
        "director_output": {
            "segments": ["第一段文案", "第二段文案"],
            "segment_beats": [
                {"segment_index": 0, "segment_text": "第一段文案", "rhythm": "快", "segment_goal": "开场", "beats": [{"route_candidates": ["scene", "digital_human"]}]},
                {"segment_index": 1, "segment_text": "第二段文案", "rhythm": "稳", "segment_goal": "收束", "beats": [{"route_candidates": ["scene"]}]},
            ],
        },
        "tts_group_timelines": [{"start": 0, "end": 3_000_000}, {"start": 3_000_000, "end": 6_000_000}],
        "total_timeline": {"start": 0, "end": 6_000_000},
        "shot_groups": [
            {"shots": [
                {"source_text": "第一段前半", "clip_role": "hook", "story_beat": "吸引注意"},
                {"source_text": "第一段后半", "clip_role": "explain", "story_beat": "解释"},
            ], "timelines": [{"start": 0, "end": 1_000_000}, {"start": 1_000_000, "end": 3_000_000}]},
            {"shots": [{"source_text": "第二段", "clip_role": "close", "story_beat": "结论"}], "timelines": [{"start": 3_000_000, "end": 6_000_000}]},
        ],
        "caption_segments": ["第一句", "第二句", "第三句"],
        "caption_timelines": [{"start": 0, "end": 900_000}, {"start": 1_000_000, "end": 3_000_000}, {"start": 3_000_000, "end": 6_000_000}],
        "project_id": "test-project",
        "run_id": "test-run",
        "plan_version": "director-lock-v1",
        "tts_fingerprint": "tts-real-result-sha256-redacted",
    }


def _cinematic_story() -> dict:
    return {
        "movie_outline": {
            "protagonist": "城市职场新人", "protagonist_goal": "完成关键交付",
            "central_conflict": "压力与变化同时逼近", "character_arc": "从慌乱到主动",
            "ending_hook": "新的代价仍在远处亮起",
        },
        "source_semantic_anchor": {
            "core_thesis": "信息变化要求重新理解用户", "old_system": "经验无法自动迁移",
            "new_capability": "用新工具重组信息", "governance_choice": "按用户需求调整表达",
            "systemic_cost": "效率提升也有资源代价",
        },
        "story_anchor": {
            "premise": "深夜交付前资料失序", "protagonist_want": "主角想按时完成交付",
            "opposing_force": "时间和旧经验同时压迫", "stakes_if_fail": "团队错失关键机会",
            "point_of_no_return": "倒计时进入最后一小时", "decisive_choice": "主角改用新工具重组资料",
            "transformation": "从独扛变成理解用户", "final_image_hook": "机房灯光在窗外持续亮起",
        },
        "supporting_characters": [
            {"name": "同事", "role": "催促交付", "own_goal": "按时拿到结果", "relationship_to_protagonist": "协作伙伴", "conflict_or_alliance": "催促带来压力", "decisive_interaction": "递来最后一份资料"},
            {"name": "客户", "role": "需求参照", "own_goal": "快速解决问题", "relationship_to_protagonist": "服务对象", "conflict_or_alliance": "需求打破专业自嗨", "decisive_interaction": "留下一个未解决的提问"},
        ],
        "conflict_chain": {"external": "倒计时逼近", "relationship": "同事催促与主角迟疑", "internal": "主角不敢放下旧方法", "escalation": "资料越堆越高"},
        "silent_story_text": "主角在深夜办公室整理散乱资料，随后隔窗看向持续亮着的机房。",
        "scene_groups": [
            {"scene_id": "scene_01", "group_ids": ["g01"], "scene_purpose": "建立压力", "state_before": "任务堆积", "visible_conflict": "时限逼近", "turn": "资料被归类", "state_after": "出现秩序"},
            {"scene_id": "scene_02", "group_ids": ["g02"], "scene_purpose": "留下悬念", "state_before": "完成交付", "visible_conflict": "资源持续消耗", "turn": "远望机房", "state_after": "新的问题出现"},
        ],
        "narration_mappings": [
            {"group_id": "g01", "scene_id": "scene_01", "source_claim": "信息变化要求重新理解用户", "story_range": "资料失序到重新归类", "trailer_highlight": "纸页在倒计时前重新列队", "semantic_mapping": "规模映射为压力", "conflict": "时间压迫旧经验", "choice_or_action": "主角改用新工具归整纸页", "consequence": "出现可交付的秩序", "trailer_hook": "资料转入交付前仍有一页未定", "silent_action": "归整纸页", "metaphor": "纸页是信息单元", "state_before": "慌乱", "state_after": "有序", "bridge_to_next": "资料转入交付"},
            {"group_id": "g02", "scene_id": "scene_02", "source_claim": "效率提升也有资源代价", "story_range": "交付完成后的窗前停顿", "trailer_highlight": "机房灯光在远处突然亮起", "semantic_mapping": "代价映射为机房", "conflict": "完成交付却看见新代价", "choice_or_action": "主角隔窗远望", "consequence": "新的问题出现", "trailer_hook": "灯光一直亮到画面切黑", "silent_action": "隔窗远望", "metaphor": "灯光是计算代价", "state_before": "完成", "state_after": "警觉", "bridge_to_next": "引向下一集"},
        ],
    }


class DirectorRuntimeLockTests(unittest.TestCase):
    def test_locks_fresh_artifacts_with_ids_order_and_microseconds(self) -> None:
        source = _artifacts()
        before = deepcopy(source)
        manifest = lock_director_output(
            **source,
            cinematic_story=_cinematic_story(),
            story_review_status="APPROVED",
        )
        self.assertEqual(source, before)
        self.assertEqual([group.group_id for group in manifest.groups], ["g01", "g02"])
        self.assertEqual([shot.shot_id for shot in manifest.shots], ["g01_s01", "g01_s02", "g02_s01"])
        self.assertEqual([caption.caption_id for caption in manifest.captions], ["caption_001", "caption_002", "caption_003"])
        self.assertTrue(all(caption.source_node == "capcut_stt" for caption in manifest.captions))
        self.assertTrue(any(
            item.role == "subtitle_track" and item.route == "capcut_stt" and item.source_node == "capcut_stt"
            for item in manifest.requirements
        ))
        self.assertEqual(manifest.total_timeline.to_dict(), {"start_us": 0, "end_us": 6_000_000, "duration_us": 6_000_000})
        self.assertEqual(manifest.groups[0].caption_ids, ["caption_001", "caption_002"])
        self.assertEqual(manifest.shots[0].route_candidates, ["scene", "digital_human"])
        self.assertEqual(manifest.shots[0].production_spec["duration_guidance"]["status"], "shorter_than_target")
        self.assertEqual(manifest.shots[0].production_spec["story_mapping"]["group_id"], "g01")
        self.assertEqual(manifest.groups[0].metadata["story_scene"]["scene_id"], "scene_01")
        self.assertEqual(manifest.shots[2].production_spec["duration_guidance"]["status"], "within_target")
        self.assertFalse(manifest.shots[0].production_spec["short_shot_static_image"])
        self.assertTrue(manifest.shots[0].production_spec["first_shot_must_be_aigc"])
        self.assertEqual(manifest.shots[0].requirement_ids, ["g01_s01.video.aigc", "g01_s01.image.first_frame", "g01_s01.prompt.video"])
        self.assertEqual(manifest.shots[1].requirement_ids, ["g01_s02.image.first_frame"])
        self.assertEqual(len(manifest.requirements), 13)
        self.assertEqual(manifest.requirements[1].requirement_id, "project.image.character_anchor")
        self.assertIn({"source_node": "故事编写器", "path": "movie_outline/scene_groups/narration_mappings"}, manifest.provenance)
        self.assertEqual(validate_director_convergence(manifest).status, "DIRECTOR_LOCKED")

    def test_rejects_cinematic_story_group_coverage_drift(self) -> None:
        data = _artifacts()
        story = _cinematic_story()
        story["scene_groups"][1]["group_ids"] = ["g01"]
        with self.assertRaisesRegex(GovernanceContractError, "scene_groups"):
            lock_director_output(**data, cinematic_story=story, story_review_status="APPROVED")

    def test_formal_cinematic_entry_runs_story_before_locking(self) -> None:
        data = _artifacts()
        received = []

        def transport(system, user):
            received.append((system, user))
            return _cinematic_story()

        draft = run_cinematic_director_lock(
            **data,
            text="用于验证编导收口的完整文案",
            story_transport=transport,
        )
        self.assertEqual(draft.review_status, "PENDING_USER_REVIEW")
        self.assertEqual(draft.to_dict()["handoff_state"], "WAITING_FOR_USER_APPROVAL")
        manifest = approve_cinematic_story(draft, **data)
        self.assertEqual(manifest.status, "DIRECTOR_LOCKED")
        self.assertEqual(manifest.story_review_status, "APPROVED")
        self.assertEqual(manifest.groups[1].metadata["story_mapping"]["group_id"], "g02")
        self.assertIn("用于验证编导收口的完整文案", received[0][1])

    def test_rejects_misaligned_director_segment_index(self) -> None:
        data = _artifacts()
        data["director_output"]["segment_beats"][1]["segment_index"] = 0
        with self.assertRaisesRegex(GovernanceContractError, "segment_index"):
            lock_director_output(**data, cinematic_story=_cinematic_story(), story_review_status="APPROVED")

    def test_rejects_shot_timeline_gap(self) -> None:
        data = _artifacts()
        data["shot_groups"][0]["timelines"][1]["start"] = 1_100_000
        with self.assertRaisesRegex(GovernanceContractError, "不连续"):
            lock_director_output(**data, cinematic_story=_cinematic_story(), story_review_status="APPROVED")

    def test_rejects_caption_outside_all_groups(self) -> None:
        data = _artifacts()
        data["caption_timelines"][2] = {"start": 5_900_000, "end": 6_100_000}
        with self.assertRaisesRegex(GovernanceContractError, "字幕时间线"):
            lock_director_output(**data, cinematic_story=_cinematic_story(), story_review_status="APPROVED")

    def test_rejects_material_prompt_leaking_into_director_visual_spec(self) -> None:
        data = _artifacts()
        data["visual_specs"] = [{"composition": "近景"}, {"prompt": "不得进入编导层"}, {"composition": "远景"}]
        with self.assertRaisesRegex(GovernanceContractError, "素材层"):
            lock_director_output(**data, cinematic_story=_cinematic_story(), story_review_status="APPROVED")

    def test_rejects_visual_spec_count_drift(self) -> None:
        data = _artifacts()
        data["visual_specs"] = [{"composition": "近景"}]
        with self.assertRaisesRegex(GovernanceContractError, "visual_specs 数量"):
            lock_director_output(**data, cinematic_story=_cinematic_story(), story_review_status="APPROVED")


if __name__ == "__main__":
    unittest.main()
