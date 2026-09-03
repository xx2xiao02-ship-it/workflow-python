from __future__ import annotations

from copy import deepcopy

from workflow_1256.layers import lock_director_output
from workflow_1256.editing_layer import compile_editing_layer
from workflow_1256.governance import AssetRecord, AssetRegistry, TimeWindow


def _story() -> dict:
    return {
        "movie_outline": {
            "protagonist": "tester", "protagonist_goal": "finish",
            "central_conflict": "time", "character_arc": "grow", "ending_hook": "next",
        },
        "silent_story_text": "A protagonist completes a task.",
        "scene_groups": [{
            "scene_id": "scene_01", "group_ids": ["g01"], "scene_purpose": "setup",
            "state_before": "start", "visible_conflict": "time", "turn": "act", "state_after": "finish",
        }],
        "narration_mappings": [{
            "group_id": "g01", "scene_id": "scene_01", "semantic_mapping": "work",
            "silent_action": "act", "metaphor": "work", "state_before": "start",
            "state_after": "finish", "bridge_to_next": "end",
        }],
    }


def _artifacts() -> dict:
    return {
        "director_output": {
            "segments": ["segment"],
            "segment_beats": [{
                "segment_index": 0, "segment_text": "segment", "rhythm": "fast",
                "segment_goal": "hook", "beats": [{"route_candidates": ["scene", "digital_human"]}],
            }],
        },
        "tts_group_timelines": [{"start": 0, "end": 4_000_000}],
        "total_timeline": {"start": 0, "end": 4_000_000},
        "shot_groups": [{
            "shots": [{"source_text": "shot", "clip_role": "hook", "story_beat": "setup"}],
            "timelines": [{"start": 0, "end": 4_000_000}],
        }],
        "caption_segments": [], "caption_timelines": [],
        "project_id": "test", "run_id": "run", "plan_version": "v1", "tts_fingerprint": "tts",
    }


def test_selected_digital_human_route_cannot_override_full_video_first_shot() -> None:
    data = deepcopy(_artifacts())
    data["shot_groups"][0]["shots"][0]["selected_route"] = "digital_human"
    manifest = lock_director_output(
        **data,
        cinematic_story=_story(),
        story_review_status="APPROVED",
    )

    first_shot = manifest.shots[0]
    requirement_ids = {item.requirement_id for item in manifest.requirements}
    assert first_shot.requirement_ids == [
        "g01_s01.video.aigc",
        "g01_s01.image.first_frame",
        "g01_s01.prompt.video",
    ]
    assert first_shot.production_spec.get("selected_route") is None
    assert "g01_s01.video.digital_human" not in requirement_ids
    assert {"g01_s01.video.aigc", "g01_s01.image.first_frame", "g01_s01.prompt.video"}.issubset(requirement_ids)


def test_digital_human_candidate_without_selection_is_not_skipped() -> None:
    data = deepcopy(_artifacts())
    manifest = lock_director_output(
        **data,
        cinematic_story=_story(),
        story_review_status="APPROVED",
    )

    first_shot = manifest.shots[0]
    assert first_shot.production_spec.get("selected_route") is None
    assert first_shot.requirement_ids == [
        "g01_s01.video.aigc",
        "g01_s01.image.first_frame",
        "g01_s01.prompt.video",
    ]


def test_editing_layer_keeps_digital_human_placeholder_only_in_selected_slot(tmp_path) -> None:
    data = deepcopy(_artifacts())
    data["shot_groups"][0]["shots"] = [
        {"source_text": "ordinary", "clip_role": "hook", "story_beat": "setup"},
        {"source_text": "host", "clip_role": "turn", "story_beat": "turn", "selected_route": "digital_human"},
    ]
    data["shot_groups"][0]["timelines"] = [
        {"start": 0, "end": 2_000_000},
        {"start": 2_000_000, "end": 5_000_000},
    ]
    data["tts_group_timelines"] = [{"start": 0, "end": 5_000_000}]
    data["total_timeline"] = {"start": 0, "end": 5_000_000}
    manifest = lock_director_output(**data, cinematic_story=_story(), story_review_status="APPROVED")
    media = tmp_path / "placeholder.jpg"
    media.write_bytes(b"placeholder")
    records = [
        AssetRecord(
            asset_id="asset.static", requirement_id="g01_s01.image.first_frame", group_id="g01",
            shot_ids=["g01_s01"], asset_type="image", role="primary_visual_image", status="VALIDATED",
            source_node="frame", source_index=0, asset_version="assets-v1", local_path=str(media),
        ),
        AssetRecord(
            asset_id="asset.host", requirement_id="g01_s02.video.digital_human", group_id="g01",
            shot_ids=["g01_s02"], asset_type="image", role="digital_human_placeholder", status="VALIDATED",
            source_node="digital_human", source_index=2, asset_version="assets-v1", local_path=str(media),
        ),
    ]
    assets = AssetRegistry(
        project_id="test", run_id="run", source_plan_version="v1", asset_version="assets-v1",
        records=records, status="ASSETS_READY",
    )
    plan = compile_editing_layer(manifest, assets)
    items = {item["asset_id"]: item for item in plan.native_items}
    assert items["asset.static"]["track_name"] == "AIGC动画"
    assert items["asset.static"]["start_us"] == 0
    assert items["asset.static"]["end_us"] == 2_000_000
    # 数字人是主视觉视频来源，必须与 AIGC 视频共用同一轨道；其坑位仍由
    # digital_human requirement 精确限定，不能覆盖其他镜头。
    assert items["asset.host"]["track_name"] == "AIGC动画"
    assert items["asset.host"]["start_us"] == 2_000_000
    assert items["asset.host"]["end_us"] == 5_000_000
    assert not any("g01_s02.image.first_frame" == item["requirement_id"] for item in plan.native_items)
    assert not any("g01_s02.video.aigc" == item["requirement_id"] for item in plan.native_items)
