import json
from pathlib import Path

import pytest

import workflow_1256.video_packaging_bundle as bundle_module
from workflow_1256.video_packaging_bundle import (
    REQUIRED_CATEGORIES,
    VideoPackagingBundleError,
    analyze_full_draft_style,
    diff_full_draft_style,
    build_bundle_application_preview,
    build_video_packaging_bundle,
    extract_category_template,
    materialize_relative_keyframes,
    scale_relative_keyframe,
    scale_relative_timerange,
    summarize_template_components,
    update_bundle_full_draft_style,
)


def _draft(duration_us: int = 10_000_000) -> dict:
    return {
        "name": "母版草稿",
        "duration": duration_us,
        "canvas_config": {"width": 1080, "height": 1920, "ratio": "9:16"},
        "platform": {"os": "windows", "app_version": "5.9.0"},
        "materials": {
            "videos": [{"id": "video-1", "path": "D:\\source\\video.mp4", "duration": duration_us}],
            "video_effects": [{
                "id": "effect-1",
                "name": "暗角",
                "apply_time_range": {"start": 0, "duration": 3_000_000},
                "common_keyframes": [],
            }],
        },
        "tracks": [{
            "name": "AIGC动画",
            "type": "video",
            "segments": [{
                "id": "segment-1",
                "material_id": "video-1",
                "target_timerange": {"start": 0, "duration": duration_us},
                "common_keyframes": [{
                    "property_type": "KFTypeScaleX",
                    "keyframe_list": [
                        {"id": "kf-1", "time_offset": 0, "values": [1.0]},
                        {"id": "kf-2", "time_offset": 3_000_000, "values": [1.2]},
                    ],
                }],
                "uniform_scale": {"on": True, "value": 1.0},
            }],
        }],
    }


def _write_draft(path: Path, duration_us: int = 10_000_000) -> Path:
    path.mkdir(parents=True)
    (path / "assets").mkdir()
    (path / "assets" / "placeholder.txt").write_text("asset", encoding="utf-8")
    (path / "assets" / "source-content.mp4").write_bytes(b"source video")
    (path / "draft_content.json").write_text(json.dumps(_draft(duration_us), ensure_ascii=False), encoding="utf-8")
    (path / "draft_info.json").write_text(json.dumps({"name": path.name, "duration": duration_us}), encoding="utf-8")
    return path


def test_template_extracts_full_raw_data_and_relative_keyframes(tmp_path: Path) -> None:
    source = _write_draft(tmp_path / "aigc")
    package = extract_category_template("aigc", source)

    assert package["validation"]["raw_data_preserved"] is True
    assert package["raw_draft"]["materials"]["video_effects"][0]["name"] == "暗角"
    assert package["template_draft"]["materials"]["videos"] == []
    assert package["slot_policy"]["slots"][0]["media_type"] == "visual_media"
    assert package["validation"]["content_variables_excluded"] is True
    assert package["validation"]["content_leak_count"] == 0
    timing = package["normalized_timing"]["tracks"][0]["segments"][0]
    assert timing["target_timerange"]["duration_ratio"] == 1.0
    assert timing["keyframes"][0]["property_type"] == "UNIFORM_SCALE"
    assert timing["keyframes"][0]["points"][1]["offset_ratio"] == 0.3
    assert package["normalized_timing"]["materials"]["time_ranges"][0]["projection"]["duration_ratio"] == 0.3
    assert package["validation"]["collected_components"] == {
        "effects": 1,
        "keyframes": 2,
        "transitions": 0,
        "text_templates": 0,
    }


def test_template_component_summary_covers_effect_aliases_transitions_and_text_templates() -> None:
    draft = _draft()
    draft["materials"].update({
        "effects": [{"id": "effect-2"}],
        "plugin_effects": [{"id": "effect-3"}],
        "transitions": [{"id": "transition-1"}],
        "text_templates": [{"id": "text-template-1"}],
    })

    assert summarize_template_components(draft) == {
        "effects": 3,
        "keyframes": 2,
        "transitions": 1,
        "text_templates": 1,
    }


