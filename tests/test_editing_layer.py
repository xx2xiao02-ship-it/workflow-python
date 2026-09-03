from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import pytest

from workflow_1256.editing_layer import (
    DEFAULT_CAPTION_ALIGNMENT,
    DEFAULT_EDITING_SETTINGS,
    EditingLayerError,
    _record_window,
    _bounded_source_duration_us,
    _jianying_target_window,
    compile_editing_layer,
    format_caption_display_text,
    normalize_editing_settings,
    validate_windows_native_plan,
)
from workflow_1256 import real_asset_handoff
from workflow_1256.real_asset_handoff import _bgm_segment_windows, build_live_asset_registry
from workflow_1256.governance.contracts import (
    AssetRecord,
    AssetRegistry,
    CaptionRecord,
    DirectorGroup,
    DirectorLockedManifest,
    DirectorShot,
    MaterialRequirement,
    TimeWindow,
)


def test_caption_display_inserts_line_break_at_natural_pause() -> None:
    assert format_caption_display_text("一个团队要看完百万页材料、翻十年财报") == "一个团队要看完百万页材料、\n翻十年财报"


def test_caption_display_leaves_short_or_unbreakable_text_unchanged() -> None:
    assert format_caption_display_text("短字幕。") == "短字幕。"
    assert format_caption_display_text("这是一条没有停顿符号的长字幕文本") == "这是一条没有停顿符号的长字幕文本"


def test_caption_alignment_default_is_centered_in_draft_and_capcut_payload() -> None:
    director, assets = _manifest_pair()
    plan = compile_editing_layer(director, assets)
    assert DEFAULT_CAPTION_ALIGNMENT == 1
    assert plan.capcut_payloads["add_captions"]["alignment"] == 1


def test_editing_settings_are_normalized_for_the_native_writer() -> None:
    settings = normalize_editing_settings({
        "caption_alignment": "画面居中",
        "caption_size": "14",
        "caption_color": "#12abEF",
        "border_color": "#334455",
        "border_width": "粗描边",
        "line_spacing": "紧凑",
        "narration_volume": "80",
        "bgm_volume": "35",
        "sfx_volume": "60",
        "transition": "无转场",
        "transition_duration": "0.8",
        "filter": "无滤镜",
        "effect": "无特效",
    })
    assert settings["caption_alignment"] == "bottom_center"
    assert settings["caption_size"] == 10.0
    assert settings["caption_color"] == "#FFFFFF"
    assert settings["border_width"] == 40.0
    assert settings["line_spacing"] == 0
    assert settings["narration_volume"] == 80.0
    assert settings["bgm_volume"] == 35.0
    assert settings["sfx_volume"] == 60.0
    assert "transition" not in settings
    assert "transition_duration" not in settings
    assert "filter" not in settings
    assert "effect" not in settings
    assert "text_in_animation" not in settings


def test_editing_settings_defaults_are_stable() -> None:
    assert normalize_editing_settings(None) == normalize_editing_settings(DEFAULT_EDITING_SETTINGS)


def test_jianying_target_window_uses_shared_frame_boundaries() -> None:
    first = _jianying_target_window(0, 3_798_524, 30)
    second = _jianying_target_window(3_798_524, 7_896_932, 30)
    assert first == (0, 3_800_000)
    assert second == (3_800_000, 4_100_000)
    assert first[0] + first[1] == second[0]


def test_native_source_duration_is_bounded_by_actual_media_duration() -> None:
    item = {"asset_id": "asset.bgm", "asset_duration_us": 124_210_000, "duration_us": 123_960_000}
    assert _bounded_source_duration_us(item, 123_218_000) == 123_218_000


def test_native_source_duration_rejects_empty_media() -> None:
    item = {"asset_id": "asset.video", "asset_duration_us": 4_000_000, "duration_us": 3_900_000}
    with pytest.raises(EditingLayerError, match="本地素材时长必须大于 0"):
        _bounded_source_duration_us(item, 0)


