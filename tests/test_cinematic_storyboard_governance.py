from __future__ import annotations

import copy

import pytest

from workflow_1256.cinematic_storyboard_governance import (
    CinematicStoryboardGovernanceError,
    build_cinematic_storyboard_fallback,
    build_cinematic_storyboard_prompt,
    merge_governance_into_story_context,
    normalize_cinematic_storyboard_governance,
    resolve_cinematic_media_routes,
    run_cinematic_storyboard_governance,
)


def _lock() -> dict:
    return {
        "status": "DIRECTOR_LOCKED", "project_id": "p1", "run_id": "r1",
        "cinematic_story": {
            "movie_outline": {"protagonist": "林夏", "protagonist_goal": "完成任务", "central_conflict": "时间不足", "character_arc": "学会选择", "ending_hook": "新的清晨"},
            "silent_story_text": "林夏在清晨进入办公楼，最终走向日出。",
            "scene_groups": [{"scene_id": "scene_01", "group_ids": ["g01", "g02"]}],
            "narration_mappings": [{"group_id": "g01"}, {"group_id": "g02"}],
        },
        "shots": [
            {"shot_id": "g01_s01", "group_id": "g01", "timeline": {"start_us": 0, "end_us": 3_000_000}, "source_text": "第一段", "clip_role": "建立", "story_beat": "进入", "production_spec": {"story_mapping": {"group_id": "g01"}}},
            {"shot_id": "g02_s01", "group_id": "g02", "timeline": {"start_us": 3_000_000, "end_us": 6_000_000}, "source_text": "第二段", "clip_role": "转折", "story_beat": "选择", "production_spec": {"story_mapping": {"group_id": "g02"}}},
        ],
    }


def _response() -> dict:
    return {
        "director_book": {"dramatic_question": "林夏能否穿过压力", "visual_spine": "从狭窄走廊走向天光", "reveal_policy": "先压迫后开阔", "shot_rhythm_rule": "由远到近再拉开", "cutting_rule": "动作完成处切换"},
        "continuity_bible": {"protagonist_anchor": "林夏短发深色风衣", "wardrobe_anchor": "深色风衣不变", "location_anchor": "同一办公楼", "prop_state_anchor": "文件夹始终在左手", "lighting_anchor": "主光从右侧窗进入", "screen_direction_anchor": "始终由左向右", "color_anchor": "冷蓝过渡暖金", "forbidden_drifts": ["不得改变发型"]},
        "beat_sequence": [{"beat_id": "beat_01", "scene_id": "scene_01", "group_ids": ["g01", "g02"], "dramatic_function": "压力到选择", "state_before": "走廊受困", "trigger": "握紧文件夹", "state_after": "走向窗边", "bridge_to_next": "窗光承接"}],
        "shot_contexts": [
            {"shot_id": "g01_s01", "group_id": "g01", "timeline": {"start_us": 0, "end_us": 3_000_000}, "beat_id": "beat_01", "scene_id": "scene_01", "narrative_job": "建立压迫", "blocking": "门口→穿过走廊→停在窗前", "camera_intent": "远景压缩空间", "visual_direction": {"shot_size": "远景", "camera_angle": "高机位", "camera_motion": "跟拍", "composition": "边缘压力切入", "staging": "林夏贴墙穿过狭窄走廊", "lighting": "右侧冷窗光压低人物", "visual_motif": "左手文件夹"}, "continuity_in": "深色风衣进入", "continuity_out": "文件夹留在左手", "must_show": "狭窄走廊", "must_avoid": ["可读文字"]},
            {"shot_id": "g02_s01", "group_id": "g02", "timeline": {"start_us": 3_000_000, "end_us": 6_000_000}, "beat_id": "beat_01", "scene_id": "scene_01", "narrative_job": "完成选择", "blocking": "窗前→握紧文件夹→走向光", "camera_intent": "近景转中景", "visual_direction": {"shot_size": "近景", "camera_angle": "平视", "camera_motion": "拉远", "composition": "斜向纵深", "staging": "林夏握紧文件夹后向右走", "lighting": "冷蓝窗光过渡为暖金边光", "visual_motif": "左手文件夹"}, "continuity_in": "文件夹留在左手", "continuity_out": "向右走出画面", "must_show": "窗光", "must_avoid": ["图表"]},
        ],
        "review": {"status": "PASS", "checks": ["locked_ids_preserved", "locked_timelines_preserved", "story_causality_present", "continuity_rules_present", "no_text_or_ui"], "issues": []},
    }


