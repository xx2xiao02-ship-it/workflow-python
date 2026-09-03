from __future__ import annotations

import json
import copy
from pathlib import Path

import pytest

from workflow_1256.packaging_capture import (
    PackagingCaptureError,
    build_parameter_catalog,
    capture_packaging_pair,
    infer_track_roles,
    write_capture_artifact,
)
from workflow_1256.packaging_replay import write_replay_artifact


def _write_draft(path: Path, *, packaged: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    tracks = [
        {
            "name": "BGM",
            "type": "audio",
            "segments": [{
                "material_id": "audio-1",
                "volume": 0.22 if not packaged else 0.18,
                "target_timerange": {"start": 0, "duration": 5_000_000},
            }],
        },
        {
            "name": "音效",
            "type": "audio",
            "segments": [{
                "material_id": "audio-2",
                "volume": 1.0,
                "target_timerange": {"start": 2_000_000, "duration": 500_000},
                "is_sound_effect": True,
            }],
        },
        {
            "name": "AIGC动画",
            "type": "video",
            "segments": [{
                "material_id": "video-1",
                "target_timerange": {"start": 0, "duration": 5_000_000},
                "common_keyframes": [{
                    "keyframe_list": [{"time_offset": 0, "values": [1.0]}]
                }],
            }],
        },
    ]
    materials = {
        "audios": [
            {"id": "audio-1", "name": "BGM", "type": "music"},
            {"id": "audio-2", "name": "whoosh", "type": "sound"},
        ],
        "videos": [{"id": "video-1", "name": "shot-1", "path": "D:/media/shot.mp4"}],
        "transitions": [],
        "video_effects": [],
        "filters": [],
        "texts": [{
            "id": "text-1",
            "font_title": "微软雅黑",
            "font_size": 30 if not packaged else 34,
            "has_shadow": True,
        }],
    }
    if packaged:
        materials["video_effects"] = [{
            "id": "effect-1",
            "effect_id": "effect-vignette",
            "name": "暗角",
            "apply_time_range": {"start": 0, "duration": 5_000_000},
            "intensity": 0.6,
        }]
        tracks.append({
            "name": "包装特效",
            "type": "effect",
            "segments": [{
                "effect_id": "effect-1",
                "target_timerange": {"start": 0, "duration": 5_000_000},
            }],
        })
    draft = {
        "name": path.name,
        "duration": 5_000_000,
        "canvas_config": {"width": 720, "height": 1280, "fps": 30},
        "platform": {"os": "windows", "app_version": "test"},
        "tracks": tracks,
        "materials": materials,
    }
    (path / "draft_content.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return path


def test_capture_pair_keeps_raw_changes_and_semantic_parameters(tmp_path: Path) -> None:
    baseline = _write_draft(tmp_path / "A_无包装")
    packaged = _write_draft(tmp_path / "B_人工包装", packaged=True)

    result = capture_packaging_pair(
        baseline,
        packaged,
        track_role_overrides={"1": "transition_sfx"},
    )

    assert result["schema_version"] == "packaging-capture-v1"
    assert result["raw_diff"]["change_count"] > 0
    assert result["raw_diff"]["truncated"] is False
    assert result["semantic_diff"]["status"] == "changed"
    assert result["track_roles"]["tracks"][1]["suggested_role"] == "transition_sfx"
    assert result["track_roles"]["needs_confirmation"] is False
    assert result["compatibility"]["status"] == "compatible"
    assert result["compatibility"]["warnings"] == []
    assert result["candidate_package"]["write_policy"] == "candidate_only_no_production_write"
    assert result["candidate_package"]["parameter_catalog_file"] == "parameter_catalog.json"
    assert result["candidate_package"]["parameters"]["audio_tracks"]
    assert result["candidate_package"]["parameters"]["subtitle_profiles"]


def test_parameter_catalog_separates_narration_as_audio_variable_track() -> None:
    draft = {
        "duration": 8_000_000,
        "tracks": [{
            "name": "解说",
            "type": "audio",
            "segments": [{
                "material_id": "narration-1",
                "target_timerange": {"start": 0, "duration": 3_000_000},
                "volume": 0.85,
            }],
        }],
        "materials": {"audios": [{"id": "narration-1", "name": "解说.wav", "type": "voice"}]},
    }

    catalog = build_parameter_catalog(
        draft,
        track_roles={"tracks": [{"track_name": "解说", "suggested_role": "narration"}]},
    )

    assert catalog["categories"]["variable"]["track_count"] == 1
    assert catalog["categories"]["variable"]["tracks"][0]["purpose"].startswith("音频变量轨：")
    assert catalog["categories"]["global_audio"]["track_count"] == 0


def test_audio_role_inference_uses_segment_material_names() -> None:
    roles = infer_track_roles({
        "audio": {
            "tracks": [
                {
                    "name": "audio_track_1",
                    "track_role": "sound_effect",
                    "segments": [{"material_name": "“呼”的转场音效"}],
                },
                {
                    "name": "audio_track_2",
                    "track_role": "sound_effect",
                    "segments": [{"material_name": "快速翻页声"}],
                },
            ],
        },
    })

    assert [item["suggested_role"] for item in roles["tracks"]] == [
        "transition_sfx",
        "visual_enhancement_sfx",
    ]
    assert roles["needs_confirmation"] is False


def test_mixed_audio_track_keeps_segment_level_roles() -> None:
    catalog = build_parameter_catalog(
        {
            "duration": 4_000_000,
            "tracks": [{
                "name": "audio_track_1",
                "type": "audio",
                "segments": [
                    {"material_id": "s1", "target_timerange": {"start": 0, "duration": 500_000}},
                    {"material_id": "s2", "target_timerange": {"start": 500_000, "duration": 500_000}},
                ],
            }],
            "materials": {"audios": [
                {"id": "s1", "name": "“呼”的转场音效", "type": "sound"},
                {"id": "s2", "name": "敲击键盘声", "type": "sound"},
            ]},
        },
        track_roles={"tracks": [{
            "track_name": "音效 · “呼”的转场音效、敲击键盘声",
            "source_track_index": 0,
            "suggested_role": "transition_sfx",
        }]},
    )

    rows = catalog["track_lanes"][0]["segments"]
    assert [row["audio_role"] for row in rows] == ["transition_sfx", "visual_enhancement_sfx"]


def test_capture_artifact_isolated_and_does_not_overwrite(tmp_path: Path) -> None:
    baseline = _write_draft(tmp_path / "A")
    packaged = _write_draft(tmp_path / "B", packaged=True)
    result = capture_packaging_pair(baseline, packaged)
    output = tmp_path / "capture"

    artifact = write_capture_artifact(result, output)

    assert (output / "capture_result.json").is_file()
    assert (output / "raw_diff.json").is_file()
    assert (output / "candidate_package.json").is_file()
    assert (output / "parameter_catalog.json").is_file()
    assert "candidate_package.json" in artifact["files"]
    assert "parameter_catalog.json" in artifact["files"]
    assert (baseline / "draft_content.json").is_file()
    assert (packaged / "draft_content.json").is_file()


def test_replay_builds_c_from_a_and_verifies_against_b(tmp_path: Path) -> None:
    baseline = _write_draft(tmp_path / "A")
    packaged = _write_draft(tmp_path / "B", packaged=True)
    before_a = (baseline / "draft_content.json").read_bytes()
    before_b = (packaged / "draft_content.json").read_bytes()
    result = capture_packaging_pair(baseline, packaged)
    output = tmp_path / "capture"
    write_capture_artifact(result, output)

    replay = write_replay_artifact(baseline, packaged, result, output)

    assert replay["status"] == "verified"
    assert replay["verification"]["raw_change_count"] == 0
    assert replay["verification"]["semantic_change_count"] == 0
    assert Path(replay["acceptance_report_path"]).is_file()
    assert replay["merge"]["added_tracks"] == 1
    assert replay["merge"]["added_materials"]["video_effects"] == 1
    replay_draft = json.loads(Path(replay["replay_draft_content_path"]).read_text(encoding="utf-8"))
    assert any(track.get("type") == "effect" for track in replay_draft["tracks"])
    assert (baseline / "draft_content.json").read_bytes() == before_a
    assert (packaged / "draft_content.json").read_bytes() == before_b


def test_capture_marks_same_count_content_change_for_review(tmp_path: Path) -> None:
    baseline = _write_draft(tmp_path / "A")
    packaged = _write_draft(tmp_path / "B", packaged=True)
    content_path = packaged / "draft_content.json"
    content = json.loads(content_path.read_text(encoding="utf-8"))
    content["materials"]["videos"][0]["name"] = "changed-content-shot"
    content_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    result = capture_packaging_pair(baseline, packaged)

    assert result["compatibility"]["status"] == "review_required"
    assert any("内容签名" in warning for warning in result["compatibility"]["warnings"])
    assert result["status"] == "pending_review"


def test_replay_applies_reviewed_relative_keyframe_parameters(tmp_path: Path) -> None:
    baseline = _write_draft(tmp_path / "A")
    packaged = _write_draft(tmp_path / "B", packaged=True)
    content_path = packaged / "draft_content.json"
    content = json.loads(content_path.read_text(encoding="utf-8"))
    # A/B 只改变内容轨上的关键帧，保持同源脚本兼容。
    content["tracks"][2]["segments"][0]["common_keyframes"][0]["keyframe_list"][0]["values"] = [1.35]
    content_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    result = capture_packaging_pair(baseline, packaged)
    reviewed = copy.deepcopy(result["candidate_package"]["parameters"])
    assert reviewed.get("keyframes")
    reviewed["keyframes"][0]["keyframes"][0]["points"][0]["values"] = [1.5]
    output = tmp_path / "capture"

    replay = write_replay_artifact(
        baseline,
        packaged,
        result,
        output,
        reviewed_parameters=reviewed,
    )

    assert replay["parameter_review"]["applied_group_count"] > 0
    replay_content = json.loads((output / "replay_C" / "draft_content.json").read_text(encoding="utf-8"))
    assert replay_content["tracks"][2]["segments"][0]["common_keyframes"][0]["keyframe_list"][0]["values"] == [1.5]


def test_parameter_catalog_groups_tracks_by_shot_category() -> None:
    draft = {
        "name": "B",
        "duration": 8_000_000,
        "tracks": [
            {
                "name": "内容视频",
                "type": "video",
                "segments": [
                    {"material_id": "dh", "target_timerange": {"start": 0, "duration": 2_000_000}},
                    {"material_id": "aigc", "target_timerange": {"start": 2_000_000, "duration": 2_000_000}},
                ],
            },
            {
                "name": "包装-片头-暗角",
                "type": "effect",
                "segments": [{"material_id": "effect", "target_timerange": {"start": 0, "duration": 1_000_000}}],
            },
            {
                "name": "BGM",
                "type": "audio",
                "segments": [{"material_id": "music", "volume": 0.2, "target_timerange": {"start": 0, "duration": 8_000_000}}],
            },
        ],
        "materials": {
            "videos": [
                {"id": "dh", "material_name": "asset-g01_s01-digital-human.mp4", "type": "video"},
                {"id": "aigc", "material_name": "asset-g01_s02-aigc-video.mp4", "type": "video"},
            ],
            "video_effects": [{"id": "effect", "name": "暗角", "effect_id": "e1", "adjust_params": [{"name": "强度", "value": 0.6}]}],
            "audios": [{"id": "music", "name": "bgm.mp3", "type": "music"}],
        },
    }

    catalog = build_parameter_catalog(draft)

    assert catalog["schema_version"] == "packaging-parameter-catalog-v1"
    assert catalog["categories"]["opening"]["track_count"] == 1
    assert catalog["categories"]["digital_human"]["track_count"] == 1
    assert catalog["categories"]["aigc"]["track_count"] == 1
    assert catalog["categories"]["global_audio"]["tracks"][0]["parameter_summary"]["volume"]["average_percent"] == 20.0
    assert catalog["categories"]["opening"]["tracks"][0]["parameter_summary"]["parameter_groups"]["effects"][0]["parameters"]["adjust_params"][0]["value"] == 0.6


def test_variable_layer_uses_internal_a_with_timing_tolerance() -> None:
    baseline = {
        "name": "A_无包装",
        "duration": 8_000_000,
        "tracks": [{
            "name": "",
            "type": "text",
            "segments": [{
                "text_template_id": "title-template",
                "target_timerange": {"start": 0, "duration": 1_000_000},
            }],
        }],
        "materials": {
            "text_templates": [{"id": "title-template", "name": "主标题模板"}],
        },
    }
    packaged = copy.deepcopy(baseline)
    packaged["name"] = "B_人工包装"
    # 手工拖动后产生 150ms 的偏移，仍应绑定到 A 的同一变量模板。
    packaged["tracks"][0]["segments"][0]["target_timerange"]["start"] = 150_000

    catalog = build_parameter_catalog(packaged, baseline_draft=baseline)
    row = catalog["track_lanes"][0]["segments"][0]

    assert row["baseline_variable_match"] is True
    assert row["baseline_variable_role"] == "main_title"
    assert catalog["variable_baseline"]["source"] == "baseline_A"
    assert catalog["variable_baseline"]["alignment_tolerance_us"] == 250_000


def test_text_and_narration_stay_on_variable_axis_not_opening_segments() -> None:
    draft = {
        "name": "B_音频变量",
        "duration": 20_000_000,
        "tracks": [
            {
                "name": "包装-片头-闪白",
                "type": "effect",
                "segments": [{"effect_id": "effect-1", "target_timerange": {"start": 0, "duration": 500_000}}],
            },
            {
                "name": "片头音效",
                "type": "audio",
                "segments": [{"material_id": "sfx-1", "target_timerange": {"start": 100_000, "duration": 300_000}}],
            },
            {
                "name": "解说",
                "type": "audio",
                "segments": [{"material_id": "voice-1", "target_timerange": {"start": 0, "duration": 1_000_000}}],
            },
            {
                "name": "主标题",
                "type": "text",
                "segments": [{"text_template_id": "title-1", "target_timerange": {"start": 100_000, "duration": 600_000}}],
            },
        ],
        "materials": {
            "video_effects": [{"id": "effect-1", "name": "闪白", "effect_id": "flash"}],
            "audios": [
                {"id": "sfx-1", "name": "片头转场音效", "type": "sound"},
                {"id": "voice-1", "name": "解说.wav", "type": "voice"},
            ],
            "text_templates": [{"id": "title-1", "name": "主标题模板"}],
        },
    }

    catalog = build_parameter_catalog(
        draft,
        track_roles={"tracks": [
            {"track_name": "片头音效", "source_track_index": 1, "suggested_role": "transition_sfx"},
            {"track_name": "解说", "source_track_index": 2, "suggested_role": "narration"},
        ]},
    )

    variable_lanes = [lane for lane in catalog["track_lanes"] if lane["segments"] and lane["segments"][0]["category"] == "variable"]
    assert {lane["track_type"] for lane in variable_lanes} == {"audio", "text"}
    assert {lane["segments"][0]["variable_kind"] for lane in variable_lanes} == {"audio", "text"}
    opening = next(item for item in catalog["composition_patterns"]["groups"] if item["group_id"] == "opening_composition_1")
    assert {item["role"] for item in opening["members"]} == {"visual", "transition_sfx", "narration_variable", "auto"}
    assert opening["audio_packaging_member_count"] == 1


def test_composition_requires_visual_audio_or_variable_overlap() -> None:
    draft = {
        "name": "B_组合",
        "duration": 10_000_000,
        "tracks": [
            {
                "name": "包装-片头-闪白",
                "type": "effect",
                "segments": [{"effect_id": "effect-1", "target_timerange": {"start": 0, "duration": 1_000_000}}],
            },
            {
                "name": "片头音效",
                "type": "audio",
                "segments": [{"material_id": "sfx-1", "target_timerange": {"start": 100_000, "duration": 500_000}}],
            },
            {
                "name": "主标题变量",
                "type": "text",
                "segments": [{"text_template_id": "title-template", "target_timerange": {"start": 200_000, "duration": 800_000}}],
            },
        ],
        "materials": {
            "video_effects": [{"id": "effect-1", "name": "闪白", "effect_id": "flash"}],
            "audios": [{"id": "sfx-1", "name": "片头转场音效", "type": "sound"}],
            "text_templates": [{"id": "title-template", "name": "主标题模板"}],
        },
    }

    catalog = build_parameter_catalog(
        draft,
        track_roles={"tracks": [{"track_name": "片头音效", "source_track_index": 1, "suggested_role": "transition_sfx"}]},
    )
    opening = next(item for item in catalog["composition_patterns"]["boundary_windows"] if item["category"] == "opening")

    assert opening["status"] == "composite_confirmed"
    assert set(opening["layer_types"]) == {"visual", "audio", "variable"}
    assert catalog["composition_patterns"]["alignment_tolerance_us"] == 250_000


def test_composition_excludes_global_effect_bgm_and_narration() -> None:
    draft = {
        "name": "B_局部组合",
        "duration": 10_000_000,
        "tracks": [
            {
                "name": "全局暗角",
                "type": "effect",
                "segments": [{"effect_id": "global-effect", "target_timerange": {"start": 0, "duration": 10_000_000}}],
            },
            {
                "name": "包装-片头-闪白",
                "type": "effect",
                "segments": [{"effect_id": "opening-effect", "target_timerange": {"start": 0, "duration": 800_000}}],
            },
            {
                "name": "BGM",
                "type": "audio",
                "segments": [{"material_id": "bgm-1", "target_timerange": {"start": 0, "duration": 10_000_000}}],
            },
            {
                "name": "解说",
                "type": "audio",
                "segments": [{"material_id": "voice-1", "target_timerange": {"start": 0, "duration": 4_000_000}}],
            },
            {
                "name": "片头音效",
                "type": "audio",
                "segments": [{"material_id": "sfx-1", "target_timerange": {"start": 100_000, "duration": 500_000}}],
            },
        ],
        "materials": {
            "video_effects": [
                {"id": "global-effect", "name": "暗角", "effect_id": "vignette"},
                {"id": "opening-effect", "name": "闪白", "effect_id": "flash"},
            ],
            "audios": [
                {"id": "bgm-1", "name": "bgm.mp3", "type": "music"},
                {"id": "voice-1", "name": "解说.wav", "type": "voice"},
                {"id": "sfx-1", "name": "片头转场音效", "type": "sound"},
            ],
        },
    }

    catalog = build_parameter_catalog(
        draft,
        track_roles={"tracks": [
            {"track_name": "BGM", "source_track_index": 2, "suggested_role": "bgm"},
            {"track_name": "解说", "source_track_index": 3, "suggested_role": "narration"},
            {"track_name": "片头音效", "source_track_index": 4, "suggested_role": "transition_sfx"},
        ]},
    )
    opening_group = next(item for item in catalog["composition_patterns"]["groups"] if item["group_id"] == "opening_composition_1")

    assert opening_group["time_range_us"] == {"start": 0, "end": 5_000_000}
    assert {item["layer"] for item in opening_group["members"]} == {"visual", "audio", "variable"}
    assert {item["role"] for item in opening_group["members"]} == {"visual", "transition_sfx", "bgm", "narration_variable"}
    assert opening_group["audio_packaging_member_count"] == 1
    assert opening_group["audio_context_member_count"] == 1
    audio_scopes = {item["role"]: item["audio_scope"] for item in opening_group["members"] if item["layer"] == "audio"}
    assert audio_scopes == {"bgm": "context", "transition_sfx": "packaging"}
    assert opening_group["audio_status"] == "packaging_audio_confirmed"


def test_boundary_with_only_context_audio_requires_audio_packaging_confirmation() -> None:
    draft = {
        "name": "B_待确认音频包装",
        "duration": 10_000_000,
        "tracks": [
            {
                "name": "包装-片尾-闪白",
                "type": "effect",
                "segments": [{"effect_id": "ending-effect", "target_timerange": {"start": 9_000_000, "duration": 1_000_000}}],
            },
            {
                "name": "BGM",
                "type": "audio",
                "segments": [{"material_id": "bgm-1", "target_timerange": {"start": 0, "duration": 10_000_000}}],
            },
        ],
        "materials": {
            "video_effects": [{"id": "ending-effect", "name": "闪白", "effect_id": "flash"}],
            "audios": [{"id": "bgm-1", "name": "bgm.mp3", "type": "music"}],
        },
    }

    catalog = build_parameter_catalog(
        draft,
        track_roles={"tracks": [{"track_name": "BGM", "source_track_index": 1, "suggested_role": "bgm"}]},
    )
    ending = next(item for item in catalog["composition_patterns"]["boundary_windows"] if item["category"] == "ending")
    ending_group = next(item for item in catalog["composition_patterns"]["groups"] if item["group_id"] == "ending_composition_1")

    assert ending["status"] == "audio_context_needs_confirmation"
    assert ending["audio_packaging_member_count"] == 0
    assert ending["audio_context_member_count"] == 1
    assert ending_group["audio_status"] == "context_audio_needs_confirmation"


def test_bgm_touching_boundary_is_context_until_manually_marked_as_packaging() -> None:
    draft = {
        "name": "B_BGM边界",
        "duration": 20_000_000,
        "tracks": [
            {
                "name": "包装-片头-闪白",
                "type": "effect",
                "segments": [{"effect_id": "opening-effect", "target_timerange": {"start": 0, "duration": 800_000}}],
            },
            {
                "name": "BGM",
                "type": "audio",
                "segments": [{"material_id": "bgm-1", "target_timerange": {"start": 0, "duration": 8_000_000}}],
            },
        ],
        "materials": {
            "video_effects": [{"id": "opening-effect", "name": "闪白", "effect_id": "flash"}],
            "audios": [{"id": "bgm-1", "name": "bgm.mp3", "type": "music"}],
        },
    }

    catalog = build_parameter_catalog(
        draft,
        track_roles={"tracks": [{"track_name": "BGM", "source_track_index": 1, "suggested_role": "bgm"}]},
    )

    bgm = next(lane for lane in catalog["track_lanes"] if lane["track_name"] == "BGM")["segments"][0]
    opening = next(item for item in catalog["composition_patterns"]["boundary_windows"] if item["category"] == "opening")
    assert bgm["category"] == "global_audio"
    assert bgm["boundary_category"] == ""
    assert bgm["category_source"] == "automatic_inference"
    assert opening["audio_packaging_member_count"] == 0
    assert opening["audio_status"] == "context_audio_needs_confirmation"


def test_image_sequence_requires_three_adjacent_images_and_shared_visual_effect() -> None:
    draft = {
        "name": "B_连续多图",
        "duration": 20_000_000,
        "tracks": [
            {
                "name": "内容图片",
                "type": "video",
                "segments": [
                    {"material_id": "image-1", "target_timerange": {"start": 6_000_000, "duration": 800_000}},
                    {"material_id": "image-2", "target_timerange": {"start": 6_900_000, "duration": 800_000}},
                    {"material_id": "image-3", "target_timerange": {"start": 7_800_000, "duration": 800_000}},
                    {"material_id": "image-4", "target_timerange": {"start": 9_400_000, "duration": 800_000}},
                ],
            },
            {
                "name": "图片共用特效",
                "type": "effect",
                "segments": [{"effect_id": "shared-effect", "target_timerange": {"start": 6_000_000, "duration": 2_600_000}}],
            },
        ],
        "materials": {
            "videos": [
                {"id": "image-1", "name": "static-image-1.png", "type": "video"},
                {"id": "image-2", "name": "static-image-2.png", "type": "video"},
                {"id": "image-3", "name": "static-image-3.png", "type": "video"},
                {"id": "image-4", "name": "static-image-4.png", "type": "video"},
            ],
            "video_effects": [{"id": "shared-effect", "name": "三图共用特效", "effect_id": "shared"}],
        },
    }

    catalog = build_parameter_catalog(draft)
    image_sequence = catalog["image_packaging_patterns"]["sequences"]
    content_lane = next(item for item in catalog["track_lanes"] if item["track_type"] == "video")
    content_categories = [item["category"] for item in content_lane["segments"]]

    assert len(image_sequence) == 1
    assert image_sequence[0]["member_count"] == 3
    assert image_sequence[0]["shared_visual_elements"][0]["material_name"] == "三图共用特效"
    assert content_categories == ["image_sequence", "image_sequence", "image_sequence", "image"]
    assert catalog["categories"]["image_sequence"]["segment_count"] >= 3

    no_shared_effect = copy.deepcopy(draft)
    no_shared_effect["tracks"] = no_shared_effect["tracks"][:1]
    no_shared_effect["materials"]["video_effects"] = []
    no_shared_catalog = build_parameter_catalog(no_shared_effect)
    no_shared_content_lane = next(item for item in no_shared_catalog["track_lanes"] if item["track_type"] == "video")
    assert no_shared_catalog["image_packaging_patterns"]["sequence_count"] == 0
    assert [item["category"] for item in no_shared_content_lane["segments"]] == ["image"] * 4


def test_aigc_video_material_type_overrides_static_first_frame_file_names() -> None:
    """被剪映标记为 video 的 png 首帧仍然属于 AIGC 视频。"""

    draft = {
        "name": "AIGC 首帧不等于图片镜头",
        "duration": 12_000_000,
        "tracks": [
            {
                "name": "AIGC动画",
                "type": "video",
                "segments": [
                    {"material_id": "first-frame-1", "target_timerange": {"start": 0, "duration": 3_000_000}},
                    {"material_id": "first-frame-2", "target_timerange": {"start": 3_000_000, "duration": 3_000_000}},
                    {"material_id": "first-frame-3", "target_timerange": {"start": 6_000_000, "duration": 3_000_000}},
                ],
            },
            {
                "name": "AIGC 共用特效",
                "type": "effect",
                "segments": [{"effect_id": "shared-effect", "target_timerange": {"start": 0, "duration": 9_000_000}}],
            },
        ],
        "materials": {
            "videos": [
                {"id": "first-frame-1", "name": "asset-s01-static-image_first_frame.png", "type": "video"},
                {"id": "first-frame-2", "name": "asset-s02-static-image_first_frame.png", "type": "video"},
                {"id": "first-frame-3", "name": "asset-s03-static-image_first_frame.png", "type": "video"},
            ],
            "video_effects": [{"id": "shared-effect", "name": "AIGC 共用特效", "effect_id": "shared"}],
        },
    }

    catalog = build_parameter_catalog(draft)
    aigc_lane = next(item for item in catalog["track_lanes"] if item["track_name"] == "AIGC动画")

    assert catalog["image_packaging_patterns"]["sequence_count"] == 0
    assert [item["category"] for item in aigc_lane["segments"]] == ["aigc", "aigc", "aigc"]
    assert [item["content_media_type"] for item in aigc_lane["segments"]] == ["video", "video", "video"]


def test_mixed_aigc_content_track_splits_independent_images_from_videos() -> None:
    """同一 AIGC 内容轨的独立图片与视频按片段拆分，不再整轨统一。"""

    draft = {
        "name": "AIGC 混合内容轨",
        "duration": 8_000_000,
        "tracks": [{
            "name": "AIGC动画",
            "type": "video",
            "segments": [
                {"material_id": "still", "target_timerange": {"start": 0, "duration": 2_000_000}},
                {"material_id": "motion", "target_timerange": {"start": 2_000_000, "duration": 3_000_000}},
                {"material_id": "still-2", "target_timerange": {"start": 5_000_000, "duration": 3_000_000}},
            ],
        }],
        "materials": {
            "videos": [
                {"id": "still", "name": "独立图片.png", "type": "photo"},
                {"id": "motion", "name": "AIGC视频.mp4", "type": "video"},
                {"id": "still-2", "name": "独立图片2.png", "type": "photo"},
            ],
        },
    }

    catalog = build_parameter_catalog(draft)
    lane = catalog["track_lanes"][0]

    ordered_segments = sorted(lane["segments"], key=lambda item: int(item["segment_index"]))
    assert [item["category"] for item in ordered_segments] == ["image", "aigc", "image"]
    assert [item["content_media_type"] for item in ordered_segments] == ["image", "video", "image"]
    assert catalog["categories"]["image"]["segment_count"] == 2
    assert catalog["categories"]["aigc"]["segment_count"] == 1


def test_mixed_aigc_content_track_splits_digital_human_segments_and_their_effects() -> None:
    """集合素材轨中的数字人片段不能被轨道名 AIGC动画吞并。"""

    draft = {
        "name": "AIGC 与数字人混合内容轨",
        "duration": 20_000_000,
        "tracks": [
            {
                "name": "AIGC动画",
                "type": "video",
                "segments": [
                    {"material_id": "digital", "target_timerange": {"start": 6_000_000, "duration": 2_000_000}},
                    {"material_id": "aigc", "target_timerange": {"start": 8_000_000, "duration": 2_000_000}},
                ],
            },
            {
                "name": "effect_track_2",
                "type": "effect",
                "segments": [
                    {"effect_id": "digital-effect", "target_timerange": {"start": 6_000_000, "duration": 2_000_000}},
                    {"effect_id": "aigc-effect", "target_timerange": {"start": 8_000_000, "duration": 2_000_000}},
                ],
            },
        ],
        "materials": {
            "videos": [
                {"id": "digital", "name": "asset-s01-digital-human_host.mp4", "type": "video"},
                {"id": "aigc", "name": "asset-s02-aigc-video.mp4", "type": "video"},
            ],
            "video_effects": [
                {"id": "digital-effect", "name": "人物入场特效", "effect_id": "digital-effect"},
                {"id": "aigc-effect", "name": "场景特效", "effect_id": "aigc-effect"},
            ],
        },
    }

    catalog = build_parameter_catalog(draft)
    content_lane = next(item for item in catalog["track_lanes"] if item["track_name"] == "AIGC动画")
    effect_lane = next(item for item in catalog["track_lanes"] if item["track_type"] == "effect")

    assert [item["category"] for item in content_lane["segments"]] == ["digital_human", "aigc"]
    assert [item["category"] for item in effect_lane["segments"]] == ["digital_human", "aigc"]
    assert catalog["categories"]["digital_human"]["segment_count"] == 2


def test_boundary_zone_can_overlap_image_and_aigc_content_categories() -> None:
    draft = {
        "name": "B_首尾保护区",
        "duration": 20_000_000,
        "tracks": [
            {
                "name": "内容视频",
                "type": "video",
                "segments": [
                    {"material_id": "opening-image-1", "target_timerange": {"start": 0, "duration": 800_000}},
                    {"material_id": "opening-image-2", "target_timerange": {"start": 900_000, "duration": 800_000}},
                    {"material_id": "opening-image-3", "target_timerange": {"start": 1_800_000, "duration": 800_000}},
                    {"material_id": "middle-aigc", "target_timerange": {"start": 6_000_000, "duration": 1_000_000}},
                ],
            },
            {
                "name": "片头共用特效",
                "type": "effect",
                "segments": [{"effect_id": "opening-effect", "target_timerange": {"start": 0, "duration": 2_600_000}}],
            },
        ],
        "materials": {
            "videos": [
                {"id": "opening-image-1", "name": "static-image-1.png", "type": "video"},
                {"id": "opening-image-2", "name": "static-image-2.png", "type": "video"},
                {"id": "opening-image-3", "name": "static-image-3.png", "type": "video"},
                {"id": "middle-aigc", "name": "aigc-video-1.mp4", "type": "video"},
            ],
            "video_effects": [{"id": "opening-effect", "name": "片头视觉包装", "effect_id": "opening"}],
        },
    }

    catalog = build_parameter_catalog(draft)
    content_lane = next(item for item in catalog["track_lanes"] if item["track_type"] == "video")

    assert [item["category"] for item in content_lane["segments"]] == ["image_sequence", "image_sequence", "image_sequence", "aigc"]
    assert [item["boundary_category"] for item in content_lane["segments"]] == ["opening", "opening", "opening", ""]
    assert catalog["image_packaging_patterns"]["sequence_count"] == 1
    assert catalog["categories"]["image_sequence"]["segment_count"] >= 3
    assert catalog["categories"]["aigc"]["segment_count"] == 1


def test_boundary_category_requires_a_complete_five_second_window_and_under_40_percent_coverage() -> None:
    draft = {
        "name": "B_边界硬规则",
        "duration": 20_000_000,
        "tracks": [{
            "name": "内容图片",
            "type": "video",
            "segments": [
                {"material_id": "opening", "target_timerange": {"start": 0, "duration": 4_999_999}},
                {"material_id": "crossing", "target_timerange": {"start": 4_800_000, "duration": 300_000}},
                {"material_id": "long", "target_timerange": {"start": 0, "duration": 8_000_000}},
                {"material_id": "ending", "target_timerange": {"start": 15_000_000, "duration": 5_000_000}},
            ],
        }],
        "materials": {"videos": [
            {"id": "opening", "name": "static-image-one.png", "type": "video"},
            {"id": "crossing", "name": "static-image-crossing.png", "type": "video"},
            {"id": "long", "name": "static-image-long.png", "type": "video"},
            {"id": "ending", "name": "static-image-last.png", "type": "video"},
        ]},
    }

    catalog = build_parameter_catalog(draft)
    segments = next(lane for lane in catalog["track_lanes"] if lane["track_type"] == "video")["segments"]

    assert [segment["category"] for segment in segments] == ["image", "image", "image", "image"]
    assert [segment["boundary_category"] for segment in segments] == ["opening", "", "", "ending"]


def test_parameter_catalog_keeps_empty_tracks_and_requires_binding_confirmation() -> None:
    draft = {
        "name": "B_with_empty_lanes",
        "duration": 5_000_000,
        "tracks": [
            {"name": "预留音轨", "type": "audio", "segments": []},
            {
                "name": "effect_track_2",
                "type": "effect",
                "segments": [{"effect_id": "effect-1", "target_timerange": {"start": 0, "duration": 1_000_000}}],
            },
            {
                "name": "文案变量轨",
                "type": "text",
                "segments": [{"text_template_id": "template-1", "target_timerange": {"start": 1_000_000, "duration": 1_000_000}}],
            },
        ],
        "materials": {
            "video_effects": [{"id": "effect-1", "name": "全局暗角", "effect_id": "e1"}],
            "text_templates": [
                {
                    "id": "template-1",
                    "name": "标题模板",
                    "text_info_resources": [{"text_material_id": "text-1"}],
                }
            ],
            "texts": [{"id": "text-1", "type": "text", "content": "知识·笔记"}],
        },
    }

    catalog = build_parameter_catalog(draft, enforce_confirmation=True)

    assert catalog["totals"] == {"track_count": 3, "represented_track_count": 3, "segment_count": 2}
    assert [lane["raw_track_index"] for lane in catalog["track_lanes"]] == [2, 1, 0]
    assert [lane["jianying_track_index"] for lane in catalog["track_lanes"]] == [1, 0, -1]
    assert catalog["track_lanes"][-1]["segments"] == []
    assert all(lane["jianying_track_index"] < 0 for lane in catalog["track_lanes"] if lane["track_type"] == "audio")
    assert catalog["track_index_contract"]["display_field"] == "jianying_track_index"
    assert catalog["categories"]["global_audio"]["tracks"][0]["segment_count"] == 0
    assert catalog["confirmation"]["unresolved_track_indices"] == [0, 1, 2]
    assert catalog["confirmation"]["text_template_targets"][0]["template_names"] == ["标题模板"]
    text_track = next(
        track
        for category in catalog["categories"].values()
        for track in category["tracks"]
        if track["track_index"] == 2
    )
    assert text_track["parameter_summary"]["parameter_groups"]["text_templates"][0]["parameters"]["actual_text"] == "知识·笔记"

    confirmed = build_parameter_catalog(
        draft,
        enforce_confirmation=True,
        track_binding_overrides={
            "0": {"category": "global_audio", "element_role": "audio", "confirmed": True},
            "1": {"category": "opening", "element_role": "global_effect", "confirmed": True},
            "2": {
                "category": "global_subtitles",
                "element_role": "text_template_variable",
                "text_variable_role": "subtitle",
                "text_target_track_index": 2,
                "confirmed": True,
            },
        },
    )

    assert confirmed["confirmation"]["confirmed"] is True
    assert confirmed["confirmation"]["unresolved_track_indices"] == []
    assert confirmed["categories"]["opening"]["tracks"][0]["track_index"] == 1
    text_confirmation = next(
        item for item in confirmed["confirmation"]["tracks"] if item["raw_track_index"] == 2
    )
    assert text_confirmation["text_variable_role"] == "subtitle"


def test_replay_blocks_when_enforced_confirmation_is_unresolved(tmp_path: Path) -> None:
    baseline = _write_draft(tmp_path / "A")
    packaged = _write_draft(tmp_path / "B", packaged=True)
    result = capture_packaging_pair(baseline, packaged, enforce_confirmation=True)
    output = tmp_path / "capture"
    write_capture_artifact(result, output)

    assert result["confirmation"]["enforced"] is True
    assert result["confirmation"]["confirmed"] is False
    with pytest.raises(PackagingCaptureError, match="未确认"):
        write_replay_artifact(baseline, packaged, result, output)
