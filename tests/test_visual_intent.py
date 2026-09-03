from __future__ import annotations

import pytest

from workflow_1256.visual_intent import (
    VisualIntentTransportRequired,
    VisualIntentValidationError,
    run_visual_intent,
)


PARAMS = {
    "segments": ["第一段", "第二段"],
    "director_plan": {"core": "核心"},
}


def response(items_count: int = 2):
    return {
        "items": [
            {"visual_core": f"core-{index}", "visual_story": f"story-{index}"}
            for index in range(items_count)
        ],
        "music_cues": [{
            "emotion": "紧张",
            "energy": 3,
            "music_direction": "渐强",
            "music_role": "推进",
            "transition_to_next": "收束",
        }],
        "reasoning_content": "reason",
    }


def test_visual_intent_normalizes_yaml_output_order() -> None:
    result = run_visual_intent(PARAMS, transport=lambda _request: response())

    assert list(result) == ["items", "music_cues", "reasoning_content"]
    assert len(result["items"]) == 2
    assert result["music_cues"][0]["energy"] == 3


def test_visual_intent_requires_real_transport() -> None:
    with pytest.raises(VisualIntentTransportRequired):
        run_visual_intent(PARAMS)


def test_visual_intent_rejects_index_mismatch() -> None:
    with pytest.raises(VisualIntentValidationError, match="数量"):
        run_visual_intent(PARAMS, transport=lambda _request: response(1))


def test_visual_intent_rejects_invalid_music_energy() -> None:
    invalid = response()
    invalid["music_cues"][0]["energy"] = True
    with pytest.raises(VisualIntentValidationError, match="energy"):
        run_visual_intent(PARAMS, transport=lambda _request: invalid)
