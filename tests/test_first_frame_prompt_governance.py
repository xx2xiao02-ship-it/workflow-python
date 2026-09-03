from __future__ import annotations

import copy
import json
from pathlib import Path

from workflow_1256.asset_generation_contracts import compile_first_frame_prompt_audit
from workflow_1256.cinematic_storyboard_governance import build_cinematic_storyboard_fallback
from workflow_1256.first_frame_grid import build_first_frame_grid_plan


def _structured_shot(*, scene: str, action: str, motion: str, composition: str = "遮挡或框景") -> dict:
    return {
        "first_frame_prompt": (
            f"【场景】{scene}\n"
            f"【主体与动作】{action}\n"
            f"【镜头语言】景别=全景；视角=平视；构图={composition}\n"
            f"【镜头动势】{motion}\n"
            "【调度】人物与关键物件完成单一动作\n"
            "【光线与氛围】侧向主光"
        ),
        "camera_motion": [motion],
        "shot_size": "全景",
        "viewpoint": "平视",
        "composition": composition,
    }


def test_grid_template_removes_historical_bicycle_street_example() -> None:
    plan = build_first_frame_grid_plan([{"shot_id": "s1", "prompt": "【场景】菜场【主体与动作】陈岭整理铁盒"}])

    prompt = plan["batches"][0]["prompt"]
    assert "自行车/街道场景" not in prompt
    assert "关键道具布局或场景构图" in prompt
    assert "无缝仅表示没有边框、间隙、网格线或留白隔断" in prompt
    assert "每格的场景、主体、道具与镜头关系都必须在本格内完整闭合" in prompt


def test_static_projection_removes_video_timeline_processes() -> None:
    shot = _structured_shot(
        scene="居民楼路边",
        action="陈岭接过舞鞋，回头见父亲捂胸蹲在路边",
        motion="高机位跟拍指尖的移动轨迹，继续向前跑几步，逐渐放缓，随后停住",
    )

    compiled = compile_first_frame_prompt_audit(shot, aspect_ratio="16:9")
    prompt = str(compiled["prompt"])
    for forbidden in ("高机位跟拍", "继续向前跑几步", "逐渐放缓", "随后", "最终停住"):
        assert forbidden not in prompt
    assert "画面定格在主体与关键物件形成清晰关系的瞬间" in prompt
    assert "VIDEO_MOTION_PROJECTED_TO_STATIC" in compiled["warnings"]


def test_static_projection_hides_internal_scene_continuity_notation() -> None:
    shot = _structured_shot(
        scene="深夜家中桌前",
        action="scene_02中与当前段落相关的既有物件状态与动作余波",
        motion="固定，堆叠的钱币保持静止",
    )

    compiled = compile_first_frame_prompt_audit(shot)
    assert "scene_02" not in str(compiled["prompt"])
    assert "SCENE_CONTINUITY_OMITTED" in compiled["warnings"]


def test_structured_composition_stops_at_next_field_and_motion_is_not_duplicated() -> None:
    shot = _structured_shot(
        scene="居民楼路边",
        action="陈岭站在父亲身侧",
        motion="缓慢横摇镜，透过半开的门框形成框景",
        composition="遮挡或框景",
    )
    shot.pop("composition")
    # Reproduce the legacy field boundary that previously made ``构图`` eat
    # the following structured fields.
    shot["first_frame_prompt"] = shot["first_frame_prompt"].replace(
        "；视角=平视；构图=遮挡或框景\n",
        "；视角=平视；构图=遮挡或框景\n",
    )

    compiled = compile_first_frame_prompt_audit(shot)
    prompt = str(compiled["prompt"])
    assert "构图=遮挡或框景【镜头动势】" not in prompt
    assert prompt.count("【镜头动势】") == 1
    assert "【镜头动势】" not in str(compiled["compiled_fields"]["composition"])


def test_static_scene_guard_drops_conflicting_indoor_camera_extension() -> None:
    shot = _structured_shot(
        scene="居民楼路边",
        action="陈岭接过舞鞋，回头见父亲捂胸蹲在路边",
        motion="缓慢横摇镜，平稳扫过室内桌旁，透过半开的门框形成框景",
    )

    plan = build_first_frame_grid_plan([{
        "shot_id": "g03_s02",
        "compiled_first_frame_prompt": compile_first_frame_prompt_audit(shot)["prompt"],
        "camera_motion": shot["camera_motion"],
        "shot_size": "全景",
        "composition": "遮挡或框景",
    }])
    cell = plan["batches"][0]["cells"][0]
    prompt = plan["batches"][0]["prompt"]

    assert "STATIC_SCENE_CONFLICT" in cell["static_frame_audit"]["warnings"]
    assert "居民楼路边" in prompt
    assert "室内桌旁" not in prompt
    assert "半开的门框" not in prompt