def test_full_draft_style_analysis_covers_audio_global_visual_subtitle_and_transitions() -> None:
    draft = _draft()
    draft["duration"] = 10_000_000
    draft["materials"].update({
        "audios": [
            {"id": "audio-1", "name": "BGM", "type": "music"},
            {"id": "sound-1", "name": "撕纸声", "type": "sound"},
        ],
        "audio_effects": [{"id": "audio-effect-1", "name": "人声增强3", "effect_id": "audio-effect"}],
        "filters": [{"id": "filter-1", "name": "电影滤镜", "resource_id": "filter-resource"}],
        "transitions": [{"id": "transition-1", "name": "叠化", "transition_id": "transition-resource"}],
        "texts": [{
            "id": "text-1",
            "type": "subtitle",
            "font_size": 15,
            "line_spacing": 0.02,
            "line_max_width": 0.84,
            "has_shadow": True,
            "content": json.dumps({
                "styles": [{
                    "size": 10,
                    "bold": True,
                    "fill": {"content": {"render_type": "solid", "solid": {"color": [1, 1, 1]}}},
                    "strokes": [],
                }],
                "text": "不要保存这句字幕",
            }, ensure_ascii=False),
        }],
    })
    draft["tracks"].extend([
        {
            "name": "BGM",
            "type": "audio",
            "segments": [{
                "material_id": "audio-1",
                "volume": 0.22,
                "target_timerange": {"start": 0, "duration": 10_000_000},
                "extra_material_refs": ["audio-effect-1"],
            }],
        },
        {
            "name": "整体滤镜",
            "type": "filter",
            "segments": [{
                "material_id": "filter-1",
                "target_timerange": {"start": 0, "duration": 10_000_000},
            }],
        },
        {
            "name": "字幕",
            "type": "text",
            "segments": [{"material_id": "text-1", "target_timerange": {"start": 0, "duration": 10_000_000}}],
        },
        {
            "name": "",
            "type": "audio",
            "segments": [{
                "material_id": "sound-1",
                "volume": 0.8,
                "target_timerange": {"start": 9_500_000, "duration": 1_000_000},
            }],
        },
    ])
    draft["tracks"][0]["segments"][0]["transition_id"] = "transition-1"

    analysis = analyze_full_draft_style(draft, source_path="D:/drafts/full")

    assert analysis["audio"]["tracks"][0]["volume"]["average_percent"] == 22.0
    assert analysis["audio"]["tracks"][0]["segments"][0]["effects"][0]["name"] == "人声增强3"
    assert analysis["audio"]["tracks"][0]["effect_count"] == 1
    sound_track = next(track for track in analysis["audio"]["tracks"] if track["track_role"] == "sound_effect")
    assert sound_track["name"] == "音效 · 撕纸声"
    assert analysis["transitions"]["audio_companion_count"] == 1
    assert analysis["transitions"]["audio_companions"][0]["material_name"] == "撕纸声"
    assert analysis["visual"]["has_full_timeline_filter"] is True
    assert analysis["visual"]["overall_filters"][0]["material"]["name"] == "电影滤镜"
    assert analysis["subtitles"]["profiles"][0]["profile"]["has_shadow"] is True
    assert analysis["transitions"]["applied_count"] == 1
    assert analysis["transitions"]["applied"][0]["transition_at_us"] == 10_000_000
    assert analysis["transitions"]["applied"][0]["segment_start_us"] == 0
    serialized = json.dumps(analysis, ensure_ascii=False)
    assert "不要保存这句字幕" not in serialized


def test_full_draft_style_analysis_includes_embedded_video_audio_but_skips_images() -> None:
    draft = _draft()
    draft["materials"]["videos"][0]["has_audio"] = True
    draft["tracks"][0]["segments"][0]["volume"] = 0.6
    draft["materials"]["videos"].append({
        "id": "image-1",
        "path": "D:\\source\\still.png",
        "has_audio": True,
    })
    draft["tracks"].append({
        "name": "图片轨道",
        "type": "video",
        "segments": [{
            "material_id": "image-1",
            "volume": 0.2,
            "target_timerange": {"start": 0, "duration": 1_000_000},
        }],
    })

    analysis = analyze_full_draft_style(draft, source_path="D:/drafts/full")

    video_audio = [
        track for track in analysis["audio"]["tracks"]
        if track["source_type"] == "video_original_audio"
    ]
    assert len(video_audio) == 1
    assert video_audio[0]["name"] == "视频原声 · AIGC动画"
    assert video_audio[0]["volume"]["average_percent"] == 60.0
    assert video_audio[0]["is_video_original_audio"] is True


def test_diff_full_draft_style_returns_packaging_parameters_for_tuning() -> None:
    baseline = {
        "audio": {"tracks": [{"name": "BGM", "muted": False, "volume": {"average_percent": 22.0}}]},
        "visual": {
            "overall_effects": [{"material": {"name": "暗角"}, "coverage_ratio": 1.0}],
            "overall_filters": [],
        },
        "transitions": {"applied": [{"transition": {"name": "叠化"}, "transition_duration_us": 1_000_000}]},
        "subtitles": {"profiles": [{"profile": {"font_title": "微软雅黑", "font_size": 30, "has_shadow": True}}]},
    }
    current = {
        "audio": {"tracks": [{"name": "BGM", "muted": True, "volume": {"average_percent": 18.0}}]},
        "visual": {
            "overall_effects": [{"material": {"name": "暗角"}, "coverage_ratio": 0.65}],
            "overall_filters": [],
        },
        "transitions": {"applied": [{"transition": {"name": "叠化"}, "transition_duration_us": 800_000}]},
        "subtitles": {"profiles": [{"profile": {"font_title": "经典雅黑", "font_size": 34, "has_shadow": False}}]},
    }

    diff = diff_full_draft_style(baseline, current)

    assert diff["status"] == "changed"
    assert diff["changed_count"] == 7
    assert diff["packaging_parameters"]["audio_tracks"] == [{"index": 0, "track_name": "BGM", "volume_percent": 18.0, "muted": True}]
    assert diff["packaging_parameters"]["overall_effects"] == [{"index": 0, "coverage_percent": 65.0, "enabled": True}]
    assert diff["packaging_parameters"]["transitions"] == [{"index": 0, "duration_percent": 80.0, "enabled": True}]
    assert diff["packaging_parameters"]["subtitle_profiles"][0]["font_name"] == "经典雅黑"


