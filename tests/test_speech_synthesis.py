from __future__ import annotations

import pytest

from workflow_1256.speech_synthesis import (
    SpeechSynthesisTransportRequired,
    SpeechSynthesisValidationError,
    run_speech_synthesis,
)


PARAMS = {"text": "测试语音", "speed_ratio": 1.0, "voice_id": "voice-local"}


def test_speech_synthesis_preserves_yaml_output_shape() -> None:
    result = run_speech_synthesis(
        PARAMS,
        transport=lambda request: {
            "data": {"duration": 2, "link": "https://example.invalid/audio.mp3"},
            "log_id": "log-1",
            "msg": f"ok:{request.voice_id}",
        },
    )

    assert list(result) == ["data", "log_id", "msg"]
    assert result["data"]["duration"] == 2.0
    assert result["msg"] == "ok:voice-local"


def test_speech_synthesis_requires_real_transport() -> None:
    with pytest.raises(SpeechSynthesisTransportRequired):
        run_speech_synthesis(PARAMS)


@pytest.mark.parametrize(
    "params, message",
    [
        ({"text": "", "speed_ratio": 1, "voice_id": "voice"}, "text"),
        ({"text": "x", "speed_ratio": "1", "voice_id": "voice"}, "speed_ratio"),
        ({"text": "x", "speed_ratio": 1, "voice_id": ""}, "voice_id"),
    ],
)
def test_speech_synthesis_rejects_invalid_inputs(params, message) -> None:
    with pytest.raises(SpeechSynthesisValidationError, match=message):
        run_speech_synthesis(params, transport=lambda _request: {})


def test_speech_synthesis_rejects_missing_audio_link() -> None:
    with pytest.raises(SpeechSynthesisValidationError, match="link"):
        run_speech_synthesis(
            PARAMS,
            transport=lambda _request: {
                "data": {"duration": 2.0, "link": ""},
                "log_id": "log",
                "msg": "ok",
            },
        )


def test_speech_synthesis_accepts_visible_optional_fields() -> None:
    result = run_speech_synthesis(
        {
            **PARAMS,
            "emotion": "冷静",
            "emotion_scale": 0.7,
            "language": "中文",
            "speaker_id": "speaker-local",
            "loudness_rate": 10,
            "pitch": 2,
            "tail_silence_ms": 300,
            "dialect": "sichuan",
        },
        transport=lambda request: {
            "code": 0,
            "data": {"duration": 1.5, "link": "https://example.invalid/audio.mp3"},
            "log_id": "log",
            "msg": request.speaker_id or "ok",
        },
    )
    assert list(result) == ["code", "data", "log_id", "msg"]
    assert result["code"] == 0
    assert result["msg"] == "speaker-local"


def test_speech_synthesis_rejects_out_of_range_performance_controls() -> None:
    with pytest.raises(SpeechSynthesisValidationError, match="pitch"):
        run_speech_synthesis({**PARAMS, "pitch": 13}, transport=lambda _request: {})