def test_governance_preserves_locked_contract_and_merges_context() -> None:
    lock = _lock()
    prompt = build_cinematic_storyboard_prompt(lock)
    assert '"g01_s01"' in prompt
    result = normalize_cinematic_storyboard_governance(_response(), lock)
    assert result["source_contract"]["timelines_preserved"] is True
    merged = merge_governance_into_story_context(lock["cinematic_story"], result)
    assert merged["director_governance"]["shot_contexts"][1]["shot_id"] == "g02_s01"


def test_governance_rejects_changed_shot_or_prompt_leak() -> None:
    response = _response()
    response["shot_contexts"][1]["shot_id"] = "g09_s09"
    with pytest.raises(CinematicStoryboardGovernanceError, match="shot_id"):
        normalize_cinematic_storyboard_governance(response, _lock())

    response = _response()
    response["shot_contexts"][0]["prompt"] = "forbidden"
    with pytest.raises(CinematicStoryboardGovernanceError, match="素材层字段"):
        normalize_cinematic_storyboard_governance(response, _lock())


def test_governance_rejects_unpassed_review() -> None:
    response = copy.deepcopy(_response())
    response["review"]["issues"] = ["镜头跳轴"]
    with pytest.raises(CinematicStoryboardGovernanceError, match="仍有问题"):
        normalize_cinematic_storyboard_governance(response, _lock())


def test_governance_rejects_broken_same_scene_handoff() -> None:
    response = _response()
    response["shot_contexts"][1]["continuity_in"] = "忽然来到窗前"
    with pytest.raises(CinematicStoryboardGovernanceError, match="故事交接未闭合"):
        normalize_cinematic_storyboard_governance(response, _lock())


def test_governance_rejects_invalid_visual_director_field() -> None:
    response = _response()
    response["shot_contexts"][0]["visual_direction"]["composition"] = "任意构图"
    with pytest.raises(CinematicStoryboardGovernanceError, match="composition"):
        normalize_cinematic_storyboard_governance(response, _lock())


def test_paced_aigc_merge_alone_may_use_five_point_five_second_window() -> None:
    lock = _lock()
    shot = lock["shots"][1]
    shot["timeline"] = {"start_us": 3_000_000, "end_us": 8_300_000}
    shot["production_spec"] = {
        "story_mapping": {"group_id": "g02"},
        "media_pacing": {
            "force_aigc": True,
            "action": "merge_adjacent_static_to_aigc",
        },
    }

    result = build_cinematic_storyboard_fallback(lock)

    assert result["shot_contexts"][1]["media_type"] == "aigc_video"
    assert result["shot_contexts"][1]["timeline"]["end_us"] == 8_300_000

    unpaced = _lock()
    unpaced["shots"][1]["timeline"] = {"start_us": 3_000_000, "end_us": 8_300_000}
    with pytest.raises(CinematicStoryboardGovernanceError, match="2～5 秒"):
        build_cinematic_storyboard_fallback(unpaced)


def test_live_runner_compacts_foundation_request_without_losing_visual_direction() -> None:
    response = _response()
    requests: list[dict] = []

    def transport(_: str, user_prompt: str) -> dict:
        request = __import__("json").loads(user_prompt)
        requests.append(request)
        if len(requests) == 1:
            return {key: response[key] for key in ("director_book", "continuity_bible", "beat_sequence")}
        return {"shot_contexts": response["shot_contexts"]}

    result = run_cinematic_storyboard_governance(_lock(), transport=transport)
    assert "narration_mappings" not in requests[0]["approved_story"]
    assert result["shot_contexts"][0]["visual_direction"]["shot_size"] == "远景"


def test_camera_language_distribution_rejects_monotonous_long_form_plan() -> None:
    from workflow_1256.cinematic_storyboard_governance import _validate_camera_language_distribution

    contexts = [
        {"visual_direction": {"camera_angle": "平视", "camera_motion": "固定"}}
        for _ in range(8)
    ]
    with pytest.raises(CinematicStoryboardGovernanceError, match="至少需要 3 种"):
        _validate_camera_language_distribution(contexts)


