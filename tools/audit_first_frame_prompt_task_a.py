"""Offline Task A replay for persisted first-frame prompt data.

It reads a historical shot script, compiles prompts deterministically, and
writes a separate audit report.  It does not call image/video providers or
modify the source task artifact.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_1256.asset_generation_contracts import compile_first_frame_prompt_audit
from workflow_1256.first_frame_grid import build_first_frame_grid_plan


DEFAULT_STORY_TASK_ID = "4e3fcec9fc464cd2bb331e9e6a395bc2"


def _replay(source: Path) -> dict[str, object]:
    data = json.loads(source.read_text(encoding="utf-8"))
    material_inputs_path = source.with_name("material_inputs.json")
    material_inputs = json.loads(material_inputs_path.read_text(encoding="utf-8"))
    style_reference_images = [
        str(value) for value in material_inputs.get("style_reference_images", []) if str(value).strip()
    ]
    source_shots = [item for item in data["shot_video_script"] if item.get("first_frame_prompt")]
    aspect_ratio = str(data.get("aspect_ratio") or "16:9")
    replay_shots: list[dict[str, object]] = []
    compiled_by_id: dict[str, dict[str, object]] = {}
    for item in source_shots:
        compiled = compile_first_frame_prompt_audit(item, aspect_ratio=aspect_ratio)
        shot_id = str(item["shot_id"])
        compiled_by_id[shot_id] = compiled
        replay_shots.append({
            "shot_id": shot_id,
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
    plan = build_first_frame_grid_plan(
        replay_shots,
        grid_layout="3x3",
        aspect_ratio=aspect_ratio,
    )
    cell_by_id = {
        str(cell["shot_id"]): cell
        for batch in plan["batches"]
        for cell in batch["cells"]
    }
    records = []
    for source_shot in source_shots:
        shot_id = str(source_shot["shot_id"])
        compiled = compiled_by_id[shot_id]
        cell = cell_by_id[shot_id]
        before = str(compiled["source_prompt"])
        after = str(cell["compiled_cell_prompt"])
        warnings = list(dict.fromkeys([
            *[str(value) for value in compiled["warnings"]],
            *[str(value) for value in cell["static_frame_audit"]["warnings"]],
        ]))
        records.append({
            "shot_id": shot_id,
            "before_prompt": before,
            "after_prompt": after,
            "diff": list(difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                fromfile=f"{shot_id}:before", tofile=f"{shot_id}:after", lineterm="",
            )),
            "warnings": warnings,
            "reference_image_count": len(cell["reference_images"]),
        })
    return {
        "task": "first_frame_prompt_governance_task_a",
        "mode": "offline_only_no_provider_calls",
        "source": str(source),
        "aspect_ratio": aspect_ratio,
        "first_frame_shot_count": len(source_shots),
        "style_reference_image_count": len(style_reference_images),
        "shot_ids": [record["shot_id"] for record in records],
        "records": records,
        "grid_batches": [
            {
                "grid_id": batch["grid_id"],
                "grid_layout": batch["grid_layout"],
                "reference_image_count": len(batch["reference_images"]),
                "cell_shot_ids": [cell["shot_id"] for cell in batch["cells"]],
            }
            for batch in plan["batches"]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--story-task-id", default=DEFAULT_STORY_TASK_ID)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = ROOT / "outputs" / "director_story_reviews" / args.story_task_id / "shot_video_script.json"
    report = _replay(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "first_frame_shot_count": report["first_frame_shot_count"],
        "shot_ids": report["shot_ids"],
        "grid_batches": report["grid_batches"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
