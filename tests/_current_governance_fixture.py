"""当前 TTS-STT 编导链路的无外部依赖治理测试夹具。"""

from __future__ import annotations

from workflow_1256.governance import (
    AssetRecord,
    AssetRegistry,
    EditBinding,
    EditManifest,
    ProjectManifest,
    TimeWindow,
)
from workflow_1256.layers import lock_director_output


def _story() -> dict:
    return {
        "movie_outline": {
            "protagonist": "内容创作者", "protagonist_goal": "完成可信表达",
            "central_conflict": "时间压力与表达准确性冲突", "character_arc": "从急于交付到尊重证据",
            "ending_hook": "下一次表达仍需继续验证",
        },
        "source_semantic_anchor": {
            "core_thesis": "可信表达需要尊重真实时间线", "old_system": "按文字比例猜测时间",
            "new_capability": "依据真实 TTS 与 STT 时间线规划", "governance_choice": "先锁定时序再生成画面",
            "systemic_cost": "需要额外的转写与校对步骤",
        },
        "story_anchor": {
            "premise": "创作者在截止前校准一条口播", "protagonist_want": "让画面与旁白准确同步",
            "opposing_force": "旧字幕链路产生错位", "stakes_if_fail": "观众理解被打断",
            "point_of_no_return": "真实 TTS 已生成", "decisive_choice": "用 STT 时间线重新规划镜头",
            "transformation": "从猜测节奏转为依据音频", "final_image_hook": "时间线在屏幕上对齐",
        },
        "supporting_characters": [
            {"name": "剪辑师", "role": "时序校验", "own_goal": "交付可编辑脚本", "relationship_to_protagonist": "协作者", "conflict_or_alliance": "坚持按真实时间线", "decisive_interaction": "指出旧字幕错位"},
            {"name": "观众", "role": "表达对象", "own_goal": "清楚理解观点", "relationship_to_protagonist": "接收者", "conflict_or_alliance": "要求画面和旁白一致", "decisive_interaction": "在评论区反馈节奏"},
        ],
        "conflict_chain": {"external": "截止时间临近", "relationship": "创作者与剪辑师校验节奏", "internal": "是否继续依赖旧规则", "escalation": "字幕与镜头开始错位"},
        "silent_story_text": "创作者和剪辑师在屏幕前核对真实字幕时间线，最后确认逐镜脚本。",
        "scene_groups": [
            {"scene_id": "scene_01", "group_ids": ["g01"], "scene_purpose": "发现错位", "state_before": "旧链路输出", "visible_conflict": "字幕与画面不同步", "turn": "读取 STT 时间线", "state_after": "得到准确坑位"},
            {"scene_id": "scene_02", "group_ids": ["g02"], "scene_purpose": "完成收口", "state_before": "坑位已锁定", "visible_conflict": "需保持下游顺序", "turn": "生成逐镜脚本", "state_after": "交付可编辑结果"},
        ],
        "narration_mappings": [
            {"group_id": "g01", "scene_id": "scene_01", "source_claim": "可信表达需要尊重真实时间线", "story_range": "发现旧字幕错位", "trailer_highlight": "时间线出现偏差", "semantic_mapping": "错位映射为风险", "conflict": "旧规则与真实音频不一致", "choice_or_action": "读取 STT 结果", "consequence": "得到准确坑位", "trailer_hook": "第二段是否仍能对齐", "silent_action": "核对时间线", "metaphor": "刻度代表证据", "state_before": "错位", "state_after": "对齐", "bridge_to_next": "切到逐镜脚本"},
            {"group_id": "g02", "scene_id": "scene_02", "source_claim": "先锁定时序再生成画面", "story_range": "生成逐镜脚本", "trailer_highlight": "镜头按坑位排列", "semantic_mapping": "坑位映射为秩序", "conflict": "下游不得改变顺序", "choice_or_action": "锁定编导出口", "consequence": "素材层可消费", "trailer_hook": "等待真实外部验收", "silent_action": "确认逐镜脚本", "metaphor": "时间线代表秩序", "state_before": "待编排", "state_after": "可交付", "bridge_to_next": "进入素材层"},
        ],
    }


