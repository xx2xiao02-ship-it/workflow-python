from __future__ import annotations

import pytest

from workflow_1256.governance.director_media_route_lock import (
    DirectorMediaRouteLockError,
    build_director_media_route_lock,
)
from tools import video_production_console as console


def _context(shot_id: str, start_us: int, end_us: int, *, media_type: str, motion: str, shot_size: str, reason: str) -> dict:
    return {
        "shot_id": shot_id,
        "group_id": "g01",
        "timeline": {"start_us": start_us, "end_us": end_us, "duration_us": end_us - start_us},
        "narrative_job": "状态建立",
        "blocking": "人物停在窗前，城市远景保持不动",
        "visual_direction": {"camera_motion": motion, "shot_size": shot_size},
        "media_type": media_type,
        "media_reason": reason,
    }


def _lock_and_governance() -> tuple[dict, dict]:
    shots = [
        {
            "shot_id": "g01_s01", "group_id": "g01",
            "timeline": {"start_us": 0, "end_us": 1_200_000, "duration_us": 1_200_000},
            "production_spec": {"narration_text": "门外的雨声忽然停了。"},
        },
        {
            "shot_id": "g01_s02", "group_id": "g01",
            "timeline": {"start_us": 1_200_000, "end_us": 4_700_000, "duration_us": 3_500_000},
            "production_spec": {"narration_text": "她抱起文件冲向电梯。"},
        },
        {
            "shot_id": "g01_s03", "group_id": "g01",
            "timeline": {"start_us": 4_700_000, "end_us": 8_200_000, "duration_us": 3_500_000},
            "production_spec": {"narration_text": "主持人正面给出关键解释。", "selected_route": "digital_human"},
        },
    ]
    return (
        {"status": "DIRECTOR_LOCKED", "shots": shots},
        {"shot_contexts": [
            _context("g01_s01", 0, 1_200_000, media_type="aigc_video", motion="跟拍", shot_size="远景", reason="全片首镜必须使用 AIGC 建立开场视觉锚点。"),
            _context("g01_s02", 1_200_000, 4_700_000, media_type="aigc_video", motion="跟拍", shot_size="中景", reason="人物冲向电梯有连续动作与运动关系，需要视频表达。"),
            _context("g01_s03", 4_700_000, 8_200_000, media_type="digital_human_video", motion="固定", shot_size="中景", reason="该坑位已被编导指定为数字人正面讲解。"),
        ]},
    )


def test_media_route_lock_preserves_slot_timeline_and_declares_skip_rules() -> None:
    lock, governance = _lock_and_governance()
    result = build_director_media_route_lock(lock, governance)

    assert result["status"] == "DIRECTOR_MEDIA_LOCKED"
    assert result["source_contract"]["timelines_preserved"] is True
    assert [item["media_type"] for item in result["routes"]] == [
        "aigc_video", "aigc_video", "digital_human_video",
    ]
    assert result["routes"][0]["timeline"]["duration_us"] == 1_200_000
    assert result["routes"][0]["skip"]["skip_aigc_video"] is False
    assert result["routes"][1]["skip"]["skip_video_prompt"] is False
    assert result["routes"][2]["skip"]["skip_first_frame"] is True
    assert result["routes"][0]["media_selection_score"]["adjustments"] == {
        "video": 15, "static_image": -15,
    }
    assert {item["editing_track"] for item in result["routes"]} == {"main_visual"}


def test_static_image_rejects_non_fixed_dynamic_shot() -> None:
    lock, governance = _lock_and_governance()
    lock["shots"][1]["timeline"] = {"start_us": 1_200_000, "end_us": 5_100_000, "duration_us": 3_900_000}
    lock["shots"][1]["clip_role"] = "状态建立"
    governance["shot_contexts"][1]["timeline"] = {"start_us": 1_200_000, "end_us": 5_100_000, "duration_us": 3_900_000}
    governance["shot_contexts"][1]["visual_direction"]["camera_motion"] = "推近"
    governance["shot_contexts"][1]["media_type"] = "static_image"
    governance["shot_contexts"][1]["media_reason"] = "历史静态图片路由待校验。"
    with pytest.raises(DirectorMediaRouteLockError, match="固定机位"):
        build_director_media_route_lock(lock, governance)


def test_short_selected_digital_human_slot_is_static_and_skips_aigc() -> None:
    lock, governance = _lock_and_governance()
    lock["shots"][2]["timeline"] = {"start_us": 4_700_000, "end_us": 7_000_000, "duration_us": 2_300_000}
    governance["shot_contexts"][2]["timeline"] = {"start_us": 4_700_000, "end_us": 7_000_000, "duration_us": 2_300_000}
    governance["shot_contexts"][2]["media_type"] = "static_image"
    governance["shot_contexts"][2]["media_reason"] = "低于 3 秒，使用图片完成快速切换。"

    result = build_director_media_route_lock(lock, governance)

    route = result["routes"][2]
    assert route["media_type"] == "static_image"
    assert route["skip"]["skip_aigc_video"] is True
    assert route["required_assets"] == [{"asset": "first_frame_image", "action": "create", "source": "首帧生成"}]


def test_short_slot_cannot_be_promoted_to_aigc() -> None:
    lock, governance = _lock_and_governance()
    lock["shots"][1]["timeline"] = {"start_us": 1_200_000, "end_us": 3_700_000, "duration_us": 2_500_000}
    lock["shots"][1]["production_spec"]["media_pacing"] = {
        "policy": "consecutive_static_weighted_aigc_v1",
        "force_aigc": True,
    }
    governance["shot_contexts"][1]["timeline"] = {"start_us": 1_200_000, "end_us": 3_700_000, "duration_us": 2_500_000}
    governance["shot_contexts"][1]["visual_direction"]["camera_motion"] = "跟拍"
    governance["shot_contexts"][1]["media_type"] = "aigc_video"
    governance["shot_contexts"][1]["media_reason"] = "连续静态图片达到阈值，提升为 AIGC。"

    with pytest.raises(DirectorMediaRouteLockError, match=r"已锁定为\s+静态图片"):
        build_director_media_route_lock(lock, governance)


