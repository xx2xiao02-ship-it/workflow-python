from __future__ import annotations

import base64

import pytest

from workflow_1256.ark_tts_transport import (
    ArkTTSConfig,
    ArkTTSConfigError,
    ArkTTSAudioPublisherRequired,
    ArkTTSHTTPTransport,
    ArkTTSTransportError,
    DEFAULT_TTS_SPEAKER_ID,
    _json_objects,
    load_tts_api_keys_from_auth_document,
)
from workflow_1256.speech_synthesis import build_request


def _response(*, data: str, end_time: float = 2.5, code: int = 0, message: str = "ok") -> dict:
    return {
        "status_code": 200,
        "headers": {"x-tt-logid": "official-log"},
        "chunks": [
            {"code": code, "data": data[:4]},
            {"code": code, "data": data[4:], "words": [{"endTime": end_time}]},
        ],
        "body": {"code": code, "message": message, "words": [{"endTime": end_time}]},
    }


def test_tts_config_does_not_fallback_to_voice_clone_openapi_ak_sk(monkeypatch):
    """OpenAPI management credentials must never make TTS appear configured."""

    for name in (
        "ARK_TTS_API_KEY",
        "ARK_AUDIO_API_KEY",
        "VOLCENGINE_AUDIO_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_TTS_AUTH_DOCUMENT", "")
    monkeypatch.setenv("VOICE_CLONE_ADMIN_ACCESS_KEY", "management-ak")
    monkeypatch.setenv("VOICE_CLONE_ADMIN_SECRET_KEY", "management-sk")

    with pytest.raises(ArkTTSConfigError, match="未配置 ARK_TTS_API_KEY"):
        ArkTTSConfig.from_env()


def test_official_tts_builds_unidirectional_request_and_publishes_audio() -> None:
    calls = []
    published = []
    encoded = base64.b64encode(b"synthetic-mp3").decode()

    def requester(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        return _response(data=encoded)

    def publisher(audio, filename, metadata):
        published.append((audio, filename, metadata))
        return {"url": "https://audio.example/tts.mp3"}

    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id="BV-official"),
        requester=requester,
        audio_publisher=publisher,
        request_id_factory=lambda: "request-1",
    )
    result = transport.synthesize(
        build_request({"text": "测试语音", "speed_ratio": 1.1, "voice_id": "coze-id"})
    )

    assert result == {
        "code": 0,
        "data": {"duration": 2.5, "link": "https://audio.example/tts.mp3"},
        "log_id": "official-log",
        "msg": "ok",
    }
    assert calls[0][0].endswith("/api/v3/tts/unidirectional")
    assert calls[0][1]["X-Api-Key"] == "not-real"
    assert calls[0][1]["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert calls[0][1]["X-Api-Request-Id"] == "request-1"
    assert calls[0][2] == {
        "req_params": {
            "text": "测试语音",
            "speaker": "BV-official",
            "audio_params": {
                "format": "mp3",
                "sample_rate": 48000,
                "speech_rate": 10,
                "loudness_rate": 0,
                "enable_subtitle": True,
            },
        }
    }
    assert published[0][0] == b"synthetic-mp3"


def test_custom_voice_request_uses_catalog_bound_resource_without_internal_model_alias():
    calls = []
    encoded = base64.b64encode(b"custom-audio").decode()
    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id=""),
        requester=lambda url, headers, payload, timeout: (calls.append((headers, payload)) or _response(data=encoded)),
    )
    request = build_request({
        "text": "定制音色",
        "speed_ratio": 1,
        "voice_id": "custom-speaker",
        "speaker_id": "custom-speaker",
        "resource_id": "seed-icl-2.0",
        "model": "seed-tts-2.0-standard",
    })
    result = transport.synthesize_local(request)
    assert result["audio"] == b"custom-audio"
    assert calls[0][0]["X-Api-Resource-Id"] == "seed-icl-2.0"
    assert "model" not in calls[0][1]["req_params"]
    assert calls[0][1]["req_params"]["speaker"] == "custom-speaker"