def test_bgm_uses_actual_segment_window_instead_of_project_requirement_window() -> None:
    director, assets = _manifest_pair()
    bgm = assets.records[2]
    assert _record_window(bgm, director.requirements[2], director) == director.total_timeline

    split_bgm = AssetRecord(**{
        **bgm.__dict__,
        "actual_timeline": TimeWindow(2_000_000, 4_000_000),
    })
    assert _record_window(split_bgm, director.requirements[2], director) == TimeWindow(2_000_000, 4_000_000)


def test_single_shot_visual_inherits_director_shot_timeline() -> None:
    director, assets = _manifest_pair()
    visual = assets.records[0]
    wrong_requirement = MaterialRequirement(**{
        **director.requirements[0].__dict__,
        "timeline": TimeWindow(0, 3_000_000),
    })
    assert _record_window(visual, wrong_requirement, director) == director.shots[0].timeline


def test_long_bgm_is_split_at_a_director_group_boundary() -> None:
    long_window = TimeWindow(0, 90_000_000)
    groups = [
        SimpleNamespace(timeline=TimeWindow(0, 42_000_000)),
        SimpleNamespace(timeline=TimeWindow(42_000_000, 90_000_000)),
    ]
    long_director = SimpleNamespace(total_timeline=long_window, groups=groups)
    windows = _bgm_segment_windows(long_director)
    assert [(item.start_us, item.end_us) for item in windows] == [
        (0, 42_000_000),
        (42_000_000, 90_000_000),
    ]


def _manifest_pair() -> tuple[DirectorLockedManifest, AssetRegistry]:
    window = TimeWindow(0, 4_000_000)
    caption = CaptionRecord("c01", "g01", "测试字幕", window)
    group = DirectorGroup(
        group_id="g01",
        group_index=0,
        segment_text="测试段落",
        timeline=window,
        shot_ids=["g01_s01"],
        caption_ids=["c01"],
        narration_requirement_id="req.narration",
    )
    shot = DirectorShot(
        shot_id="g01_s01",
        group_id="g01",
        shot_index=0,
        source_text="测试镜头",
        clip_role="primary_visual",
        story_beat="开场",
        timeline=window,
        route_candidates=["aigc"],
        requirement_ids=["req.video", "req.narration", "req.bgm"],
    )
    requirements = [
        MaterialRequirement(
            requirement_id="req.video",
            scope="shot",
            asset_type="video",
            role="primary_visual",
            group_id="g01",
            shot_ids=["g01_s01"],
            mandatory=True,
            route="aigc_video",
            coverage_policy="one_shot",
            source_node="110697",
            timeline=window,
        ),
        MaterialRequirement(
            requirement_id="req.narration",
            scope="project",
            asset_type="audio",
            role="narration",
            group_id=None,
            shot_ids=[],
            mandatory=True,
            route="tts",
            coverage_policy="full_timeline",
            source_node="191683",
            timeline=window,
        ),
        MaterialRequirement(
            requirement_id="req.bgm",
            scope="project",
            asset_type="audio",
            role="background_music",
            group_id=None,
            shot_ids=[],
            mandatory=False,
            route="bgm",
            coverage_policy="full_timeline",
            source_node="116592",
            timeline=window,
        ),
    ]
    director = DirectorLockedManifest(
        project_id="project-test",
        run_id="run-test",
        plan_version="director-v1",
        tts_fingerprint="tts-test",
        total_timeline=window,
        groups=[group],
        shots=[shot],
        captions=[caption],
        requirements=requirements,
        provenance=[{"node_id": "103964"}],
    )
    assets = AssetRegistry(
        project_id="project-test",
        run_id="run-test",
        source_plan_version="director-v1",
        asset_version="assets-v1",
        status="ASSETS_READY",
        records=[
            AssetRecord(
                asset_id="asset.video.001",
                requirement_id="req.video",
                group_id="g01",
                shot_ids=["g01_s01"],
                asset_type="video",
                role="primary_visual",
                status="VALIDATED",
                source_node="110697",
                source_index=0,
                asset_version="assets-v1",
                asset_duration_us=4_000_000,
                remote_url="https://example.com/video.mp4",
                evidence_level="fixture_observed",
            ),
            AssetRecord(
                asset_id="asset.audio.narration",
                requirement_id="req.narration",
                group_id=None,
                shot_ids=[],
                asset_type="audio",
                role="narration",
                status="VALIDATED",
                source_node="191683",
                source_index=0,
                asset_version="assets-v1",
                asset_duration_us=4_000_000,
                remote_url="https://example.com/narration.mp3",
                evidence_level="fixture_observed",
            ),
            AssetRecord(
                asset_id="asset.audio.bgm",
                requirement_id="req.bgm",
                group_id=None,
                shot_ids=[],
                asset_type="audio",
                role="background_music",
                status="VALIDATED",
                source_node="116592",
                source_index=1,
                asset_version="assets-v1",
                asset_duration_us=4_000_000,
                remote_url="https://example.com/bgm.mp3",
                evidence_level="fixture_observed",
            ),
        ],
    )
    return director, assets


