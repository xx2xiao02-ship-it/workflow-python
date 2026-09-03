from __future__ import annotations

import base64

import pytest

from workflow_1256.ark_audio_transport import (
    ArkAudioConfig,
    ArkAudioConfigError,
    ArkAudioHTTPTransport,
)
from workflow_1256.speech_synthesis import build_request, run_speech_synthesis


def test_ark_tts_uses_official_url_and_new_single_header() -> None:
    calls = []

    def requester(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        return {
            "status_code": 200,
            "headers": {"x-tt-logid": "ark-log"},
            "body": {
                "code": 0,
                "duration": 2.5,
                "original_duration": 2.6,
                "url": "https://example.invalid/tts.mp3",
            },
        }

    transport = ArkAudioHTTPTransport(
        ArkAudioConfig(api_key="key-not-real", timeout=7),
        requester=requester,
        request_id_factory=lambda: "request-1",
    )
    result = run_speech_synthesis(
        {"text": "测试", "speed_ratio": 1.1, "voice_id": "7468518920446607398"},
        transport=transport.synthesize,
    )

    assert result == {
        "code": 0,
        "data": {"duration": 2.5, "link": "https://example.invalid/tts.mp3"},
        "log_id": "ark-log",
        "msg": "ok",
    }
    url, headers, payload, timeout = calls[0]
    assert url == "https://openspeech.bytedance.com/api/v3/tts/create"
    assert headers["X-Api-Key"] == "key-not-real"
    assert "X-Api-App-Id" not in headers
    assert headers["X-Api-Request-Id"] == "request-1"
    assert payload["model"] == "seed-audio-1.0-multilingual"
    assert payload["references"] == [{"speaker": "7468518920446607398"}]
    assert payload["audio_config"]["speech_rate"] == 10
    assert timeout == 7


def test_ark_tts_supports_legacy_dual_auth_without_exposing_values() -> None:
    observed = {}

    def requester(_url, headers, _payload, _timeout):
        observed.update(headers)
        return {
            "body": {
                "code": 0,
                "duration": 1,
                "url": "https://example.invalid/tts.mp3",
            },
            "headers": {},
        }

    transport = ArkAudioHTTPTransport(
        ArkAudioConfig(app_id="app-not-real", access_key="access-not-real"),
        requester=requester,
    )
    transport.synthesize(build_request({"text": "x", "speed_ratio": 1, "voice_id": "v"}))
    assert observed["X-Api-App-Id"] == "app-not-real"
    assert observed["X-Api-Access-Key"] == "access-not-real"
    assert "X-Api-Key" not in observed


def test_ark_audio_config_requires_one_auth_mode() -> None:
    try:
        ArkAudioHTTPTransport(ArkAudioConfig())
    except ArkAudioConfigError:
        pass
    else:
        raise AssertionError("缺少鉴权时必须阻断")
def test_ark_audio_config_reuses_tts_api_key_for_bgm(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ARK_AUDIO_API_KEY",
        "ARK_BGM_API_KEY",
        "VOLCENGINE_AUDIO_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_TTS_API_KEY", "shared-key")
    config = ArkAudioConfig.from_env()
    assert config.api_key == "shared-key"


def test_ark_audio_config_accepts_generic_ark_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ARK_AUDIO_API_KEY",
        "ARK_BGM_API_KEY",
        "ARK_TTS_API_KEY",
        "VOLCENGINE_AUDIO_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_API_KEY", "generic-key")
    assert ArkAudioConfig.from_env().api_key == "generic-key"


def test_bgm_base64_response_is_published_before_contract_mapping() -> None:
    published = {}

    def requester(_url, _headers, _payload, _timeout):
        return {
            "body": {
                "code": 0,
                "duration": 3.5,
                "audio": base64.b64encode(b"fake-mp3").decode("ascii"),
            },
            "headers": {},
        }

    def publisher(audio, filename, metadata):
        published.update({"audio": audio, "filename": filename, "metadata": metadata})
        return {"url": "https://storage.example/bgm.mp3", "duration": 3.5}

    transport = ArkAudioHTTPTransport(
        ArkAudioConfig(api_key="key-not-real"),
        requester=requester,
        audio_publisher=publisher,
    )
    result = transport.generate_bgm(
        {
            "Duration": 30,
            "Text": "instrumental",
            "Genre": ["documentary"],
            "Instrument": ["piano"],
            "Mood": ["calm"],
        }
    )

    assert result["data"]["SongDetail"]["AudioUrl"] == "https://storage.example/bgm.mp3"
    assert result["data"]["SongDetail"]["Duration"] == 3.5
    assert published["audio"] == b"fake-mp3"