def test_relative_timing_scales_ten_second_template_to_five_second_shot() -> None:
    projection = {"start_ratio": 0.0, "duration_ratio": 0.3}
    assert scale_relative_timerange(projection, 5_000_000) == {"start": 0, "duration": 1_500_000}
    assert scale_relative_keyframe(0.3, 5_000_000) == 1_500_000
    restored = materialize_relative_keyframes(
        [{"property_type": "KFTypeScale", "points": [{"offset_ratio": 0.3, "values": [1.2]}]}],
        5_000_000,
    )
    assert restored[0]["keyframe_list"][0]["time_offset"] == 1_500_000


def test_bundle_writes_each_category_without_touching_sources(tmp_path: Path) -> None:
    sources = {category: _write_draft(tmp_path / category) for category in REQUIRED_CATEGORIES}
    output = tmp_path / "bundle"
    before = {(category, (path / "draft_content.json").read_bytes()) for category, path in sources.items()}

    manifest = build_video_packaging_bundle(
        sources,
        output,
        bundle_name="日更包装包",
        version="1.0",
        full_draft_source=sources["opening"],
    )

    assert manifest["validation"]["category_count"] == 7
    assert manifest["validation"]["source_drafts_modified"] is False
    assert manifest["validation"]["content_variables_excluded"] is True
    assert manifest["validation"]["content_leak_count"] == 0
    assert manifest["validation"]["collected_components"] == {
        "effects": 7,
        "keyframes": 14,
        "transitions": 0,
        "text_templates": 0,
    }
    assert manifest["validation"]["full_draft_style_enabled"] is True
    assert manifest["full_draft_style"]["schema_version"] == "full-draft-style-v1"
    assert (output / "bundle_manifest.json").is_file()
    for category in REQUIRED_CATEGORIES:
        assert (output / "categories" / category / "raw_snapshot" / "draft_content.json").is_file()
        assert (output / "categories" / category / "normalized_timing.json").is_file()
        assert not list((output / "categories" / category / "raw_snapshot").rglob("*.mp4"))
        sanitized = json.loads(
            (output / "categories" / category / "raw_snapshot" / "draft_content.json").read_text(encoding="utf-8")
        )
        assert sanitized["materials"]["videos"] == []
    after = {(category, (path / "draft_content.json").read_bytes()) for category, path in sources.items()}
    assert before == after


def test_bundle_requires_all_categories_by_default(tmp_path: Path) -> None:
    with pytest.raises(VideoPackagingBundleError, match="缺少分类母版"):
        build_video_packaging_bundle(
            {"opening": _write_draft(tmp_path / "opening")},
            tmp_path / "bundle",
        )


def test_existing_bundle_can_receive_full_draft_style_incrementally(tmp_path: Path) -> None:
    sources = {category: _write_draft(tmp_path / category) for category in REQUIRED_CATEGORIES}
    output = tmp_path / "bundle"
    build_video_packaging_bundle(sources, output, bundle_name="日更包装包", version="1.0")

    updated = update_bundle_full_draft_style(output, sources["opening"])

    assert updated["validation"]["full_draft_style_enabled"] is True
    assert updated["full_draft_style"]["source"]["path"] == str(sources["opening"].resolve())
    saved = json.loads((output / "bundle_manifest.json").read_text(encoding="utf-8"))
    assert saved["full_draft_style"]["schema_version"] == "full-draft-style-v1"


def test_bundle_validation_blocks_content_leak_before_output(tmp_path: Path, monkeypatch) -> None:
    source = _write_draft(tmp_path / "aigc")
    output = tmp_path / "blocked-bundle"
    original_extract = bundle_module.extract_category_template

    def leaky_extract(category: str, source_draft: Path) -> dict:
        package = original_extract(category, source_draft)
        package["content_audit"]["content_leak_count"] = 1
        package["content_audit"]["content_leaks"] = ["materials.videos"]
        return package

    monkeypatch.setattr(bundle_module, "extract_category_template", leaky_extract)
    with pytest.raises(VideoPackagingBundleError, match="内容变量泄漏"):
        build_video_packaging_bundle({"aigc": source}, output, require_all_categories=False)
    assert not output.exists()


def test_bundle_application_preview_keeps_target_draft_read_only(tmp_path: Path) -> None:
    source = _write_draft(tmp_path / "aigc")
    bundle_dir = tmp_path / "bundle"
    build_video_packaging_bundle({"aigc": source}, bundle_dir, require_all_categories=False)
    preview = build_bundle_application_preview(
        bundle_dir,
        [{"shot_id": "g03_s02", "category": "aigc", "start_us": 2_000_000, "end_us": 7_000_000}],
    )
    assert preview["status"] == "ready"
    assert preview["actions"][0]["target_duration_us"] == 5_000_000
    assert preview["actions"][0]["write_policy"] == "additive_only"
    assert preview["source_draft_mutated"] is False
