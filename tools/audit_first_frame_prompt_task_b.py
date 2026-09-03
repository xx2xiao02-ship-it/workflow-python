"""Task B 离线审计：只读取历史首帧数据，不调用任何图片或 LLM API。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_1256.asset_generation_contracts import compile_first_frame_prompt_audit
from workflow_1256.first_frame_grid import build_first_frame_grid_plan
from workflow_1256.first_frame_prompt_reviewer import (
    LightweightImagePromptReviewer,
    build_reviewer_input,
    detect_deterministic_risks,
)


DEFAULT_STORY_TASK_ID = "4e3fcec9fc464cd2bb331e9e6a395bc2"


def _review_fields(item: dict[str, object], compiled_prompt: str) -> dict[str, object]:
    return {
        "position": item.get("position", ""),
        "scene": item.get("scene", ""),
        "characters": item.get("characters", item.get("character_names", [])),
        "character_count": item.get("character_count"),
        "key_props": item.get("key_props", item.get("key_prop", [])),
        "shot_size": item.get("shot_size", ""),
        "camera_angle": item.get("camera_angle", item.get("viewpoint", "")),
        "composition": item.get("composition", ""),
        "frame_intent": item.get("frame_intent", item.get("semantic_anchor", "")),
        "dominant_action": item.get("dominant_action", ""),
        "lighting": item.get("lighting", ""),
        "compiled_first_frame_prompt": compiled_prompt,
    }


def _injected_cases() -> list[dict[str, object]]:
    return [
        {"name": "B1_subject", "expected": "SUBJECT_CONFLICT", "input": {"scene": "菜场摊位前", "characters": ["周姐"], "character_count": 1, "key_props": ["招牌"], "compiled_first_frame_prompt": "菜场摊位前。周姐推招牌，陈岭摸铁盒。"}},
        {"name": "B2_scene", "expected": "SCENE_CONFLICT", "input": {"scene": "居民楼路边", "characters": ["陈岭"], "character_count": 1, "compiled_first_frame_prompt": "居民楼路边。坐在室内餐桌旁。"}},
        {"name": "B3_undefined_subject", "expected": "SUBJECT_CONFLICT", "input": {"scene": "居民楼路边", "characters": ["陈岭", "父亲"], "character_count": 2, "compiled_first_frame_prompt": "居民楼路边。陈岭和父亲看向周姐。"}},
        {"name": "B4_prop", "expected": "PROP_CONFLICT", "input": {"scene": "深夜家中桌前", "characters": ["陈岭"], "character_count": 1, "key_props": ["舞鞋"], "compiled_first_frame_prompt": "深夜家中桌前。陈岭拿着舞鞋和现金信封。"}},
        {"name": "B5_multi_action", "expected": "MULTI_ACTION_SEMANTIC_CONFLICT", "input": {"scene": "菜场", "characters": ["陈岭"], "character_count": 1, "key_props": ["铁盒"], "dominant_action": "按住铁盒，同时站起，然后转身走向门口", "compiled_first_frame_prompt": "菜场。按住铁盒，同时站起，然后转身走向门口。"}},
        {"name": "B6_normal", "expected": "PASS", "input": {"scene": "居民楼路边", "characters": ["陈岭", "父亲"], "character_count": 2, "key_props": ["舞鞋"], "compiled_first_frame_prompt": "居民楼路边。陈岭停在父亲身侧。画面采用远景，以高机位视角拍摄，光线保持侧向主光。"}},
    ]


def _audit(source: Path) -> dict[str, object]:
    data = json.loads(source.read_text(encoding="utf-8"))
    material_inputs_path = source.with_name("material_inputs.json")
    material_inputs = json.loads(material_inputs_path.read_text(encoding="utf-8"))
    style_refs = [str(value) for value in material_inputs.get("style_reference_images", []) if str(value).strip()]
    source_shots = [item for item in data.get("shot_video_script", []) if item.get("first_frame_prompt")]
    aspect_ratio = str(data.get("aspect_ratio") or "16:9")
    replay_shots: list[dict[str, object]] = []
    for item in source_shots:
        compiled = compile_first_frame_prompt_audit(item, aspect_ratio=aspect_ratio)
        replay_shots.append({
            **_review_fields(item, str(compiled["prompt"])),
            "shot_id": item.get("shot_id"),
            "compiled_first_frame_prompt": compiled["prompt"],
            "first_frame_prompt": item.get("first_frame_prompt"),
            "semantic_anchor": item.get("semantic_anchor", ""),
            "motion_seed": item.get("motion_seed", ""),
            "camera_motion": item.get("camera_motion", []),
            "ref_images": list((item.get("first_frame_reference") or {}).get("urls") or []) or style_refs,
        })
    plan = build_first_frame_grid_plan(replay_shots, grid_layout="3x3", aspect_ratio=aspect_ratio)
    cells = [cell for batch in plan["batches"] for cell in batch["cells"]]
    normal_records = [
        {
            "shot_id": cell["shot_id"],
            "review_status": cell.get("review_status"),
            "review_requested": cell.get("review_requested"),
            "review_risk_score": cell.get("review_risk_score"),
            "review_issues": json.loads(cell.get("review_issues_json") or "[]"),
            "compiled_first_frame_prompt": cell.get("compiled_first_frame_prompt"),
            "compiled_cell_prompt": cell.get("compiled_cell_prompt"),
            "final_first_frame_prompt": cell.get("final_first_frame_prompt"),
        }
        for cell in cells
    ]
    injected = []
    for case in _injected_cases():
        review_input = build_reviewer_input(case["input"])
        risk_score, issues = detect_deterministic_risks(review_input)
        injected.append({
            "name": case["name"],
            "expected_code": case["expected"],
            "detected_codes": [item["code"] for item in issues],
            "risk_score": risk_score,
            "pass": case["expected"] == "PASS" and not issues or case["expected"] != "PASS" and case["expected"] in {item["code"] for item in issues},
        })
    summary = dict(plan.get("review_summary") or {})
    summary.update({
        "story_task_id": DEFAULT_STORY_TASK_ID,
        "source_first_frame_shot_count": len(source_shots),
        "style_reference_image_count": len(style_refs),
        "mode": "offline_only_no_provider_calls",
        "provider": "offline_deterministic",
        "model": "not_invoked",
        "model_version": "task-b-v1",
        "normal_prompt_byte_change_count": sum(
            1 for item in normal_records if item["compiled_cell_prompt"] != item["final_first_frame_prompt"]
        ),
        "normal_records": normal_records,
        "injected_cases": injected,
        "grid_batches": [
            {"grid_id": batch["grid_id"], "grid_layout": batch["grid_layout"], "cell_shot_ids": [cell["shot_id"] for cell in batch["cells"]]}
            for batch in plan["batches"]
        ],
        "estimated_cost": 0.0,
        "notes": "默认 RISK_ONLY；本审计使用确定性风险检测器，未配置 provider adapter，因此没有真实 Reviewer 调用，也没有费用。",
    })
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--story-task-id", default=DEFAULT_STORY_TASK_ID)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = ROOT / "outputs" / "director_story_reviews" / args.story_task_id / "shot_video_script.json"
    report = _audit(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("total_cells", "review_requested", "pass_count", "repaired_count", "needs_review_count", "error_count", "injected_cases")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