def test_contract_fallback_preserves_locked_timelines_and_varies_camera_language() -> None:
    lock = _lock()
    original = lock["shots"][0]
    lock["shots"] = [
        {
            **copy.deepcopy(original),
            "shot_id": f"g01_s{index:02d}",
            "group_id": "g01",
            "timeline": {"start_us": (index - 1) * 3_000_000, "end_us": index * 3_000_000},
            "source_text": f"第{index}个叙事动作",
        }
        for index in range(1, 9)
    ]
    lock["cinematic_story"]["scene_groups"] = [{"scene_id": "scene_01", "group_ids": ["g01"]}]
    lock["cinematic_story"]["narration_mappings"] = [{"group_id": "g01"}]

    result = build_cinematic_storyboard_fallback(lock, reason="模型字段不完整")

    assert result["generation_mode"] == "deterministic_contract_baseline"
    assert result["source_contract"]["timelines_preserved"] is True
    assert [item["timeline"]["start_us"] for item in result["shot_contexts"]] == [
        0, 3_000_000, 6_000_000, 9_000_000, 12_000_000, 15_000_000, 18_000_000, 21_000_000,
    ]
    assert len({item["visual_direction"]["camera_angle"] for item in result["shot_contexts"]}) >= 3
    assert len({item["visual_direction"]["camera_motion"] for item in result["shot_contexts"]}) >= 3
    assert result["shot_contexts"][0]["shot_design_type"] == "空间空镜"
    assert result["shot_design_policy"]["minimum_empty_shot_count"] == 1


def test_fallback_uses_semantic_turn_as_an_imagery_bridge() -> None:
    lock = _lock()
    original = lock["shots"][0]
    lock["shots"] = [
        {
            **copy.deepcopy(original),
            "shot_id": f"g01_s{index:02d}",
            "group_id": "g01",
            "timeline": {"start_us": (index - 1) * 3_000_000, "end_us": index * 3_000_000},
            "source_text": "雨幕象征选择的转折" if index == 2 else f"第{index}个状态",
            "clip_role": "转折" if index == 2 else "状态建立",
        }
        for index in range(1, 5)
    ]
    lock["cinematic_story"]["scene_groups"] = [{"scene_id": "scene_01", "group_ids": ["g01"]}]
    lock["cinematic_story"]["narration_mappings"] = [{"group_id": "g01"}]

    result = build_cinematic_storyboard_fallback(lock)

    bridge = result["shot_contexts"][1]
    assert bridge["shot_design_type"] == "意象镜头"
    assert bridge["transition_role"] == "承上启下"


def test_media_route_plan_uses_host_candidates_and_low_motion_images() -> None:
    lock = _lock()
    original = lock["shots"][0]
    host_positions = {0, 1, 2, 3, 9, 10, 11, 16, 17, 18, 19}
    roles = [
        "信息触发", "现象建立", "关系变化", "结果落点", "现象建立",
        "矛盾揭露", "现象建立", "信息触发", "结果落点", "现象建立",
        "结果落点", "信息触发", "状态建立", "信息触发", "结果落点",
        "现象建立", "现象建立", "矛盾揭露", "结果落点", "结果落点",
    ]
    lock["shots"] = [
        {
            **copy.deepcopy(original),
            "shot_id": f"g01_s{index + 1:02d}",
            "group_id": "g01",
            "timeline": {"start_us": index * 3_000_000, "end_us": (index + 1) * 3_000_000},
            "clip_role": roles[index],
            "source_text": f"第{index + 1}个叙事动作",
            "route_candidates": ["host"] if index in host_positions else ["scene"],
        }
        for index in range(20)
    ]
    lock["cinematic_story"]["scene_groups"] = [{"scene_id": "scene_01", "group_ids": ["g01"]}]
    lock["cinematic_story"]["narration_mappings"] = [{"group_id": "g01"}]

    plan = resolve_cinematic_media_routes(lock)
    decisions = plan["decisions"]
    digital_indexes = [index for index, item in enumerate(decisions) if item["media_type"] == "digital_human_video"]

    assert len(digital_indexes) == 3
    assert decisions[0]["media_type"] != "digital_human_video"
    assert all(current - previous > 1 for previous, current in zip(digital_indexes, digital_indexes[1:]))
    assert all(index in host_positions for index in digital_indexes)
    # 前三镜的视频 +15 / 静态图 -15 约束生效后，静态候选向开场之后顺延。
    assert sum(item["media_type"] == "static_image" for item in decisions) == 6
    assert plan["policy"]["static_image_target_shot_count"] == 6

    fallback = build_cinematic_storyboard_fallback(lock)
    static_contexts = [item for item in fallback["shot_contexts"] if item["media_type"] == "static_image"]
    assert len(static_contexts) == 6
    assert {item["visual_direction"]["camera_motion"] for item in static_contexts} == {"固定"}


