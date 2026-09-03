from __future__ import annotations

import copy
import json

from workflow_1256.editing_style_package import (
    build_editing_style_package,
    normalize_draft,
    sanitize_cli_evidence,
)


def _draft() -> dict:
    text = json.dumps({
        "text": "不应出现在风格包里的原始字幕",
        "styles": [{
            "range": [0, 14],
            "size": 8.0,
            "bold": True,
            "fill": {"content": {"render_type": "solid", "solid": {"color": [1, 1, 1]}}},
            "strokes": [],
        }],
    }, ensure_ascii=False)
    return {
        "canvas_config": {"width": 1080, "height": 1920, "ratio": "9:16"},
        "fps": 30,
        "duration": 6_000_000,
        "platform": {"os": "windows", "app_version": "5.9.0", "app_source": "lv"},
        "last_modified_platform": {"os": "windows"},
        "materials": {
            "videos": [{"id": "random-video-id", "path": "D:\\secret\\video.mp4", "type": "video"}],
            "audios": [{"id": "random-audio-id", "path": "D:\\secret\\audio.mp3", "type": "extract_music"}],
            "texts": [{"id": "random-text-id", "content": text, "alignment": 1, "line_spacing": 0.02, "line_max_width": 0.84, "type": "subtitle"}],
            "transitions": [{"id": "random-transition-id", "name": "叠化", "duration": 500000}],
        },
        "tracks": [
            {"name": "视频", "type": "video", "segments": [{"id": "random-segment-id", "material_id": "random-video-id", "target_timerange": {"start": 0, "duration": 6_000_000}, "speed": 1.0, "volume": 1.0, "clip": {"scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0, "y": 0}}}]},
            {"name": "字幕", "type": "text", "segments": [{"id": "random-caption-segment", "material_id": "random-text-id", "target_timerange": {"start": 0, "duration": 6_000_000}}]},
        ],
    }


def test_normalize_draft_excludes_media_paths_text_and_random_ids() -> None:
    normalized = normalize_draft(_draft())
    serialized = json.dumps(normalized, ensure_ascii=False)
    assert "secret" not in serialized
    assert "原始字幕" not in serialized
    assert "random-video-id" not in serialized
    assert normalized["canvas"] == {"width": 1080, "height": 1920, "fps": 30, "ratio": "9:16"}
    assert normalized["rhythm"]["video_cut_durations"]["count"] == 1
    assert normalized["subtitle_style"]["profiles"][0]["profile"]["style_variants"][0]["bold"] is True


def test_build_package_attributes_edit_to_changed_subtitle_style() -> None:
    baseline = _draft()
    edited = copy.deepcopy(baseline)
    edited["materials"]["texts"][0]["alignment"] = 0
    edited["materials"]["texts"][0]["line_max_width"] = 0.72
    package = build_editing_style_package(baseline, edited, baseline_source="baseline", edited_source="edited", backend="raw-draft")
    assert package["source"]["comparison"] == "baseline_and_edited"
    sections = {item["section"] for item in package["changes"]["changed_sections"]}
    assert "subtitle_style" in sections
    assert package["changes"]["status"] == "attributed"


def test_sanitize_cli_evidence_keeps_summary_only() -> None:
    evidence = sanitize_cli_evidence({
        "operations": {
            "version": {"version": "0.19.0"},
            "info": {"name": "demo", "duration_us": 100, "tracks": 2, "project_id": "random"},
            "tracks": [{"index": 0, "name": "视频", "segments": 1, "id": "random"}],
            "materials": [{"type": "videos", "count": 1, "path": "secret"}],
            "lint": {"ok": True, "summary": {"errors": 0, "warnings": 1}, "issues": [{"path": "secret"}]},
            "texts": [{"text": "secret"}],
        }
    })
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert evidence["version"] == "0.19.0"
    assert evidence["texts"] == {"count": 1, "has_text_output": True}
    assert "secret" not in serialized
