import pytest

from workflow_1256.voice_director import normalize_voice_performance_plan


def test_voice_director_locks_segment_coverage_and_performance_controls():
    result = normalize_voice_performance_plan({"segments": [{"segment_id": "g01", "text": "第一段。", "emotion": "克制", "emotion_scale": 0.6, "speed_ratio": 1.05, "loudness_rate": 4, "pitch": 1, "tail_silence_ms": 300, "pause_plan": [{"after": "第一段", "duration_ms": 300}]}]}, ["第一段。"])
    assert result["segments"][0]["ssml_status"] == "planned_transport_pending"


def test_voice_director_enforces_input_text_and_drops_invalid_pause():
    result = normalize_voice_performance_plan({"segments": [{"segment_id": "g01", "text": "模型改写", "emotion": "自然", "pause_plan": []}]}, ["原始文案"])
    assert result["segments"][0]["text"] == "原始文案"
    result = normalize_voice_performance_plan({"segments": [{"segment_id": "g01", "text": "原始文案", "emotion": "自然", "pause_plan": [{"after": "不存在", "duration_ms": 300}]}]}, ["原始文案"])
    assert result["segments"][0]["pause_plan"] == []