def test_media_route_plan_uses_governance_fixed_camera_for_low_motion_information_shot() -> None:
    lock = _lock()
    shot = lock["shots"][0]
    shot["shot_id"] = "g01_s01"
    shot["clip_role"] = "信息触发"
    shot["timeline"] = {"start_us": 0, "end_us": 4_500_000}
    shot["route_candidates"] = ["scene"]
    governance = {
        "shot_contexts": [{
            "shot_id": "g01_s01",
            "narrative_job": "信息触发",
            "dynamic_level": "低",
            "media_reason": "历史结果曾误标为存在动作推进，现由编导低动势标记覆盖。",
            "visual_direction": {"camera_motion": "固定"},
        }],
    }

    plan = resolve_cinematic_media_routes(lock, governance)

    assert plan["decisions"][0]["media_type"] == "aigc_video"


def test_short_digital_human_candidate_is_forced_to_static_image() -> None:
    lock = _lock()
    shot = lock["shots"][1]
    shot["timeline"] = {"start_us": 3_000_000, "end_us": 5_500_000}
    shot["route_candidates"] = ["host", "digital_human"]
    shot["selected_route"] = "digital_human"

    plan = resolve_cinematic_media_routes(lock)

    decision = plan["by_shot"]["g02_s01"]
    assert decision["media_type"] == "static_image"
    assert "低于 3 秒" in decision["media_reason"]
    assert plan["policy"]["short_shot_static_image_count"] == 1
    assert plan["policy"]["static_image_low_motion_quota_enforced"] is False


def test_media_route_plan_allows_unselected_low_motion_host_candidate_as_static_image() -> None:
    lock = _lock()
    original = lock["shots"][0]
    lock["shots"] = [
        {
            **copy.deepcopy(original),
            "shot_id": f"g01_s{index + 1:02d}",
            "group_id": "g01",
            "timeline": {"start_us": index * 4_000_000, "end_us": (index + 1) * 4_000_000},
            "clip_role": "信息触发",
            "source_text": f"第{index + 1}个低动势信息镜头",
            "route_candidates": ["host"],
        }
        for index in range(7)
    ]
    lock["digital_human_max_shot_ratio"] = 0.15

    plan = resolve_cinematic_media_routes(lock)

    assert sum(item["media_type"] == "digital_human_video" for item in plan["decisions"]) == 1
    assert sum(item["media_type"] == "static_image" for item in plan["decisions"]) == 3
    assert plan["policy"]["static_image_unselected_host_candidate_allowed"] is True
    assert plan["policy"]["static_image_target_shot_ratio"] == 0.30


def test_media_route_plan_ignores_group_level_silent_action_for_static_candidate() -> None:
    lock = _lock()
    shot = lock["shots"][0]
    shot["shot_id"] = "g01_s01"
    shot["clip_role"] = "情绪停顿"
    shot["source_text"] = "她看着存折沉默不语"
    shot["timeline"] = {"start_us": 0, "end_us": 4_000_000}
    shot["route_candidates"] = ["scene"]
    shot["production_spec"]["story_mapping"] = {"silent_action": "林夏抬手推开门"}
    governance = {
        "shot_contexts": [{
            "shot_id": "g01_s01",
            "narrative_job": "情绪停顿",
            "dynamic_level": "中",
            "blocking": "前镜动作落点→可见动作→本镜动作落点",
            "must_show": "林夏抬手推开门",
            "visual_direction": {"camera_motion": "固定"},
        }],
    }

    plan = resolve_cinematic_media_routes(lock, governance)

    assert plan["decisions"][0]["media_type"] == "aigc_video"


def test_media_route_plan_action_overrides_stale_low_motion_label() -> None:
    lock = _lock()
    shot = lock["shots"][0]
    shot["shot_id"] = "g01_s01"
    shot["clip_role"] = "信息触发"
    shot["source_text"] = "林夏抬手推开门"
    shot["timeline"] = {"start_us": 0, "end_us": 4_500_000}
    shot["route_candidates"] = ["scene"]
    governance = {
        "shot_contexts": [{
            "shot_id": "g01_s01",
            "narrative_job": "信息触发",
            "dynamic_level": "低",
            "blocking": "门前→林夏抬手推开门→门打开",
            "must_show": "林夏抬手推开门",
            "visual_direction": {"camera_motion": "固定"},
        }],
    }

    plan = resolve_cinematic_media_routes(lock, governance)

    assert plan["decisions"][0]["media_type"] == "aigc_video"