def test_digital_human_auth_fallback_is_handed_off_as_static_image(tmp_path, monkeypatch) -> None:
    window = TimeWindow(0, 4_000_000)
    image = tmp_path / "grid-cell.jpg"
    image.write_bytes(b"grid-cell")
    group = DirectorGroup(
        group_id="g01",
        group_index=0,
        segment_text="数字人登录失败降级",
        timeline=window,
        shot_ids=["g01_s01"],
        caption_ids=[],
        narration_requirement_id="req.narration",
    )
    shot = DirectorShot(
        shot_id="g01_s01",
        group_id="g01",
        shot_index=0,
        source_text="数字人镜头",
        clip_role="primary_visual",
        story_beat="登录失败时使用已有宫格图",
        timeline=window,
        route_candidates=["digital_human"],
        requirement_ids=["req.video.digital_human"],
    )
    requirement = MaterialRequirement(
        requirement_id="req.video.digital_human",
        scope="shot",
        asset_type="video",
        role="digital_human",
        group_id="g01",
        shot_ids=["g01_s01"],
        mandatory=True,
        route="digital_human",
        coverage_policy="one_shot",
        source_node="digital-human",
        timeline=window,
    )
    narration_requirement = MaterialRequirement(
        requirement_id="req.narration",
        scope="project",
        asset_type="audio",
        role="narration",
        group_id=None,
        shot_ids=[],
        mandatory=True,
        route="tts",
        coverage_policy="full_timeline",
        source_node="tts",
        timeline=window,
    )
    bgm_requirement = MaterialRequirement(
        requirement_id="project.audio.bgm",
        scope="project",
        asset_type="audio",
        role="background_music",
        group_id=None,
        shot_ids=[],
        mandatory=False,
        route="bgm",
        coverage_policy="full_timeline",
        source_node="bgm",
        timeline=window,
    )
    director = DirectorLockedManifest(
        project_id="project-digital-fallback",
        run_id="run-digital-fallback",
        plan_version="director-v1",
        tts_fingerprint="tts-test",
        total_timeline=window,
        groups=[group],
        shots=[shot],
        captions=[],
        requirements=[requirement, narration_requirement, bgm_requirement],
        provenance=[{"node_id": "director"}],
        narration_assets=[{
            "asset_id": "tts-g01",
            "group_id": "g01",
            "local_path": str(tmp_path / "narration.mp3"),
            "timeline": {"start": 0, "end": window.end_us},
        }],
    )
    narration_path = tmp_path / "narration.mp3"
    bgm_path = tmp_path / "bgm.mp3"
    narration_path.write_bytes(b"narration")
    bgm_path.write_bytes(b"bgm")
    monkeypatch.setattr(
        real_asset_handoff,
        "_prepared_audio",
        lambda path, _target_dir, duration_us: (Path(path), duration_us, False),
    )
    monkeypatch.setattr(
        real_asset_handoff,
        "_prepared_bgm_segments",
        lambda _source, _target_dir, windows: ([(bgm_path, window.duration_us, windows[0], False)], 2_000_000),
    )
    asset_result = {
        "media_type_overrides": {"g01_s01": "static_image"},
        "digital_human_fallbacks": {
            "g01_s01": {
                "image_path": str(image),
                "status": "fallback_image",
                "source_grid_id": "first_frame_grid_001",
            },
        },
        "digital_human_assets": {
            "shot_results": [{
                "shot_id": "g01_s01",
                "status": "fallback_image",
                "image_path": str(image),
            }],
        },
        "first_frame_assets": {"image_paths": [str(image)]},
        "first_frame_prompt_trace": [{"shot_id": "g01_s01"}],
        "bgm_assets": {"audio_path": str(bgm_path), "source_type": "generated"},
    }

    registry, report = build_live_asset_registry(
        director,
        [{"shot_id": "g01_s01", "media_type": "digital_human_video"}],
        asset_result,
        asset_task_id="asset-digital-fallback",
        prepared_root=tmp_path / "prepared",
    )

    visual = next(item for item in registry.records if item.role == "primary_visual_image")
    assert visual.asset_type == "image"
    assert visual.local_path == str(image.resolve())
    assert visual.metadata["digital_human_fallback"] is True
    assert report["route_trace"][0]["original_media_type"] == "digital_human_video"
    assert report["route_trace"][0]["media_type"] == "static_image"


