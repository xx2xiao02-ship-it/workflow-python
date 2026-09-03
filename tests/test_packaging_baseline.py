from __future__ import annotations

import json
import shutil
from pathlib import Path

from workflow_1256.packaging_baseline import (
    create_unwrapped_baseline_from_packaged,
    register_baseline_snapshot,
    resolve_baseline_for_packaged,
    script_fingerprint,
)


def _write_packaged(path: Path) -> Path:
    path.mkdir(parents=True)
    draft = {
        "id": "B-ID",
        "name": "B",
        "duration": 5_000_000,
        "canvas_config": {"width": 720, "height": 1280, "fps": 30},
        "platform": {"os": "windows"},
        "tracks": [
            {
                "id": "video-track",
                "name": "AIGC动画",
                "type": "video",
                "segments": [
                    {
                        "id": "video-segment",
                        "material_id": "video-1",
                        "extra_material_refs": ["speed-1"],
                        "common_keyframes": [{"keyframe_list": [{"time_offset": 0}]}],
                    }
                ],
            },
            {
                "id": "effect-track",
                "name": "包装特效",
                "type": "effect",
                "segments": [{"id": "effect-segment", "material_id": "effect-1"}],
            },
        ],
        "materials": {
            "videos": [{"id": "video-1", "material_name": "shot.mp4"}],
            "speeds": [{"id": "speed-1"}],
            "video_effects": [{"id": "effect-1", "effect_id": "resource-1"}],
            "text_templates": [{"id": "template-1", "name": "标题模板"}],
            "texts": [{"id": "subtitle-1", "type": "subtitle"}, {"id": "title-1", "type": "text"}],
        },
    }
    (path / "draft_content.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return path


def test_create_unwrapped_baseline_strips_packaging_without_touching_b(tmp_path: Path) -> None:
    packaged = _write_packaged(tmp_path / "B")
    before = (packaged / "draft_content.json").read_bytes()
    output_root = tmp_path / "drafts"
    output_root.mkdir()

    manifest = create_unwrapped_baseline_from_packaged(packaged, output_root, draft_name="A_无包装")

    assert manifest["status"] == "generated_candidate"
    baseline_path = Path(manifest["baseline_a"]["draft_content_path"])
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert not any(track.get("type") == "effect" for track in baseline["tracks"])
    assert baseline["tracks"][0]["segments"][0]["common_keyframes"] == []
    assert baseline["materials"]["video_effects"] == []
    assert all(item.get("type") != "text" for item in baseline["materials"]["texts"])
    assert (packaged / "draft_content.json").read_bytes() == before


def test_internal_baseline_snapshot_survives_a_being_edited_into_b(tmp_path: Path) -> None:
    packaged = _write_packaged(tmp_path / "B")
    output_root = tmp_path / "drafts"
    output_root.mkdir()
    manifest = create_unwrapped_baseline_from_packaged(packaged, output_root, draft_name="A_无包装")
    baseline_dir = Path(manifest["baseline_a"]["draft_dir"])
    baseline_before = (baseline_dir / "draft_content.json").read_bytes()
    registry = tmp_path / "registry"

    registered = register_baseline_snapshot(baseline_dir, registry, packaged_source=packaged)
    # 模拟用户在剪映中把原始 A 直接继续编辑成 B；外部草稿不再是基线。
    shutil.copy2(packaged / "draft_content.json", baseline_dir / "draft_content.json")

    assert script_fingerprint(baseline_dir)["fingerprint"] == script_fingerprint(packaged)["fingerprint"]
    resolved = resolve_baseline_for_packaged(packaged, registry, search_roots=[output_root])

    assert resolved["status"] == "matched"
    snapshot_path = Path(resolved["baseline"]["draft_content_path"])
    assert snapshot_path.parent != baseline_dir
    assert snapshot_path.read_bytes() == baseline_before
    assert Path(registered["baseline_a"]["snapshot_dir"]).is_dir()


def test_relaxed_match_allows_packaging_changes_to_a_draft_name(tmp_path: Path) -> None:
    packaged = _write_packaged(tmp_path / "B")
    output_root = tmp_path / "drafts"
    output_root.mkdir()
    manifest = create_unwrapped_baseline_from_packaged(packaged, output_root, draft_name="包装抓取基准_A")
    baseline_dir = Path(manifest["baseline_a"]["draft_dir"])
    registry = tmp_path / "registry"
    register_baseline_snapshot(baseline_dir, registry, packaged_source=packaged)

    modified = tmp_path / "包装抓取基准_A_已修改为B"
    shutil.copytree(baseline_dir, modified)
    content_path = modified / "draft_content.json"
    content = json.loads(content_path.read_text(encoding="utf-8"))
    content["duration"] = int(content.get("duration") or 0) + 1_000
    content["tracks"].append({"type": "effect", "name": "包装-转场", "segments": [{"id": "effect-segment"}]})
    content["materials"]["video_effects"] = [{"id": "effect-1", "effect_id": "resource-1"}]
    content_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    resolved = resolve_baseline_for_packaged(modified, registry, search_roots=[output_root])

    assert resolved["status"] == "matched"
    assert resolved["match_method"] == "internal_registry_relaxed"