def test_media_route_plan_keeps_explicit_camera_motion_as_aigc() -> None:
    lock = _lock()
    shot = lock["shots"][0]
    shot["shot_id"] = "g01_s01"
    shot["clip_role"] = "信息触发"
    shot["timeline"] = {"start_us": 0, "end_us": 4_500_000}
    shot["route_candidates"] = ["scene"]
    governance = {
        "shot_contexts": [{
            "shot_id": "g01_s01",
            "narrative_job": "信息触发",
            "visual_direction": {"camera_motion": "横移"},
        }],
    }

    plan = resolve_cinematic_media_routes(lock, governance)

    assert plan["decisions"][0]["media_type"] == "aigc_video"


def test_media_route_plan_never_uses_digital_human_for_first_shot_even_when_forced() -> None:
    lock = _lock()
    first_shot = lock["shots"][0]
    first_shot["route_candidates"] = ["host"]
    first_shot["production_spec"]["selected_route"] = "digital_human"

    plan = resolve_cinematic_media_routes(lock)

    assert plan["decisions"][0]["shot_id"] == "g01_s01"
    assert plan["decisions"][0]["media_type"] == "aigc_video"
    assert plan["policy"]["first_shot_forbid_digital_human"] is True
    assert plan["policy"]["first_shot_must_be_aigc"] is True


def test_media_route_plan_forces_subthree_second_shots_to_static_image() -> None:
    lock = _lock()
    original = lock["shots"][0]
    lock["shots"] = [
        {
            **copy.deepcopy(original),
            "shot_id": "g01_s01",
            "group_id": "g01",
            "timeline": {"start_us": 0, "end_us": 2_500_000},
            "clip_role": "信息触发",
            "route_candidates": ["scene"],
        },
        {
            **copy.deepcopy(original),
            "shot_id": "g01_s02",
            "group_id": "g01",
            "timeline": {"start_us": 2_500_000, "end_us": 5_000_000},
            "clip_role": "关系变化",
            "route_candidates": ["host"],
            "production_spec": {"selected_route": "digital_human"},
        },
        {
            **copy.deepcopy(original),
            "shot_id": "g01_s03",
            "group_id": "g01",
            "timeline": {"start_us": 5_000_000, "end_us": 9_000_000},
            "clip_role": "矛盾揭露",
            "route_candidates": ["scene"],
        },
    ]

    plan = resolve_cinematic_media_routes(lock)

    assert [item["media_type"] for item in plan["decisions"]] == [
        "aigc_video", "static_image", "aigc_video",
    ]
    assert plan["policy"]["short_shot_static_image_count"] == 1


def test_opening_three_shots_shift_fifteen_points_from_static_to_video_preference() -> None:
    lock = _lock()
    original = lock["shots"][0]
    lock["shots"] = [
        {
            **copy.deepcopy(original),
            "shot_id": f"g01_s{index + 1:02d}",
            "group_id": "g01",
            "timeline": {"start_us": index * 4_000_000, "end_us": (index + 1) * 4_000_000},
            "clip_role": "信息触发",
            "source_text": f"第{index + 1}个低动势镜头",
            "route_candidates": ["scene"],
        }
        for index in range(5)
    ]
    governance = {
        "shot_contexts": [
            {
                "shot_id": shot["shot_id"],
                "dynamic_level": "低",
                "visual_direction": {"camera_motion": "固定"},
            }
            for shot in lock["shots"]
        ],
    }

    plan = resolve_cinematic_media_routes(lock, governance)

    assert [item["media_type"] for item in plan["decisions"]] == [
        "aigc_video", "aigc_video", "aigc_video", "static_image", "static_image",
    ]
    for decision in plan["decisions"][:3]:
        score = decision["media_selection_score"]
        assert score["adjustments"] == {"video": 15, "static_image": -15}
        assert score["video"] > score["static_image"]
    assert plan["decisions"][3]["media_selection_score"]["adjustments"] == {
        "video": 0,
        "static_image": 0,
    }
    preference = plan["policy"]["opening_video_preference"]
    assert preference["shot_count"] == 3
    assert preference["video_score_bonus"] == 15
    assert preference["static_image_score_penalty"] == 15
    assert preference["hard_rules_preserved"] == ["short_shot_static_image", "digital_human_quota"]