def test_custom_voice_request_preserves_icl_one_resource_binding():
    calls = []
    encoded = base64.b64encode(b"icl1-custom-audio").decode()
    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id=""),
        requester=lambda url, headers, payload, timeout: (
            calls.append((headers, payload)) or _response(data=encoded)
        ),
    )
    request = build_request({
        "text": "旧版定制音色",
        "speed_ratio": 1,
        "voice_id": "icl1-custom-speaker",
        "speaker_id": "icl1-custom-speaker",
        "resource_id": "seed-icl-1.0",
        "model": "seed-tts-1.0-standard",
    })
    result = transport.synthesize_local(request)
    assert result["audio"] == b"icl1-custom-audio"
    assert calls[0][0]["X-Api-Resource-Id"] == "seed-icl-1.0"
    assert calls[0][1]["req_params"]["speaker"] == "icl1-custom-speaker"


def test_explicit_provider_model_is_forwarded_when_not_internal_default_alias():
    calls = []
    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id="BV-official"),
        requester=lambda _url, _headers, payload, _timeout: (
            calls.append(payload) or _response(data=base64.b64encode(b"audio").decode())
        ),
    )
    transport.synthesize_local(build_request({
        "text": "显式模型",
        "speed_ratio": 1,
        "voice_id": "BV-official",
        "model": "seed-tts-1.1",
    }))
    assert calls[0]["req_params"]["model"] == "seed-tts-1.1"


def test_official_tts_forwards_level_three_performance_controls() -> None:
    calls = []
    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id="BV-official"),
        requester=lambda _u, _h, payload, _t: (calls.append(payload) or _response(data=base64.b64encode(b"audio").decode())),
        audio_publisher=lambda *_args: {"url": "https://audio.example/tts.mp3"},
    )
    transport.synthesize(build_request({"text": "测试", "speed_ratio": 1.0, "voice_id": "v", "emotion": "坚定", "emotion_scale": 0.7, "loudness_rate": 10, "pitch": 2, "silence_duration": 300, "dialect": "sichuan"}))
    audio = calls[0]["req_params"]["audio_params"]
    assert audio["loudness_rate"] == 10 and audio["pitch"] == 2 and audio["silence_duration"] == 300
    assert calls[0]["req_params"]["context_texts"] == ["请使用坚定的语气朗读，情绪强度为0.7。"]


def test_official_tts_blocks_before_network_without_audio_publisher() -> None:
    called = False

    def requester(*_args):
        nonlocal called
        called = True
        raise AssertionError("publisher 缺失时不应发起请求")

    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id="BV-official"),
        requester=requester,
    )
    with pytest.raises(ArkTTSAudioPublisherRequired):
        transport.synthesize(
            build_request({"text": "x", "speed_ratio": 1, "voice_id": "v"})
        )
    assert called is False


def test_official_tts_local_preview_returns_audio_without_publisher() -> None:
    encoded = base64.b64encode(b"local-preview-mp3").decode()
    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id="BV-official"),
        requester=lambda *_args: _response(data=encoded),
        request_id_factory=lambda: "preview-1",
    )

    result = transport.synthesize_local(
        build_request({"text": "试听", "speed_ratio": 1, "voice_id": "v"})
    )

    assert result["audio"] == b"local-preview-mp3"
    assert result["duration"] == 2.5
    assert result["request_id"] == "preview-1"


def test_tts_switches_to_backup_after_primary_resource_failure() -> None:
    encoded = base64.b64encode(b"backup-audio").decode()
    calls = []

    def requester(_url, headers, _payload, _timeout):
        calls.append(headers["X-Api-Key"])
        if len(calls) == 1:
            return _response(
                data="",
                code=55_000_000,
                message="resource ID is mismatched with speaker related resource",
            )
        return _response(data=encoded)

    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(
            api_key="primary-key",
            backup_api_key="backup-key",
            speaker_id="zh_male_qingcang_uranus_bigtts",
        ),
        requester=requester,
        duration_probe=lambda _audio, _format: 1.0,
    )
    result = transport.synthesize_local(
        build_request({"text": "试听", "speed_ratio": 1, "voice_id": "v"})
    )

    assert calls == ["primary-key", "backup-key"]
    assert result["audio"] == b"backup-audio"
    assert result["metadata"]["auth_slot"] == "backup"