def test_single_action_normalization_does_not_leave_a_second_action() -> None:
    shot = _structured_shot(
        scene="菜场摊位前",
        action="周姐把新招牌推到摊前，又敲了敲租金牌",
        motion="固定，手停在招牌边缘",
    )

    compiled = compile_first_frame_prompt_audit(shot)
    prompt = str(compiled["prompt"])
    assert "周姐把新招牌推到摊前" in prompt
    assert "又敲了敲租金牌" not in prompt
    assert "MULTI_ACTION_NORMALIZED" in compiled["warnings"]


def test_single_character_fallback_never_uses_multi_person_composition() -> None:
    base_shot = {
        "shot_id": "g01_s01",
        "group_id": "g01",
        "timeline": {"start_us": 0, "end_us": 3_000_000},
        "source_text": "林夏整理铁盒",
        "clip_role": "建立",
        "story_beat": "进入",
        "production_spec": {"story_mapping": {"group_id": "g01"}},
    }
    lock = {
        "status": "DIRECTOR_LOCKED",
        "known_character_count": 1,
        "cinematic_story": {
            "movie_outline": {"protagonist": "林夏", "central_conflict": "时间不足"},
            "silent_story_text": "林夏整理铁盒。",
            "scene_groups": [{"scene_id": "scene_01", "group_ids": ["g01"]}],
            "narration_mappings": [{"group_id": "g01"}],
        },
        "shots": [
            {
                **copy.deepcopy(base_shot),
                "shot_id": f"g01_s{index:02d}",
                "timeline": {"start_us": (index - 1) * 3_000_000, "end_us": index * 3_000_000},
            }
            for index in range(1, 7)
        ],
    }

    fallback = build_cinematic_storyboard_fallback(lock)
    assert all(
        item["visual_direction"]["composition"] != "多人层级"
        for item in fallback["shot_contexts"]
    )


def test_real_17_first_frame_replay_keeps_order_and_emits_cell_audits() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "outputs" / "director_story_reviews" / "4e3fcec9fc464cd2bb331e9e6a395bc2" / "shot_video_script.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    material_inputs = json.loads(source.with_name("material_inputs.json").read_text(encoding="utf-8"))
    style_reference_images = material_inputs["style_reference_images"]
    assert len(style_reference_images) == 4
    source_shots = [item for item in data["shot_video_script"] if item.get("first_frame_prompt")]
    assert len(source_shots) == 17

    replay_shots = []
    for item in source_shots:
        compiled = compile_first_frame_prompt_audit(item, aspect_ratio=data.get("aspect_ratio") or "16:9")
        replay_shots.append({
            "shot_id": item["shot_id"],
            "compiled_first_frame_prompt": compiled["prompt"],
            "first_frame_prompt": item["first_frame_prompt"],
            "semantic_anchor": item.get("semantic_anchor", ""),
            "motion_seed": item.get("motion_seed", ""),
            "camera_motion": item.get("camera_motion", []),
            "shot_size": item.get("shot_size", ""),
            "viewpoint": item.get("viewpoint", ""),
            "composition": item.get("composition", ""),
            "ref_images": list((item.get("first_frame_reference") or {}).get("urls") or []) or style_reference_images,
        })

    plan = build_first_frame_grid_plan(replay_shots, grid_layout="3x3", aspect_ratio=data.get("aspect_ratio") or "16:9")
    cells = [cell for batch in plan["batches"] for cell in batch["cells"]]
    final_prompt = "\n".join(batch["prompt"] for batch in plan["batches"])

    assert [cell["shot_id"] for cell in cells] == [item["shot_id"] for item in source_shots]
    assert len(cells) == 17
    assert all("source_cell_prompt" in cell["static_frame_audit"] for cell in cells)
    assert all("warnings" in cell["static_frame_audit"] for cell in cells)
    assert all(len(cell["reference_images"]) == 4 for cell in cells)
    assert "自行车/街道场景" not in final_prompt
    assert "scene_02" not in final_prompt
    assert final_prompt.count("【镜头动势】") == 0
    assert "后停住" not in final_prompt