def test_static_image_accepts_fixed_medium_shot_when_route_policy_marks_low_motion() -> None:
    lock, governance = _lock_and_governance()
    # 开场前三镜带有视频优先评分，因此把该静态图片校验放到第 4 镜。选路器允许
    # 不超过 4 秒的低动态“状态建立”镜头使用图片；此处必须与其一致，
    # 不能因为画面是中景再将已锁定的图片路线拒绝。
    extra_shot = {**lock["shots"][1], "shot_id": "g01_s04", "timeline": {"start_us": 8_200_000, "end_us": 12_100_000, "duration_us": 3_900_000}, "clip_role": "状态建立", "route_candidates": ["scene"]}
    extra_context = _context("g01_s04", 8_200_000, 12_100_000, media_type="static_image", motion="固定", shot_size="中景", reason="固定机位的低动势状态建立镜头。")
    lock["shots"].append(extra_shot)
    governance["shot_contexts"].append(extra_context)

    result = build_director_media_route_lock(lock, governance)

    assert result["routes"][3]["media_type"] == "static_image"


def test_media_route_lock_rejects_digital_human_first_shot() -> None:
    lock, governance = _lock_and_governance()
    governance["shot_contexts"][0]["media_type"] = "digital_human_video"
    governance["shot_contexts"][0]["media_reason"] = "旧快照错误地把首帧分给数字人。"
    with pytest.raises(DirectorMediaRouteLockError, match="全片第一个镜头必须使用 AIGC"):
        build_director_media_route_lock(lock, governance)


def test_video_prompt_input_skips_static_and_digital_human_slots() -> None:
    lock, governance = _lock_and_governance()
    route_lock = build_director_media_route_lock(lock, governance)
    director_result = {
        "director_lock": lock,
        "shot_refinement": {"output": {"Code_list": [{
            "group_id": "g01", "shots": [{}, {}, {}],
        }]}},
    }
    material_plan = {
        "media_route_lock": route_lock,
        "motion_seed": ["静态种子", "动作种子", "数字人种子"],
        "ref_image": [{"ref_image": []}, {"ref_image": ["frame.png"]}, {"ref_image": []}],
        "int_duration": [2, 4, 4],
        "director_governance": {"shot_contexts": [
            {**governance["shot_contexts"][0], "camera_intent": "建立空间", "visual_direction": {**governance["shot_contexts"][0]["visual_direction"], "camera_angle": "高机位", "composition": "斜向纵深"}},
            {**governance["shot_contexts"][1], "camera_intent": "跟随动作", "visual_direction": {**governance["shot_contexts"][1]["visual_direction"], "camera_angle": "平视", "composition": "斜向纵深"}},
            {**governance["shot_contexts"][2], "camera_intent": "正面说明", "visual_direction": {**governance["shot_contexts"][2]["visual_direction"], "camera_angle": "平视", "composition": "局部焦点与留白"}},
        ]},
    }

    params = console._group_video_prompt_inputs(director_result, material_plan)

    assert params["shot_ids"] == ["g01_s01", "g01_s02"]
    assert params["skipped_shot_ids"] == ["g01_s03"]
    assert len(params["items"]) == 2
    assert params["motion_seed"] == [{"motion_seed": ["静态种子"]}, {"motion_seed": ["动作种子"]}]


def test_shot_script_keeps_all_slots_but_only_aigc_has_video_plan() -> None:
    lock, governance = _lock_and_governance()
    route_lock = build_director_media_route_lock(lock, governance)
    contexts = [
        {**item, "camera_intent": "建立空间", "continuity_in": "", "continuity_out": "窗边的雨", "visual_direction": {**item["visual_direction"], "camera_angle": "高机位", "composition": "斜向纵深"}}
        for item in governance["shot_contexts"]
    ]
    director_result = {"director_lock": lock}
    material_plan = {
        "media_route_lock": route_lock,
        "prompt": ["首镜视频首帧提示词", "视频首帧提示词", "数字人不应有首帧"],
        "motion_seed": ["静态种子", "视频种子", "数字人种子"],
        "director_governance": {"shot_contexts": contexts},
    }
    video_plan = {
        "shot_ids": ["g01_s01", "g01_s02"],
        "plan_out_list": [{"plans": [{
            "shot_index": 0,
            "stages": [{"time_range": "0-1.2秒", "camera_motion": "跟拍", "action": "建立开场视觉锚点"}],
        }]}, {"plans": [{
            "shot_index": 1,
            "stages": [{"time_range": "0-3.5秒", "camera_motion": "跟拍", "action": "抱着文件冲向电梯"}],
        }]}],
    }

    script = console._build_shot_video_script(director_result, material_plan, video_plan)

    assert [item["media_type"] for item in script] == ["aigc_video", "aigc_video", "digital_human_video"]
    assert script[0]["first_frame_prompt"] == "首镜视频首帧提示词"
    assert script[0]["camera_motion"][0]["camera_motion"] == "跟拍"
    assert script[1]["motion_seed"] == "视频种子"
    assert script[2]["first_frame_prompt"] is None
    assert script[2]["motion_seed"] is None
    assert {item["editing_track"] for item in script} == {"main_visual"}