def test_tts_failover_keeps_custom_voice_binding_across_accounts() -> None:
    encoded = base64.b64encode(b"backup-custom-audio").decode()
    calls = []

    def requester(_url, headers, payload, _timeout):
        calls.append((headers["X-Api-Key"], headers["X-Api-Resource-Id"], payload["req_params"]["speaker"]))
        if len(calls) == 1:
            return _response(
                data="",
                code=55_000_000,
                message="resource ID is mismatched with speaker related resource",
            )
        return _response(data=encoded)

    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="account-a-key", backup_api_key="account-b-key"),
        requester=requester,
        duration_probe=lambda _audio, _format: 1.0,
    )
    result = transport.synthesize_local(build_request({
        "text": "跨账号定制音色",
        "speed_ratio": 1,
        "voice_id": "S_CUSTOM",
        "speaker_id": "S_CUSTOM",
        "resource_id": "seed-icl-2.0",
        "model": "seed-tts-2.0-standard",
    }))

    assert calls == [
        ("account-a-key", "seed-icl-2.0", "S_CUSTOM"),
        ("account-b-key", "seed-icl-2.0", "S_CUSTOM"),
    ]
    assert result["metadata"]["auth_slot"] == "backup"
    assert result["metadata"]["voice_binding"] == {
        "speaker_id": "S_CUSTOM",
        "resource_id": "seed-icl-2.0",
        "model": "seed-tts-2.0-standard",
    }


def test_custom_voice_can_explicitly_disable_cross_account_failover() -> None:
    calls = []

    def requester(_url, headers, _payload, _timeout):
        calls.append(headers["X-Api-Key"])
        return _response(data="", code=55_000_000, message="permission denied")

    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(
            api_key="account-a-key",
            backup_api_key="account-b-key",
            failover_strategy="primary_only",
        ),
        requester=requester,
    )
    with pytest.raises(ArkTTSTransportError, match="permission denied"):
        transport.synthesize_local(build_request({
            "text": "只允许主账号",
            "speed_ratio": 1,
            "voice_id": "S_CUSTOM",
            "speaker_id": "S_CUSTOM",
            "resource_id": "seed-icl-2.0",
            "model": "seed-tts-2.0-standard",
        }))

    assert calls == ["account-a-key"]


def test_default_speaker_is_qingcang_2() -> None:
    assert DEFAULT_TTS_SPEAKER_ID == "zh_male_qingcang_uranus_bigtts"


def test_official_tts_rejects_old_resource_id() -> None:
    with pytest.raises(ArkTTSConfigError):
        ArkTTSHTTPTransport(
            ArkTTSConfig(api_key="not-real", resource_id="seed-audio-1.0")
        )


def test_official_tts_requires_http_published_url() -> None:
    encoded = base64.b64encode(b"audio").decode()
    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id="BV-official"),
        requester=lambda *_args: _response(data=encoded),
        audio_publisher=lambda *_args: "C:\\audio\\tts.mp3",
    )
    with pytest.raises(RuntimeError, match=r"http\(s\)"):
        transport.synthesize(
            build_request({"text": "x", "speed_ratio": 1, "voice_id": "v"})
        )


def test_official_tts_accepts_documented_success_code_20000000() -> None:
    encoded = base64.b64encode(b"audio").decode()
    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id="BV-official"),
        requester=lambda *_args: _response(data=encoded, code=20_000_000),
        audio_publisher=lambda *_args: {"url": "https://audio.example/tts.mp3"},
    )
    result = transport.synthesize(
        build_request({"text": "x", "speed_ratio": 1, "voice_id": "v"})
    )
    assert result["code"] == 20_000_000