def build_current_governance_fixture() -> ProjectManifest:
    director = lock_director_output(
        director_output={
            "segments": ["第一段旁白。", "第二段旁白。"],
            "segment_beats": [
                {"segment_index": 0, "segment_text": "第一段旁白。", "rhythm": "hook", "segment_goal": "发现时序问题", "beats": [{"route_candidates": ["scene"]}]},
                {"segment_index": 1, "segment_text": "第二段旁白。", "rhythm": "close", "segment_goal": "完成时序收口", "beats": [{"route_candidates": ["scene"]}]},
            ],
        },
        tts_group_timelines=[{"start": 0, "end": 3_000_000}, {"start": 3_000_000, "end": 6_000_000}],
        total_timeline={"start": 0, "end": 6_000_000},
        shot_groups=[
            {"shots": [
                {"source_text": "第一段前半", "narration_text": "第一段", "clip_role": "开场", "story_beat": "发现"},
                {"source_text": "第一段后半", "narration_text": "旁白。", "clip_role": "解释", "story_beat": "校准"},
            ], "timelines": [{"start": 0, "end": 1_500_000}, {"start": 1_500_000, "end": 3_000_000}]},
            {"shots": [{"source_text": "第二段", "narration_text": "第二段旁白。", "clip_role": "收束", "story_beat": "锁定"}], "timelines": [{"start": 3_000_000, "end": 6_000_000}]},
        ],
        caption_segments=["第一段", "旁白。", "第二段旁白。"],
        caption_timelines=[{"start": 0, "end": 1_500_000}, {"start": 1_500_000, "end": 3_000_000}, {"start": 3_000_000, "end": 6_000_000}],
        project_id="current-stt-contract-fixture",
        run_id="current-stt-run",
        plan_version="current-stt-v1",
        tts_fingerprint="current-stt-fixture",
        cinematic_story=_story(),
        story_review_status="APPROVED",
        narration_assets=[
            {"asset_id": "g01.audio.narration", "requirement_id": "g01.audio.narration", "group_id": "g01", "timeline": {"start": 0, "end": 3_000_000}, "local_path": "synthetic://g01.mp3", "source_node": "159953"},
            {"asset_id": "g02.audio.narration", "requirement_id": "g02.audio.narration", "group_id": "g02", "timeline": {"start": 3_000_000, "end": 6_000_000}, "local_path": "synthetic://g02.mp3", "source_node": "159953"},
        ],
        subtitle_pipeline={"status": "succeeded", "source": "capcut_stt_doubao_mini", "timeline_unit": "microseconds"},
    )
    shot = director.shots[0]
    requirement_id = shot.requirement_ids[0]
    timeline = shot.timeline
    asset = AssetRecord(
        asset_id="fixture.asset.g01_s01", requirement_id=requirement_id, group_id=shot.group_id,
        shot_ids=[shot.shot_id], asset_type="video", role="primary_visual", status="OBSERVED_REDACTED",
        source_node="fixture", source_index=0, asset_version="fixture-assets-v1",
        asset_duration_us=timeline.duration_us, actual_timeline=timeline,
    )
    assets = AssetRegistry(
        project_id=director.project_id, run_id=director.run_id, source_plan_version=director.plan_version,
        asset_version="fixture-assets-v1", records=[asset], status="ASSET_MAPPING_ONLY",
    )
    edit = EditManifest(
        project_id=director.project_id, run_id=director.run_id, source_plan_version=director.plan_version,
        source_asset_version=assets.asset_version, edit_version="fixture-edit-v1", status="EDIT_MAPPING_ONLY",
        tracks=[], draft_url="", local_draft_path="", validation={"fixture": True},
        bindings=[EditBinding(
            shot_id=shot.shot_id, requirement_id=requirement_id, asset_id=asset.asset_id,
            track_role="主画面", track_id="fixture-track", segment_id="fixture-segment",
            planned_timeline=timeline, asset_duration_us=timeline.duration_us, edit_timeline=timeline,
            fit_policy="exact_slot", status="PLANNED",
        )],
    )
    mapping = {
        "ref_node": "105880", "source_path": "captions/shot_slot_plan", "target_path": "director.captions/shots",
        "source_type": "array", "target_type": "array", "cardinality": "ordered", "order_policy": "preserve",
        "timeline_unit": "microseconds", "evidence_status": "synthetic_current_path",
    }
    return ProjectManifest(
        director.project_id, director.run_id, director, assets, edit, "synthetic_current_stt_contract",
        mapping_notes=["当前 TTS-STT 字幕链路测试夹具"], field_mappings=[mapping],
    )