def test_explanation_remotion_video_is_handed_off_without_cross_task_first_frame(tmp_path, monkeypatch) -> None:
    window = TimeWindow(0, 4_000_000)
    video_path = tmp_path / "g01_s01-remotion.mp4"
    narration_path = tmp_path / "narration.mp3"
    bgm_path = tmp_path / "bgm.mp3"
    for path in (video_path, narration_path, bgm_path):
        path.write_bytes(b"fixture")
    group = DirectorGroup(
        group_id="g01", group_index=0, segment_text="说明镜头", timeline=window,
        shot_ids=["g01_s01"], caption_ids=[], narration_requirement_id="g01.audio.narration",
    )
    shot = DirectorShot(
        shot_id="g01_s01", group_id="g01", shot_index=0, source_text="说明内容",
        clip_role="explanation", story_beat="因果链", timeline=window,
        route_candidates=["explanation"], requirement_ids=["g01_s01.video.aigc"],
        production_spec={"selected_route": "explanation"},
    )
    requirements = [
        MaterialRequirement(
            "g01_s01.video.aigc", "shot", "video", "primary_visual", "g01", ["g01_s01"],
            True, "AIGC动画", "fit_to_shot", "AIGC动画*", window,
        ),
        MaterialRequirement(
            "g01.audio.narration", "group", "audio", "narration", "g01", ["g01_s01"],
            True, "speech_synthesis", "cover_group", "speech_synthesis", window,
        ),
        MaterialRequirement(
            "project.audio.bgm", "project", "audio", "background_music", None, [],
            False, "background_music", "cover_project", "背景音乐*", window,
        ),
    ]
    director = DirectorLockedManifest(
        project_id="project-explanation", run_id="run-explanation", plan_version="director-v1",
        tts_fingerprint="tts-test", total_timeline=window, groups=[group], shots=[shot],
        captions=[], requirements=requirements, provenance=[{"node_id": "director"}],
        narration_assets=[{
            "asset_id": "tts-g01", "group_id": "g01", "local_path": str(narration_path),
            "timeline": {"start": 0, "end": window.end_us},
        }],
    )
    monkeypatch.setattr(
        real_asset_handoff, "_prepared_video",
        lambda path, _target_dir, duration_us: (Path(path), duration_us, False),
    )
    monkeypatch.setattr(
        real_asset_handoff, "_prepared_audio",
        lambda path, _target_dir, duration_us: (Path(path), duration_us, False),
    )
    monkeypatch.setattr(
        real_asset_handoff, "_prepared_bgm_segments",
        lambda _source, _target_dir, windows: ([(bgm_path, window.duration_us, windows[0], False)], 2_000_000),
    )
    registry, report = build_live_asset_registry(
        director,
        [{"shot_id": "g01_s01", "media_type": "explanation", "shot_class": "explanation"}],
        {
            "explanation_assets": {
                "status": "succeeded",
                "shot_results": [{
                    "shot_id": "g01_s01", "status": "succeeded", "output_path": str(video_path),
                    "template_id": "causal_chain", "style_lock_id": "style-test",
                }],
            },
            "bgm_assets": {"audio_path": str(bgm_path), "source_type": "generated"},
        },
        asset_task_id="asset-explanation",
        prepared_root=tmp_path / "prepared",
    )
    visual = next(item for item in registry.records if item.role == "explanation_video")
    assert visual.asset_type == "video"
    assert visual.local_path == str(video_path.resolve())
    assert visual.metadata["template_id"] == "causal_chain"
    assert visual.metadata["style_lock_id"] == "style-test"
    assert report["route_trace"][0]["media_type"] == "explanation"