def test_official_tts_probes_duration_when_response_has_no_timestamps() -> None:
    encoded = base64.b64encode(b"audio").decode()
    published = []

    def publisher(audio, filename, metadata):
        published.append(metadata)
        return {"url": "https://audio.example/tts.mp3"}

    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id="BV-official"),
        requester=lambda *_args: {
            "status_code": 200,
            "headers": {},
            "chunks": [{"code": 20_000_000, "data": encoded}],
            "body": {"code": 20_000_000, "message": "OK"},
        },
        audio_publisher=publisher,
        duration_probe=lambda audio, audio_format: 1.75,
    )
    result = transport.synthesize(
        build_request({"text": "x", "speed_ratio": 1, "voice_id": "v"})
    )
    assert result["data"]["duration"] == 1.75
    assert published[0]["duration"] == 1.75


def test_chunked_json_parser_accepts_concatenated_json_objects() -> None:
    assert _json_objects(b'{"code":0}{"code":0,"data":"YQ=="}') == [
        {"code": 0},
        {"code": 0, "data": "YQ=="},
    ]


def test_load_tts_keys_only_from_tts_section(tmp_path) -> None:
    auth = tmp_path / "auth.md"
    auth.write_text("# Seedance\nseedance-secret-012345678901234\n# TTS 豆包\n主 tts-primary-012345678901234567890123\n备 tts-backup-012345678901234567890123\n# other\nother-secret-012345678901234567890123", encoding="utf-8")
    assert load_tts_api_keys_from_auth_document(auth) == [
        "tts-primary-012345678901234567890123",
        "tts-backup-012345678901234567890123",
    ]


def test_explicit_empty_tts_document_disables_document_fallback(monkeypatch) -> None:
    for name in (
        "ARK_TTS_API_KEY",
        "ARK_AUDIO_API_KEY",
        "ARK_API_KEY",
        "VOLCENGINE_AUDIO_API_KEY",
        "ARK_TTS_BACKUP_API_KEY",
        "ARK_AUDIO_BACKUP_API_KEY",
        "VOLCENGINE_AUDIO_BACKUP_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_TTS_AUTH_DOCUMENT", "")
    monkeypatch.setattr(
        "workflow_1256.ark_tts_transport.load_tts_api_keys_from_auth_document",
        lambda _path: pytest.fail("页面凭据模式不应读取历史 TTS 鉴权文档"),
    )

    with pytest.raises(ArkTTSConfigError, match="未配置 ARK_TTS_API_KEY"):
        ArkTTSConfig.from_env()


def test_new_voice_clone_preview_can_disable_legacy_document_fallback(monkeypatch, tmp_path) -> None:
    legacy = tmp_path / "鉴权信息 .md"
    legacy.write_text(
        "# TTS\nlegacy-tts-key-012345678901234567890123\n",
        encoding="utf-8",
    )
    for name in (
        "ARK_TTS_API_KEY",
        "ARK_AUDIO_API_KEY",
        "VOLCENGINE_AUDIO_API_KEY",
        "ARK_TTS_BACKUP_API_KEY",
        "ARK_AUDIO_BACKUP_API_KEY",
        "VOLCENGINE_AUDIO_BACKUP_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_TTS_AUTH_DOCUMENT", str(legacy))

    with pytest.raises(ArkTTSConfigError, match="未配置 ARK_TTS_API_KEY"):
        ArkTTSConfig.from_env(allow_legacy_document=False)


