from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow_1256.governance import (
    DirectorContext,
    GovernanceContractError,
)


ROOT = Path(__file__).resolve().parents[1]


def _director_output() -> dict:
    return {
        "text": "第一段",
        "director_plan": {"goal": "说明"},
        "segments": ["第一段"],
        "segment_beats": [{"segment_index": 0, "rhythm": "steady"}],
    }


def test_context_views_preserve_original_node_fields_and_order() -> None:
    context = DirectorContext.from_director_output(_director_output())
    context = context.with_visual_intent({
        "items": [{"visual_core": "主体", "visual_story": "动作"}],
        "music_cues": [],
        "reasoning_content": "reason",
    })
    context = context.with_speech_links(["synthetic://audio/1"])
    context = context.with_timing(
        link_list=["synthetic://audio/1"],
        group_timelines=[{"start": 0, "end": 2_000_000}],
        all_timelines=[{"start": 0, "end": 2_000_000}],
    )
    context = context.with_timing(
        link_list=["synthetic://audio/1"],
        group_timelines=[{"start": 0, "end": 2_000_000}],
        all_timelines=[{"start": 0, "end": 2_000_000}],
        subtitle_output={
            "new_segments": ["第一段"],
            "new_timelines": [{"start": 0, "end": 2_000_000}],
            "duration": [2.0],
        },
    )
    context = context.with_shots({
        "Code_list": [{"shots": [], "timelines": []}],
        "LLM_list": [{"shots": []}],
    })

    assert context.visual_intent_input() == {
        "segments": ["第一段"],
        "director_plan": {"goal": "说明"},
    }
    assert context.timeline_input() == {"links": ["synthetic://audio/1"]}
    assert context.subtitle_input()["segment_text"] == ["第一段"]
    with pytest.raises(GovernanceContractError, match="DirectorLockedManifest"):
        context.scene_type_input()


def test_context_builds_tts_defaults_and_bgm_input_from_locked_timing() -> None:
    context = DirectorContext.from_director_output(_director_output())
    context = context.with_visual_intent({
        "items": [{"visual_core": "涓讳綋", "visual_story": "鍔ㄤ綔"}],
        "music_cues": [{
            "music_role": "推进",
            "emotion": "稳定",
            "energy": 3,
            "music_direction": "steady",
            "transition_to_next": "crossfade",
        }],
        "reasoning_content": "reason",
    })
    context = context.with_timing(
        link_list=["https://synthetic.example/audio.mp3"],
        group_timelines=[{"start": 0, "end": 2_000_000}],
        all_timelines=[{"start": 0, "end": 2_000_000}],
        subtitle_output={
            "new_segments": ["第一段"],
            "new_timelines": [{"start": 0, "end": 2_000_000}],
            "duration": [2.0],
        },
    )

    assert context.speech_input({}, 0) == {
        "speed_ratio": 1.1,
        "voice_id": "zh_male_qingcang_uranus_bigtts",
        "text": "第一段",
    }
    enriched = context.speech_input({}, 0, performance={"emotion": "坚定", "pitch": 2, "tail_silence_ms": 300})
    assert enriched["emotion"] == "坚定"
    assert enriched["pitch"] == 2
    assert enriched["tail_silence_ms"] == 300
    assert context.speech_input({"overall_speed_ratio": 1.2}, 0)["speed_ratio"] == 1.2
    assert context.bgm_input() == {
        "timelines": [{"start": 0, "end": 2_000_000}],
        "music_cues": [{
            "music_role": "推进",
            "emotion": "稳定",
            "energy": 3,
            "music_direction": "steady",
            "transition_to_next": "crossfade",
        }],
    }

def test_context_does_not_store_final_prompt_in_visual_route_ledger() -> None:
    context = DirectorContext.from_director_output(_director_output())
    context = context.with_visual_arrangement({
        "prompt": ["最终供应商提示词"],
        "ref_image": [],
        "motion_seed": ["seed"],
        "timelines": [{"start": 0, "end": 2_000_000}],
        "int_duration": [2],
        "error": "",
        "debug": {},
    })
    assert context.snapshot()["contains_final_prompt"] is False
    assert "prompt" not in context.visual_route.visual_spec[0]
    assert "motion_seed" in context.visual_route.visual_spec[0]


def test_context_rejects_mismatched_arrays_and_invalid_json_shape() -> None:
    with pytest.raises(GovernanceContractError):
        DirectorContext.from_director_output({
            **_director_output(),
            "segment_beats": [],
        })

    with pytest.raises(GovernanceContractError):
        DirectorContext.from_director_output({
            **_director_output(),
            "segments": '{"not": "an array"}',
        })


def test_context_snapshot_matches_schema() -> None:
    import jsonschema

    context = DirectorContext.from_director_output(_director_output())
    schema = json.loads(
        (ROOT / "schemas" / "director_context_snapshot.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(context.snapshot(), schema)