class EditingLayerTests(unittest.TestCase):
    def test_compile_preserves_locked_timeline_and_builds_capcut_projection(self) -> None:
        director, assets = _manifest_pair()
        plan = compile_editing_layer(director, assets)

        self.assertEqual(plan.platform_os, "windows")
        self.assertEqual(plan.draft_method, "capcut-mate.pyJianYingDraft.DraftFolder.create_draft")
        self.assertEqual(plan.total_timeline.duration_us, 4_000_000)
        self.assertEqual([item["track_name"] for item in plan.tracks], [
            "首帧参考", "AIGC动画", "解说", "BGM", "字幕"
        ])
        self.assertEqual(plan.validation["timeline_unit"], "microseconds")
        self.assertEqual(len(plan.bindings), 1)
        self.assertEqual(plan.bindings[0].planned_timeline.start_us, 0)
        self.assertEqual(plan.capcut_payloads["create_draft"], {"height": 1920, "width": 1080})
        video_infos = json.loads(plan.capcut_payloads["add_videos"]["video_infos"])
        self.assertEqual(len(video_infos), 1)
        self.assertEqual(video_infos[0]["start"], 0)
        self.assertEqual(video_infos[0]["end"], 4_000_000)
        plan.to_edit_manifest().validate_against(director, assets)

    def test_native_gate_rejects_remote_only_assets(self) -> None:
        director, assets = _manifest_pair()
        plan = compile_editing_layer(director, assets)
        with self.assertRaises(EditingLayerError):
            validate_windows_native_plan(plan)

    def test_digital_human_uses_the_shared_main_visual_track(self) -> None:
        director, assets = _manifest_pair()
        record = assets.records[0]
        digital = AssetRecord(**{**record.__dict__, "role": "digital_human"})
        registry = AssetRegistry(
            project_id=assets.project_id,
            run_id=assets.run_id,
            source_plan_version=assets.source_plan_version,
            asset_version=assets.asset_version,
            records=[digital, *assets.records[1:]],
            status=assets.status,
        )
        plan = compile_editing_layer(director, registry)
        self.assertTrue(any(item["track_name"] == "AIGC动画" for item in plan.native_items))
        self.assertFalse(any(item["track_name"] == "数字人" for item in plan.native_items))

    def test_static_image_can_be_bound_as_main_visual(self) -> None:
        director, assets = _manifest_pair()
        image = AssetRecord(**{
            **assets.records[0].__dict__,
            "asset_type": "image",
            "role": "primary_visual_image",
            "local_path": __file__,
            "remote_url": "",
        })
        registry = AssetRegistry(
            project_id=assets.project_id,
            run_id=assets.run_id,
            source_plan_version=assets.source_plan_version,
            asset_version=assets.asset_version,
            records=[image, *assets.records[1:]],
            status=assets.status,
        )
        plan = compile_editing_layer(director, registry)
        self.assertEqual(len(plan.bindings), 1)
        self.assertEqual(plan.bindings[0].asset_id, image.asset_id)
        self.assertEqual(plan.bindings[0].track_role, "AIGC动画")
        self.assertTrue(any(item["asset_id"] == image.asset_id and item["asset_type"] == "image" for item in plan.native_items))

    def test_native_gate_allows_existing_local_assets_without_effects(self) -> None:
        director, assets = _manifest_pair()
        records = [
            AssetRecord(
                **{
                    **record.__dict__,
                    "local_path": __file__,
                    "remote_url": "",
                }
            )
            for record in assets.records
        ]
        local_assets = AssetRegistry(
            project_id=assets.project_id,
            run_id=assets.run_id,
            source_plan_version=assets.source_plan_version,
            asset_version=assets.asset_version,
            records=records,
            status=assets.status,
        )
        plan = compile_editing_layer(director, local_assets)
        validate_windows_native_plan(plan)


if __name__ == "__main__":
    unittest.main()