def test_tts_from_env_never_reads_legacy_document_by_default(monkeypatch, tmp_path) -> None:
    legacy = tmp_path / "鉴权信息 .md"
    legacy.write_text(
        "# TTS\nlegacy-tts-key-012345678901234567890123\n",
        encoding="utf-8",
    )
    for name in (
        "ARK_TTS_API_KEY",
        "ARK_AUDIO_API_KEY",
        "VOLCENGINE_AUDIO_API_KEY",
        "ARK_TTS_BACKUP_API_KEY",
        "ARK_AUDIO_BACKUP_API_KEY",
        "VOLCENGINE_AUDIO_BACKUP_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_TTS_AUTH_DOCUMENT", str(legacy))
    monkeypatch.setattr(
        "workflow_1256.ark_tts_transport.load_tts_api_keys_from_auth_document",
        lambda _path: pytest.fail("新版 TTS 默认不应读取历史鉴权文档"),
    )

    with pytest.raises(ArkTTSConfigError, match="未配置 ARK_TTS_API_KEY"):
        ArkTTSConfig.from_env()


def test_tts_config_does_not_reuse_generic_story_ark_key(monkeypatch) -> None:
    for name in (
        "ARK_TTS_API_KEY",
        "ARK_AUDIO_API_KEY",
        "VOLCENGINE_AUDIO_API_KEY",
        "ARK_TTS_BACKUP_API_KEY",
        "ARK_AUDIO_BACKUP_API_KEY",
        "VOLCENGINE_AUDIO_BACKUP_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_TTS_AUTH_DOCUMENT", "")
    monkeypatch.setenv("ARK_API_KEY", "story-only-key")

    with pytest.raises(ArkTTSConfigError, match="未配置 ARK_TTS_API_KEY"):
        ArkTTSConfig.from_env()


def test_v3_tts_from_env_ignores_legacy_endpoint_model_and_resource(monkeypatch) -> None:
    """旧环境变量不能把新版 TTS 请求切回旧 endpoint 或资源。"""

    monkeypatch.setenv("ARK_TTS_API_KEY", "tts-primary")
    monkeypatch.setenv("ARK_TTS_API_URL", "https://open.volcengineapi.com/")
    monkeypatch.setenv("ARK_AUDIO_API_URL", "https://legacy.example/tts/create")
    monkeypatch.setenv("ARK_TTS_MODEL", "seed-audio-1.0-multilingual")
    monkeypatch.setenv("ARK_TTS_RESOURCE_ID", "seed-icl-1.0")
    monkeypatch.delenv("ARK_TTS_SPEAKER_ID", raising=False)

    config = ArkTTSConfig.from_env()

    assert config.api_url == "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    assert config.model == "seed-tts-2.0-standard"
    assert config.resource_id == "seed-tts-2.0"


def test_v3_tts_transport_rejects_legacy_endpoint() -> None:
    with pytest.raises(ArkTTSConfigError, match="endpoint 固定"):
        ArkTTSHTTPTransport(
            ArkTTSConfig(
                api_key="tts-primary",
                api_url="https://open.volcengineapi.com/",
            )
        )


def test_official_tts_does_not_use_coze_voice_id_as_ark_speaker() -> None:
    called = False

    def requester(*_args):
        nonlocal called
        called = True
        raise AssertionError("speaker 缺失时不应发起请求")

    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real"),
        requester=requester,
        audio_publisher=lambda *_args: {"url": "https://audio.example/tts.mp3"},
    )
    with pytest.raises(ArkTTSConfigError, match="speaker"):
        transport.synthesize(
            build_request({"text": "x", "speed_ratio": 1, "voice_id": "7468518920446607398"})
        )
    assert called is False


def test_official_tts_rejects_configured_speaker_overriding_request() -> None:
    called = False

    def requester(*_args):
        nonlocal called
        called = True
        raise AssertionError("音色不一致时不应发起请求")

    transport = ArkTTSHTTPTransport(
        ArkTTSConfig(api_key="not-real", speaker_id="voice-from-env"),
        requester=requester,
    )
    with pytest.raises(ArkTTSConfigError, match="不一致"):
        transport.synthesize_local(
            build_request({
                "text": "x",
                "speed_ratio": 1,
                "voice_id": "voice-request",
                "speaker_id": "voice-request",
            })
        )
    assert called is False
