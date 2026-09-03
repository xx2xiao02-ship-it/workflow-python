from __future__ import annotations

import unittest
from dataclasses import replace

from workflow_1256.governance import (
    AssetRecord,
    AssetRegistry,
    DigitalHumanGovernanceError,
    EditBinding,
    EditManifest,
    GovernanceContractError,
    ProjectManifest,
    TimeWindow,
    build_director_locked_manifest,
    validate_digital_human_slots,
    DEFAULT_MAX_DIGITAL_HUMAN_SHOT_RATIO,
)


def _director():
    return build_director_locked_manifest(
        project_id="test", run_id="run", plan_version="v1", tts_fingerprint="tts",
        director_output={
            "segments": ["第一段"],
            "segment_beats": [{"segment_index": 0, "segment_text": "第一段", "rhythm": "快", "segment_goal": "开场", "beats": [
                {"route_candidates": ["scene", "digital_human"]},
            ]}],
        },
        tts_group_timelines=[{"start": 0, "end": 16_000_000}],
        total_timeline={"start": 0, "end": 16_000_000},
        shot_groups=[{
            "shots": [
                {"source_text": "镜头一", "clip_role": "hook", "story_beat": "建立", "selected_route": "digital_human"},
                {"source_text": "镜头二", "clip_role": "explain", "story_beat": "推进", "selected_route": "digital_human"},
                {"source_text": "镜头三", "clip_role": "turn", "story_beat": "转折", "selected_route": "digital_human"},
                {"source_text": "镜头四", "clip_role": "close", "story_beat": "收束", "selected_route": "digital_human"},
            ],
            "timelines": [
                {"start": 0, "end": 4_000_000}, {"start": 4_000_000, "end": 8_000_000},
                {"start": 8_000_000, "end": 12_000_000}, {"start": 12_000_000, "end": 16_000_000},
            ],
        }],
        caption_segments=[],
        caption_timelines=[],
        story_review_status="APPROVED",
        cinematic_story={
            "movie_outline": {
                "protagonist": "测试主角", "protagonist_goal": "完成任务",
                "central_conflict": "时间不足", "character_arc": "主动解决",
                "ending_hook": "新的挑战出现",
            },
            "silent_story_text": "测试主角在办公室完成一次任务。",
            "scene_groups": [{
                "scene_id": "scene_01", "group_ids": ["g01"], "scene_purpose": "建立任务",
                "state_before": "混乱", "visible_conflict": "时间不足", "turn": "找到方案", "state_after": "推进",
            }],
            "narration_mappings": [{
                "group_id": "g01", "scene_id": "scene_01", "semantic_mapping": "任务推进",
                "silent_action": "整理资料", "metaphor": "文件象征任务", "state_before": "混乱",
                "state_after": "推进", "bridge_to_next": "进入下一步",
            }],
        },
    )


class DigitalHumanGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.director = _director()

    def test_exact_non_consecutive_slots_within_default_ratio_pass(self) -> None:
        result = validate_digital_human_slots(self.director, [{
            "target_shot_id": "g01_s02", "start": 4_000_000, "end": 8_000_000,
        }], policy={"max_shot_ratio": 0.25})
        self.assertEqual(result["digital_human_shot_ids"], ["g01_s02"])
        self.assertEqual(result["actual_shot_ratio"], 0.25)
        self.assertEqual(DEFAULT_MAX_DIGITAL_HUMAN_SHOT_RATIO, 0.15)

    def test_rejects_timeline_that_does_not_equal_target_slot(self) -> None:
        with self.assertRaisesRegex(DigitalHumanGovernanceError, "精确等于"):
            validate_digital_human_slots(self.director, [{
                "target_shot_id": "g01_s02", "start": 4_000_000, "end": 7_999_999,
            }])

    def test_rejects_consecutive_or_unapproved_or_over_ratio_slots(self) -> None:
        with self.assertRaisesRegex(DigitalHumanGovernanceError, "不允许连续"):
            validate_digital_human_slots(self.director, [
                {"target_shot_id": "g01_s02", "start": 4_000_000, "end": 8_000_000},
                {"target_shot_id": "g01_s03", "start": 8_000_000, "end": 12_000_000},
            ], policy={"max_shot_ratio": 1})
        with self.assertRaisesRegex(DigitalHumanGovernanceError, "超过上限"):
            validate_digital_human_slots(self.director, [{
                "target_shot_id": "g01_s02", "start": 4_000_000, "end": 8_000_000,
            }], policy={"max_shot_ratio": 0.2})

    def test_project_manifest_blocks_unbound_digital_human_before_editing(self) -> None:
        timeline = TimeWindow(4_000_000, 8_000_000)
        record = AssetRecord(
            asset_id="asset.digital.001", requirement_id="g01_s02.video.digital_human",
            group_id="g01", shot_ids=["g01_s02"], asset_type="video", role="digital_human",
            status="VALIDATED", source_node="数字人", source_index=0, asset_version="assets-v1",
            asset_duration_us=timeline.duration_us, actual_timeline=timeline, remote_url="https://example.invalid/a.mp4",
            metadata={"target_shot_id": "g01_s02"},
        )
        assets = AssetRegistry(
            project_id="test", run_id="run", source_plan_version="v1", asset_version="assets-v1",
            records=[record], status="ASSETS_READY", digital_human_policy={"max_shot_ratio": 0.25},
        )
        edit = EditManifest(
            project_id="test", run_id="run", source_plan_version="v1", source_asset_version="assets-v1",
            edit_version="edit-v1", bindings=[EditBinding(
                shot_id="g01_s02", requirement_id=record.requirement_id, asset_id=record.asset_id,
                track_role="数字人", track_id="track", segment_id="segment", planned_timeline=timeline,
                asset_duration_us=timeline.duration_us, edit_timeline=timeline, fit_policy="exact_slot", status="PLANNED",
            )], tracks=[], draft_url="", local_draft_path="", status="EDIT_PLAN_READY",
        )
        project = ProjectManifest("test", "run", self.director, assets, edit, "synthetic")
        self.assertEqual(project.assets.records[0].metadata["target_shot_id"], "g01_s02")

        missing_target = replace(record, metadata={})
        invalid_assets = AssetRegistry(
            project_id="test", run_id="run", source_plan_version="v1", asset_version="assets-v2",
            records=[missing_target], status="ASSETS_READY",
        )
        invalid_edit = EditManifest(
            project_id="test", run_id="run", source_plan_version="v1", source_asset_version="assets-v2",
            edit_version="edit-v2", bindings=[EditBinding(
                shot_id="g01_s02", requirement_id=missing_target.requirement_id, asset_id=missing_target.asset_id,
                track_role="数字人", track_id="track", segment_id="segment", planned_timeline=timeline,
                asset_duration_us=timeline.duration_us, edit_timeline=timeline, fit_policy="exact_slot", status="PLANNED",
            )], tracks=[], draft_url="", local_draft_path="", status="EDIT_PLAN_READY",
        )
        with self.assertRaisesRegex(GovernanceContractError, "target_shot_id"):
            ProjectManifest("test", "run", self.director, invalid_assets, invalid_edit, "synthetic")


if __name__ == "__main__":
    unittest.main()
