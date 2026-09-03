import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import tools.video_production_console as console


def test_voice_schema_requires_runtime_display_and_audition_fields():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "voice_profile.schema.json").read_text(encoding="utf-8")
    )
    required = set(schema["properties"]["voices"]["items"]["required"])
    assert {
        "gender", "languages", "style_tags", "training_status",
        "authorization_status", "preview_url", "catalog_version",
        "created_at", "updated_at",
    } <= required


def test_voice_schema_route_matches_json_schema_required_fields():
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/voices/schema") as response:
            payload = json.loads(response.read().decode("utf-8"))
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas" / "voice_profile.schema.json").read_text(encoding="utf-8")
        )
        expected = set(schema["properties"]["voices"]["items"]["required"])
        assert set(payload["required"]) == expected
    finally:
        server.shutdown()
        server.server_close()


def test_register_existing_custom_speaker_derives_stable_voice_key(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "VOICE_CATALOG_PATH", tmp_path / "voice_catalog.json")
    result = console._register_voice_profile({
        "display_name": "我的定制旁白",
        "speaker_id": "S_EXISTING_001",
    })

    assert result["status"] == "registered"
    assert result["voice_key"].startswith("custom_")
    voice = next(item for item in result["catalog"]["voices"] if item["speaker_id"] == "S_EXISTING_001")
    assert voice["voice_type"] == "custom"
    assert voice["resource_id"] == "seed-icl-2.0"
    assert voice["enabled"] is False
    assert voice["verified"] is False
    assert voice["training_status"] == "registered"


def test_register_console_created_custom_speaker_keeps_console_status(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "VOICE_CATALOG_PATH", tmp_path / "voice_catalog.json")
    result = console._register_voice_profile({
        "confirm": "voice-console-registration",
        "voice_type": "custom",
        "display_name": "控制台创建旁白",
        "speaker_id": "S_CONSOLE_CREATED_001",
        "resource_id": "seed-icl-2.0",
        "training_status": "console_created",
        "authorization_status": "pending",
    })

    voice = next(
        item for item in result["catalog"]["voices"]
        if item["speaker_id"] == "S_CONSOLE_CREATED_001"
    )
    assert voice["training_status"] == "console_created"
    assert voice["authorization_status"] == "pending"
    assert voice["enabled"] is False
    assert voice["verified"] is False


def test_new_tts_task_contract_does_not_write_flat_fields():
    flat_fields = {
        "tts_voice_key": "custom_demo_v1",
        "tts_speaker_id": "S_CUSTOM",
        "tts_resource_id": "seed-icl-2.0",
        "tts_model": "seed-tts-2.0-standard",
        "tts_paths": ["C:/audio/voice_01.mp3"],
        "tts_audio_sha256": ["hash-a"],
        "tts_actual_durations": [1.25],
        "tts_auth_slots": ["primary"],
        "tts_voice_bindings": [{"speaker_id": "S_CUSTOM", "resource_id": "seed-icl-2.0", "model": "seed-tts-2.0-standard"}],
    }

    current = console._tts_contract_only(flat_fields)

    assert current["tts_contract"]["schema_version"] == "tts-voice-binding-v1"
    assert current["tts_contract"]["voice"]["speaker_id"] == "S_CUSTOM"
    assert current["tts_contract"]["voice"]["resource_id"] == "seed-icl-2.0"
    assert current["tts_contract"]["audio"]["actual_durations_s"] == [1.25]
    assert not any(key.startswith("tts_") for key in current if key != "tts_contract")

    import jsonschema

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "tts_voice_binding.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(current["tts_contract"], schema)


def test_api_management_contains_separate_voice_clone_admin_channel():
    snapshot = console._api_management_snapshot()
    admin_group = next(group for group in snapshot["groups"] if group["id"] == "voice-clone-admin")
    channel = admin_group["channels"][0]
    assert admin_group["name"] == "声音复刻 API"
    assert channel["endpoint"] == "https://openspeech.bytedance.com/api/v3/tts/voice_clone"
    assert channel["secret_fields"] == []
    assert channel["read_only_first"] is False
    assert channel["capabilities"]["requires_app_id"] is False
    assert channel["capabilities"]["shared_credential_channel"] == "tts"
    assert channel["fixed_endpoints"] == [
        {
            "id": "voice_clone",
            "label": "创建 / 训练音色",
            "url": "https://openspeech.bytedance.com/api/v3/tts/voice_clone",
            "mode": "上传声音文件（外部写入）",
        },
        {
            "id": "get_voice",
            "label": "查询训练状态",
            "url": "https://openspeech.bytedance.com/api/v3/tts/get_voice",
            "mode": "只读查询",
        },
    ]


def test_voice_clone_endpoint_is_fixed_even_when_legacy_saved_endpoint_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)
    monkeypatch.setattr(console, "_api_management_load_secrets", lambda: {"tts": {"primary_api_key": "tts-key"}})
    monkeypatch.setattr(console, "_api_management_secret_health_snapshot", lambda: {"state": "loaded"})
    config = {
        "version": 1,
        "groups": {"voice-clone-admin": {"endpoint": "https://open.volcengineapi.com/"}},
        "api_groups": {}, "api_functions": {}, "api_channels": {}, "removed_channels": {},
    }
    tmp_path.joinpath("api_management.json").write_text(json.dumps(config), encoding="utf-8")

    admin = next(
        group for group in console._api_management_snapshot()["groups"]
        if group["id"] == "voice-clone-admin"
    )["channels"][0]

    assert admin["endpoint"] == "https://openspeech.bytedance.com/api/v3/tts/voice_clone"
    assert admin["endpoint_source"] == "固定新版 v3 协议"
    assert next(item for item in console.API_MANAGEMENT_SPECS if item["id"] == "voice-clone-admin")["url_envs"] == ()
    assert "voice-clone-admin" not in console.API_MANAGEMENT_GROUP_RUNTIME


def test_voice_clone_admin_status_label_explains_credential_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)
    snapshot = console._api_management_snapshot()
    admin_group = next(group for group in snapshot["groups"] if group["id"] == "voice-clone-admin")
    admin = admin_group["channels"][0]

    assert admin["status_label"] in {"待配置 TTS 主 API Key", "复用 TTS 主 API Key"}
    assert admin["secret_fields"] == []


def test_voice_clone_admin_reuses_tts_api_key_without_legacy_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)
    monkeypatch.setattr(console, "_api_management_load_secrets", lambda: {"tts": {"primary_api_key": "tts-key"}})
    monkeypatch.setattr(console, "_api_management_secret_health_snapshot", lambda: {"state": "loaded"})
    monkeypatch.delenv("VOICE_CLONE_ADMIN_APP_ID", raising=False)

    snapshot = console._api_management_snapshot()
    admin = next(group for group in snapshot["groups"] if group["id"] == "voice-clone-admin")["channels"][0]

    assert admin["status"] == "ready"
    assert admin["status_label"] == "复用 TTS 主 API Key"
    assert admin["capabilities"]["upload"] is True
    assert admin["capabilities"]["query"] is True
    assert "resource_management" not in admin["capabilities"]
    assert "resource_management" not in admin["config"]
    assert admin["source"] == "复用 TTS 主 API Key"

    audits = [
        item for item in snapshot["runtime_audit"]["nodes"]
        if item["effective_channel_id"] == "voice-clone-admin"
    ]
    assert audits
    assert all(item["status"] == "wired" for item in audits)
    assert all("页面本机加密凭据" in item["auth_source"] for item in audits)


def test_voice_clone_page_transport_uses_only_tts_primary_page_key(monkeypatch):
    captured = {}

    class Transport:
        @classmethod
        def from_api_key(cls, api_key):
            captured["api_key"] = api_key
            return cls()

    monkeypatch.setattr(console, "VoiceCloneTransport", Transport)
    monkeypatch.setattr(console, "_api_management_load_secrets", lambda: {
        "tts": {"primary_api_key": "page-primary"},
        "voice-clone-admin-resource": {
            "access_key": "legacy-ak",
            "secret_key": "legacy-sk",
        },
    })

    console._voice_clone_transport_from_page()

    assert captured == {"api_key": "page-primary"}


def test_api_management_keeps_tikhub_tts_and_sound_effect_separate(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    snapshot = console._api_management_snapshot()
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    tts = next(group for group in snapshot["groups"] if group["id"] == "tts")
    sound_effect = next(group for group in snapshot["groups"] if group["id"] == "sound-effect")
    tikhub = next(group for group in snapshot["groups"] if group["id"] == "tikhub")

    assert {channel["id"] for channel in tts["channels"] if not channel.get("is_custom_channel")} == {"tts"}
    assert {channel["id"] for channel in sound_effect["channels"] if not channel.get("is_custom_channel")} == {"sound-effect"}
    assert len(snapshot["groups"]) == 9
    assert tikhub["channel_ids"] == ["tikhub"]
    assert all(channel["id"] != "tikhub" for channel in tts["channels"])
    assert all(channel["id"] != "sound-effect" for channel in tts["channels"])
    assert tikhub["channels"][0]["secret_fields"] == ["primary_api_key"]
    assert "不属于 TTS API" in tikhub["note"]
    assert "官方 TTS 合成" in tts["note"]
    assert "Seed-Audio" not in tts["note"]
    assert "VOICE_CLONE_UPLOAD_API_KEY" not in snapshot_json
    assert "VOICE_CLONE_ADMIN_ACCESS_TOKEN" not in snapshot_json
    assert "clone-upload" not in snapshot_json
    assert "clone-status" not in snapshot_json

    page = console._api_management_page().decode("utf-8")
    assert "tikub" not in page.lower()
    assert "isAuthOnlyChannel(channel)?'TikHub 鉴权'" not in page
    assert "TikHub 独立鉴权 → Bearer Token" in page
    assert "VOICE_CLONE_UPLOAD_API_KEY" not in page
    assert "VOICE_CLONE_ADMIN_ACCESS_TOKEN" not in page
    assert "clone-upload" not in page
    assert "clone-status" not in page


def test_tts_group_treats_voice_clone_admin_as_optional_management(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)
    for env_name in (
        "ARK_TTS_API_KEY", "ARK_AUDIO_API_KEY", "VOLCENGINE_AUDIO_API_KEY",
        "ARK_TTS_BACKUP_API_KEY", "ARK_AUDIO_BACKUP_API_KEY",
        "VOLCENGINE_AUDIO_BACKUP_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)

    snapshot = console._api_management_snapshot()
    tts = next(group for group in snapshot["groups"] if group["id"] == "tts")
    admin_group = next(group for group in snapshot["groups"] if group["id"] == "voice-clone-admin")
    admin = admin_group["channels"][0]

    assert tts["status"] in {"ready", "document"}
    assert tts["api_failover"]["order"] == ["tts"]
    assert all("voice-status" not in str(item.get("id")) for item in tts["functions"])
    assert admin["management_only"] is True


def test_voice_clone_admin_auth_verify_reports_shared_tts_key(monkeypatch):
    monkeypatch.setattr(console, "_api_management_load_secrets", lambda: {"tts": {"primary_api_key": "tts-key"}})
    result = console._api_management_verify_voice_clone_admin_channel({
        "id": "voice-clone-admin", "name": "声音复刻能力",
    })

    assert result[0]["status"] == "not_checked"
    assert result[0]["auth_verified"] is False
    assert result[0]["credential_source"] == "shared_tts_primary_api_key"


def test_voice_clone_admin_auth_verify_reports_missing_tts_key(monkeypatch):
    monkeypatch.setattr(console, "_api_management_load_secrets", lambda: {})
    result = console._api_management_verify_voice_clone_admin_channel({
        "id": "voice-clone-admin",
        "name": "声音复刻能力",
    })

    assert result[0]["status"] == "not_checked"
    assert result[0]["auth_verified"] is False
    assert result[0]["credential_source"] == "tts_primary_api_key_missing"
    assert "未配置 TTS 主 API Key" in result[0]["message"]


def test_voice_clone_admin_backup_only_does_not_enable_training(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)
    monkeypatch.setattr(console, "_api_management_load_secrets", lambda: {
        "tts": {"backup_api_key": "backup-only"},
    })
    monkeypatch.setattr(console, "_api_management_secret_health_snapshot", lambda: {"state": "loaded"})
    for env_name in ("ARK_TTS_API_KEY", "ARK_AUDIO_API_KEY", "VOLCENGINE_AUDIO_API_KEY"):
        monkeypatch.delenv(env_name, raising=False)

    snapshot = console._api_management_snapshot()
    admin = next(group for group in snapshot["groups"] if group["id"] == "voice-clone-admin")["channels"][0]
    assert admin["status"] == "missing"
    assert admin["status_label"] == "待配置 TTS 主 API Key"
    assert admin["capabilities"]["upload"] is False
    assert admin["capabilities"]["query"] is False

    result = console._api_management_verify_voice_clone_admin_channel({
        "id": "voice-clone-admin", "name": "声音复刻能力",
    })
    assert result[0]["credential_source"] == "tts_primary_api_key_missing"
    assert "未配置 TTS 主 API Key" in result[0]["message"]


def test_voice_clone_never_treats_inherited_tts_environment_as_page_configuration(monkeypatch, tmp_path):
    """声音复刻不能因服务继承的环境变量而绕过 API 管理页。"""

    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)
    monkeypatch.setattr(console, "_api_management_load_secrets", lambda: {})
    monkeypatch.setattr(console, "_api_management_secret_health_snapshot", lambda: {"state": "missing"})
    monkeypatch.setenv("ARK_TTS_API_KEY", "inherited-primary")

    admin = next(
        group for group in console._api_management_snapshot()["groups"]
        if group["id"] == "voice-clone-admin"
    )["channels"][0]

    assert admin["status"] == "missing"
    assert admin["status_label"] == "待配置 TTS 主 API Key"
    assert admin["capabilities"]["upload"] is False
    result = console._api_management_verify_voice_clone_admin_channel(admin)
    assert result[0]["credential_source"] == "tts_primary_api_key_missing"


def test_voice_clone_page_transport_refuses_environment_fallback_in_production(monkeypatch):
    monkeypatch.setattr(console, "_API_MANAGEMENT_RUNTIME_ENFORCE_PAGE_CREDENTIALS", True)
    monkeypatch.setattr(console, "_api_management_load_secrets", lambda: {})
    monkeypatch.setenv("ARK_TTS_API_KEY", "inherited-key")

    with pytest.raises(console.VoiceCloneError, match="API 管理中心"):
        console._voice_clone_transport_from_page()


def test_voice_clone_config_rejects_legacy_settings_and_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)
    monkeypatch.setattr(console, "_api_management_load_secrets", lambda: {})
    monkeypatch.setattr(console, "_api_management_secret_health_snapshot", lambda: {"state": "loaded"})

    payload = {
        "group_id": "voice-clone-admin",
        "channel_id": "voice-clone-admin",
        "settings": {
            "endpoint": "https://openspeech.bytedance.com/api/v3/tts/voice_clone",
            "region": "cn-beijing",
        },
        "secrets": {"access_key": "legacy-ak"},
        "group_settings": {},
    }

    with pytest.raises(ValueError, match="只接受固定 v3 地址和超时"):
        console._api_management_save_config(payload)


def test_tts_page_explains_custom_resource_uses_same_tts_api():
    page = console._api_management_page().decode("utf-8")
    assert "seed-icl-2.0" in page
    assert "共用同一 TTS endpoint 和 API Key" in page
    assert "无需第二套 TTS API" in page
    assert "声音复刻训练和 get_voice 查询固定使用主 Key" in page
    assert "试听只读取训练返回的 demo_audio" in page
    assert "speaker_id" in page
    assert "主/备用 TTS API Key 可以属于不同火山账号" in page
    assert "定制音色" in page
    # 鉴权中心只配置 Key；直达链接仍进入同一个编导创建弹窗，不新增第二套入口。
    assert 'href="/director?open_voice_clone=1"' in page
    assert "打开编导音色创建弹窗" in page


def test_tts_snapshot_and_persisted_config_use_fixed_v3_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)
    monkeypatch.setattr(console, "_api_management_load_secrets", lambda: {"tts": {"primary_api_key": "tts-key"}})
    monkeypatch.setattr(console, "_api_management_secret_health_snapshot", lambda: {"state": "loaded"})
    monkeypatch.setenv("ARK_TTS_API_URL", "https://open.volcengineapi.com/")
    monkeypatch.setenv("ARK_TTS_MODEL", "legacy-model")
    (tmp_path / "api_management.json").write_text(json.dumps({
        "version": 1,
        "groups": {"tts": {
            "endpoint": "https://open.volcengineapi.com/",
            "primary_model": "legacy-model",
            "model_slots": {"ARK_TTS_MODEL": "legacy-model"},
            "timeout_seconds": 45,
        }},
        "api_groups": {}, "api_functions": {}, "api_channels": {}, "removed_channels": {},
    }), encoding="utf-8")

    snapshot = console._api_management_snapshot()
    tts = next(group for group in snapshot["groups"] if group["id"] == "tts")["channels"][0]

    assert tts["endpoint"] == "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    assert tts["endpoint_source"] == "固定新版 v3 协议"
    assert tts["primary_model"] == "seed-tts-2.0-standard"
    assert tts["primary_model_source"] == "固定新版 v3 协议"
    assert tts["model_slots"] == []
    saved = json.loads((tmp_path / "api_management.json").read_text(encoding="utf-8"))
    assert set(saved["groups"]["tts"]) == {"timeout_seconds"}
    assert "ARK_TTS_API_URL" not in str(console.os.environ.get("ARK_TTS_API_URL", ""))


def test_tts_save_rejects_legacy_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)

    with pytest.raises(ValueError, match="TTS endpoint 固定"):
        console._api_management_save_config({
            "group_id": "tts",
            "channel_id": "tts",
            "settings": {"endpoint": "https://open.volcengineapi.com/"},
            "secrets": {},
            "group_settings": {},
        })


def test_tts_api_management_endpoint_is_read_only():
    page = console._api_management_page().decode("utf-8")
    assert "新版 V3 API 地址（固定）" in page
    assert 'data-api-endpoint="true"' in page
    assert 'readonly aria-readonly="true"' in page


def test_assets_page_exposes_voice_management_without_changing_selection_timing():
    page = console._assets_page().decode("utf-8")
    assert "本任务音色" in page
    assert "只读锁定" in page
    assert "音色管理与试听已移到编导层" in page
    assert 'id="openAssetVoiceManager"' not in page
    assert 'id="assetVoiceManagerModal"' not in page
    assert 'id="assetVoiceManagerList"' not in page
    assert "素材页不提供训练入口" in page
    assert "/director" in page
    assert "/api/director/story-tasks/" in page
    assert "/voice" in page
    assert "避免在 TTS 生成后误改时间轴依据" in page
    assert "/api/voices/clone" not in page
    assert "/voice-change" not in page


def test_director_page_places_voice_popup_before_tts_timeline():
    page = console._director_page().decode("utf-8")
    # 音色在编导层选择，锁定后才进入 TTS/STT 时间轴；素材页不再提供入口。
    assert 'id="openVoiceManager"' in page
    assert 'id="voiceManagerModal"' in page
    assert 'id="voiceManagerList"' in page
    assert 'id="voiceKey"' in page
    assert 'id="changeLockedVoice"' in page
    assert "rebuild-with-new-voice" in page
    assert "/voice-change" in page
    assert "TTS 前选择" in page
    assert "剪映 STT" in page
    assert 'id="voiceClonePanel"' in page
    # 生产页只输出一个新版 V3 预付费/免费额度表单；首次创建允许留空
    # speaker_id，由接口返回真实 ID，旧版多模式表单不得重新进入运行时页面。
    assert 'id="v3VoiceCloneAudio"' in page
    assert 'id="v3VoiceCloneSpeakerId"' in page
    assert 'id="v3VoiceCloneConsent"' in page
    assert 'id="submitV3VoiceClone"' in page
    assert 'id="voiceCloneMode"' not in page
    assert 'id="voiceCloneSpeakerId"' not in page
    assert 'id="voiceCloneCustomSpeakerId"' not in page
    assert "mode:'prepaid_create'" in page
    assert "postpaid_create" not in page
    assert "上传并创建预付费音色" in page
    assert "V3 预付费/免费额度" in page
    assert "首次创建：留空，由新版 V3 返回 speaker_id" in page
    assert "音频从本页直接上传到本地后台" in page
    assert "不会打开官网" in page
    assert "留空" in page
    assert "使用 12 个预付费槽位创建音色" not in page
    assert 'id="openOfficialPrepaidClone"' not in page
    assert "cloneMode.value='postpaid_create'" not in page
    assert "mode.value='postpaid_create';refresh()" not in page
    assert "navigator.clipboard.readText" not in page
    assert "https://console.volcengine.com/speech/new/experience/clone?projectName=default" not in page
    assert "/api/voices/clone" in page
    assert "voice_clone stage=upload" not in page  # 服务端日志不应泄露到 HTML
    assert "<select id=\"voiceKey\"" not in page
    # 提交处理器外层的请求 speaker_id 与响应 speaker_id 必须使用不同变量名；
    # 否则 let/const 暂时性死区会在点击时直接抛出初始化错误，接口不会发起。
    assert "const returnedSpeakerId=String(data.speaker_id||'').trim();" in page
    assert "const speakerId=String(data.speaker_id||'').trim();" not in page


def test_legacy_voice_register_route_is_gone_and_points_to_v3_clone(monkeypatch):
    """旧登记入口只能返回 410，不能绕过新版上传/状态绑定流程。"""

    monkeypatch.setattr(console, "DEV_READ_ONLY", False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/voices/register",
            data=b'{"display_name":"legacy","speaker_id":"S_LEGACY"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as raised:
            urlopen(request)
        assert raised.value.code == 410
        body = json.loads(raised.value.read().decode("utf-8"))
        assert body["status"] == "legacy_route_disabled"
        assert body["replacement"] == "/api/voices/clone"
    finally:
        server.shutdown()
        server.server_close()


def test_api_management_page_exposes_new_voice_clone_http_contract_without_legacy_fields():
    page = console._api_management_page().decode("utf-8")
    assert "/api/v3/tts/voice_clone" in page
    assert "/api/v3/tts/get_voice" in page
    assert "新版 V3 固定接入点（只读）" in page
    assert "fixed_endpoints" in page
    assert "data-api-fixed-voice-endpoint" in page
    assert "个固定 V3 接入点（展开查看）" in page
    assert "复用 TTS 主 API Key" in page
    assert "本流程不需要额外鉴权参数" in page
    assert "apiVoiceCloneAppId_" not in page
    assert "speech_saas_prod" not in page
    assert "BatchListMegaTTSTrainStatus" not in page
    assert "ListSpeakers" not in page
    assert "OpenAPI 管理参数" not in page
    assert "声音复刻与普通 TTS 共用主" in page
    assert "副 Key 只用于普通 TTS 合成跌落，不参与复刻" in page
    assert "resource-status" not in page  # 查询只在编导弹窗触发，鉴权页不自动请求
    assert "resource_management" not in page
    assert "ProjectName" not in page
    assert "api-voice-resource" not in page
    assert "data-api-voice-resource" not in page
    assert "data-api-resource-secret" not in page
    assert "paid-voice-resource-order" not in page


def test_director_voice_popup_renders_custom_preview_without_paid_call(monkeypatch):
    monkeypatch.setattr(console, "_voice_catalog_snapshot", lambda: {
        "version": "test",
        "defaults": {"male": "custom_demo"},
        "voices": [{
            "voice_key": "custom_demo", "speaker_id": "S_CUSTOM", "display_name": "定制示例",
            "voice_type": "custom", "gender": "female", "resource_id": "seed-icl-2.0",
            "model": "seed-tts-2.0-standard", "training_status": "Active",
            "authorization_status": "active", "enabled": True,
            "preview_url": "https://example.test/demo.mp3",
        }],
    })
    page = console._director_page().decode("utf-8")
    assert "定制示例" in page
    assert "seed-icl-2.0" in page
    assert 'audio controls' in page
    assert "https://example.test/demo.mp3" in page
    assert "不会自动发起付费试听" in page
    assert "训练接口返回的 demo_audio" in page
    assert "正在使用 TTS 主 API Key 生成试听" not in page
    assert "调用正式 TTS" in page
    assert "目录版本：test" in page


def test_director_voice_popup_separates_official_and_custom_voice_actions(monkeypatch):
    monkeypatch.setattr(console, "_voice_catalog_snapshot", lambda: {
        "version": "test",
        "defaults": {"male": "official_demo"},
        "voices": [
            {
                "voice_key": "official_demo", "speaker_id": "zh_male_demo", "display_name": "官方示例",
                "voice_type": "official", "gender": "male", "resource_id": "seed-tts-2.0",
                "model": "seed-tts-2.0-standard", "training_status": "ready",
                "authorization_status": "verified", "enabled": True, "verified": False,
                "preview_url": "https://example.test/official.mp3",
            },
            {
                "voice_key": "custom_demo", "speaker_id": "custom_speaker_id", "display_name": "我的定制示例",
                "voice_type": "custom", "gender": "female", "resource_id": "seed-icl-2.0",
                "model": "seed-tts-2.0-standard", "training_status": "ready",
                "authorization_status": "verified", "enabled": True, "verified": False,
                "preview_url": "https://example.test/custom.mp3",
            },
        ],
    })

    page = console._director_page().decode("utf-8")
    official_card = page.split('data-voice-key="official_demo"', 1)[1].split("</article>", 1)[0]
    custom_card = page.split('data-voice-key="custom_demo"', 1)[1].split("</article>", 1)[0]

    assert "选择此官方音色" in official_card or "继续使用" in official_card
    assert "试听训练结果" not in official_card
    assert "真实闭环验收" not in official_card
    assert "目录契约检查" not in official_card
    assert "停用" not in official_card
    assert "真实验收后可选择" not in official_card
    assert "试听训练结果" in custom_card
    assert "真实闭环验收" in custom_card
    assert "目录契约检查" in custom_card
    assert "停用" in custom_card


def test_voice_profile_verify_is_readonly_and_requires_explicit_confirmation(monkeypatch):
    monkeypatch.setattr(console, "_voice_catalog_snapshot", lambda: {
        "voices": [{
            "voice_key": "custom", "speaker_id": "S_CUSTOM", "display_name": "定制",
            "voice_type": "custom", "resource_id": "seed-icl-2.0", "model": "seed-tts-2.0-standard",
            "enabled": True, "verified": False,
        }],
    })
    result = console._verify_voice_profile("custom", {"confirm": "readonly-voice-verify"})
    assert result["status"] == "passed"
    assert result["evidence_level"] == "offline_catalog_contract"
    assert result["verified"] is False


def test_disabled_voice_can_pass_offline_contract_check_before_local_enable(monkeypatch):
    monkeypatch.setattr(console, "_voice_catalog_snapshot", lambda: {
        "voices": [{
            "voice_key": "listed", "speaker_id": "S_LISTED", "display_name": "官方待启用",
            "voice_type": "official", "resource_id": "seed-tts-2.0", "model": "seed-tts-2.0-standard",
            "enabled": False, "verified": False,
        }],
    })
    result = console._verify_voice_profile("listed", {"confirm": "readonly-voice-verify"})
    assert result["status"] == "passed"
    assert result["checks"]["enabled"] is False
    assert result["ready_to_enable"] is True


def test_readonly_voice_status_sync_updates_local_catalog_only(monkeypatch):
    catalog = {"version": "v", "voices": [{
        "voice_key": "custom", "speaker_id": "S_CUSTOM", "voice_type": "custom",
        "resource_id": "seed-icl-2.0", "model": "seed-tts-2.0-standard",
        "training_status": "Training", "authorization_status": "pending",
    }]}

    class Store:
        def load(self):
            return catalog

        def save(self, value):
            return value

    monkeypatch.setattr(console, "_voice_catalog_store", lambda: Store())
    result = console._sync_voice_catalog_statuses({"response": {
        "Result": {"Statuses": [{"SpeakerID": "S_CUSTOM", "State": "Active", "DemoAudio": "https://example.test/demo.mp3"}]}
    }})
    assert result == {"updated_voice_keys": ["custom"], "status_count": 1, "catalog_written": True}
    assert catalog["voices"][0]["training_status"] == "Active"
    assert catalog["voices"][0]["authorization_status"] == "active"
    assert catalog["voices"][0]["preview_url"].startswith("https://")


def test_voice_replacement_commits_old_task_only_after_new_task_succeeds(tmp_path, monkeypatch):
    tasks = {
        "old": {
            "task_id": "old", "task_type": "director_story_review", "state": "succeeded",
            "result": {"tts_fingerprint": "old-fp", "invalidation": {"status": "replacement_pending", "scope": ["tts", "timeline"]}},
        },
        "new": {
            "task_id": "new", "task_type": "director_story_review", "state": "succeeded",
            "replacement_of_task_id": "old", "result": {"tts_fingerprint": "new-fp"},
        },
        "asset": {
            "task_id": "asset", "task_type": "asset_production", "state": "succeeded",
            "source_story_task_id": "old", "result": {"source_story_task_id": "old"},
        },
    }
    monkeypatch.setattr(console, "STYLE_TASKS", tasks)
    monkeypatch.setattr(console, "_persist_task_required", lambda task: None)
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path)

    console._commit_voice_replacement("new")

    assert tasks["old"]["lifecycle_status"] == "superseded"
    assert tasks["old"]["superseded_by"] == "new"
    assert tasks["old"]["result"]["invalidation"]["status"] == "committed"
    assert tasks["new"]["result"]["voice_change"]["status"] == "committed"
    assert tasks["asset"]["result"]["invalidation"]["status"] == "stale"
    assert tasks["asset"]["superseded_by"] == "new"
    assert tasks["asset"]["lifecycle_status"] == "superseded"
    events = (tmp_path / "old" / "events.jsonl").read_text(encoding="utf-8")
    assert '"status": "committed"' in events
    assert '"old_fingerprint": "old-fp"' in events
    assert '"new_fingerprint": "new-fp"' in events


def test_voice_replacement_waits_for_complete_asset_production(tmp_path, monkeypatch):
    tasks = {
        "old": {
            "task_id": "old", "task_type": "director_story_review", "state": "succeeded",
            "result": {"tts_fingerprint": "old-fp", "invalidation": {"status": "replacement_pending", "scope": ["tts", "timeline"]}},
        },
        "new": {
            "task_id": "new", "task_type": "director_story_review", "state": "succeeded",
            "replacement_of_task_id": "old", "result": {"tts_fingerprint": "new-fp", "voice_change": {"status": "pending_downstream_assets"}},
        },
        "asset": {
            "task_id": "asset", "task_type": "asset_production", "state": "succeeded",
            "source_story_task_id": "new", "mode": "full",
            "result": {"source_story_task_id": "new", "categories": {
                "first_frame_images": {"state": "succeeded"},
                "aigc_videos": {"state": "skipped"},
                "bgm": {"state": "succeeded"},
                "digital_humans": {"state": "skipped"},
                "explanation_videos": {"state": "succeeded"},
            }},
        },
    }
    monkeypatch.setattr(console, "STYLE_TASKS", tasks)
    monkeypatch.setattr(console, "_persist_task_required", lambda task: None)
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path)

    assert console._commit_voice_replacement_after_asset_production("asset") is True
    assert tasks["old"]["lifecycle_status"] == "superseded"
    assert tasks["new"]["result"]["voice_change"]["status"] == "committed"
    assert tasks["asset"]["result"]["voice_change_commit"]["status"] == "committed"


def test_voice_replacement_does_not_commit_partial_asset_production(tmp_path, monkeypatch):
    tasks = {
        "old": {
            "task_id": "old", "task_type": "director_story_review", "state": "succeeded",
            "result": {"tts_fingerprint": "old-fp", "invalidation": {"status": "replacement_pending", "scope": ["tts", "timeline"]}},
        },
        "new": {
            "task_id": "new", "task_type": "director_story_review", "state": "succeeded",
            "replacement_of_task_id": "old", "result": {"tts_fingerprint": "new-fp"},
        },
        "asset": {
            "task_id": "asset", "task_type": "asset_production", "state": "failed",
            "source_story_task_id": "new", "result": {"categories": {"digital_humans": {"state": "failed"}}},
        },
    }
    monkeypatch.setattr(console, "STYLE_TASKS", tasks)
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path)

    assert console._commit_voice_replacement_after_asset_production("asset") is False
    assert "lifecycle_status" not in tasks["old"]
    assert tasks["new"]["result"].get("voice_change", {}).get("status") != "committed"


def test_voice_change_requires_explicit_rebuild_confirmation():
    with pytest.raises(ValueError, match="confirm=rebuild-with-new-voice"):
        console._change_story_task_voice("missing-task", {})


def test_voice_change_queues_replacement_and_marks_dependents_pending(tmp_path, monkeypatch):
    tasks = {
        "old": {
            "task_id": "old", "task_type": "director_story_review", "state": "succeeded",
            "source_rewrite_id": "rewrite-1", "voice_resolution": {"voice_key": "qingcang_2"},
            "result": {"source_rewrite_id": "rewrite-1", "active_version": "fp-old"},
        },
        "asset-old": {
            "task_id": "asset-old", "task_type": "asset_production", "state": "succeeded",
            "source_story_task_id": "old", "result": {},
        },
    }

    def snapshot(task_id):
        return tasks.get(task_id)

    def set_task(task_id, **changes):
        target = tasks[task_id]
        result = changes.pop("result", None)
        remove_keys = changes.pop("_remove_result_keys", ())
        target.update(changes)
        if isinstance(result, dict):
            merged = dict(target.get("result") or {})
            merged.update(result)
            for key in remove_keys:
                merged.pop(key, None)
            target["result"] = merged

    def create(payload):
        replacement = {
            "task_id": "new", "task_type": "director_story_review", "state": "queued",
            "replacement_of_task_id": payload["replacement_of_task_id"],
            "voice_resolution": {"voice_key": payload["voice_key"]}, "result": {},
        }
        tasks["new"] = replacement
        return replacement

    monkeypatch.setattr(console, "STYLE_TASKS", tasks)
    monkeypatch.setattr(console, "_task_snapshot", snapshot)
    monkeypatch.setattr(console, "_list_style_tasks", lambda **_: list(tasks.values()))
    monkeypatch.setattr(console, "_set_task", set_task)
    monkeypatch.setattr(console, "_create_director_story_task", create)
    monkeypatch.setattr(console, "_resolve_story_voice", lambda _: {
        "voice_key": "custom_demo", "speaker_id": "S_CUSTOM", "resource_id": "seed-icl-2.0",
        "model": "seed-tts-2.0-standard", "voice_type": "custom",
    })
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path)

    result = console._change_story_task_voice("old", {
        "confirm": "rebuild-with-new-voice", "voice_key": "custom_demo",
    })

    assert result["status"] == "rebuild_queued"
    assert result["new_task"]["task_id"] == "new"
    assert tasks["asset-old"]["result"]["invalidation"]["status"] == "stale_pending_rebuild"
    assert tasks["old"]["result"]["active_version"] == "fp-old"
    assert tasks["old"]["result"]["invalidation"]["status"] == "replacement_pending"
    assert tasks["old"]["result"]["pending_version"] == "new"
    events = (tmp_path / "old" / "events.jsonl").read_text(encoding="utf-8")
    assert '"new_task_id": "new"' in events
    assert '"affected_task_ids": ["asset-old"]' in events


def test_voice_clone_preview_reads_training_demo_audio_without_formal_tts(monkeypatch, tmp_path):
    class Store:
        def update(self, voice_key, patch):
            assert voice_key == "custom_demo"
            assert patch["preview_url"].startswith("/api/voice-preview/")
            # 试听只缓存训练返回的 demo_audio；不能把试听误当成
            # 正式 TTS/STT/时间轴验收，也不能提前放开音色。
            assert "verified" not in patch
            assert "enabled" not in patch
            return {"version": "v2"}

    monkeypatch.setattr(console, "_resolve_story_voice", lambda _key: {
        "voice_key": "custom_demo", "speaker_id": "S_CUSTOM", "voice_type": "custom",
        "training_status": "Active", "authorization_status": "active",
        "resource_id": "seed-icl-2.0", "model": "seed-tts-2.0-standard",
        "preview_url": "https://example.test/demo.mp3",
    })
    monkeypatch.setattr(console, "_voice_catalog_store", lambda: Store())
    calls = []
    monkeypatch.setattr(
        console,
        "_voice_clone_download_demo_audio",
        lambda url, key: (calls.append((url, key)) or ("/api/voice-preview/demo.mp3", 11)),
    )
    monkeypatch.setattr(
        console,
        "_voice_clone_primary_tts_transport",
        lambda: (_ for _ in ()).throw(AssertionError("试听不得构造正式 TTS transport")),
    )
    monkeypatch.setattr(
        console,
        "_api_management_require_runtime_credentials",
        lambda *_: (_ for _ in ()).throw(AssertionError("试听不需要读取 TTS 凭据")),
    )

    with pytest.raises(ValueError, match="paid-voice-preview"):
        console._voice_clone_preview({"voice_key": "custom_demo", "text": "试听"})

    result = console._voice_clone_preview({
        "confirm": "paid-voice-preview", "voice_key": "custom_demo", "text": "试听",
    })
    assert result["preview_url"].startswith("/api/voice-preview/")
    assert result["preview_source"] == "voice_clone_demo_audio"
    assert result["formal_tts_called"] is False
    assert result["bytes"] == 11
    assert calls == [("https://example.test/demo.mp3", "custom_demo")]


def test_voice_clone_download_demo_audio_publishes_local_file(monkeypatch, tmp_path):
    class Response:
        headers = {"Content-Length": "12"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"demo-audio-1"

    calls = []
    monkeypatch.setattr(console, "urlopen", lambda request, timeout: (calls.append((request.full_url, timeout)) or Response()))
    monkeypatch.setattr(console, "VOICE_PREVIEW_DIR", tmp_path)

    preview_url, size = console._voice_clone_download_demo_audio(
        "https://example.test/demo.mp3", "custom_demo"
    )

    assert preview_url.startswith("/api/voice-preview/demo-")
    assert preview_url.endswith(".mp3")
    assert size == 12
    token = preview_url.rsplit("/", 1)[-1]
    assert (tmp_path / token).read_bytes() == b"demo-audio-1"
    assert calls == [("https://example.test/demo.mp3", 30)]


def test_voice_clone_preview_transport_is_primary_only(monkeypatch):
    config = console.ArkTTSConfig(
        api_key="account-a-key",
        backup_api_key="account-b-key",
        extra_api_keys=("account-c-key",),
    )
    captured = {}

    class Transport:
        def __init__(self, value):
            captured["config"] = value

    monkeypatch.setattr(
        console.ArkTTSConfig,
        "from_env",
        classmethod(lambda cls, **_kwargs: config),
    )
    monkeypatch.setattr(console, "ArkTTSHTTPTransport", Transport)

    console._voice_clone_primary_tts_transport()

    actual = captured["config"]
    assert actual.api_key == "account-a-key"
    assert actual.backup_api_key == ""
    assert actual.extra_api_keys == ()
    assert actual.failover_strategy == "primary_only"


def test_voice_clone_acceptance_requires_both_write_confirmations():
    with pytest.raises(ValueError, match="allow_external_write=true"):
        console._voice_clone_acceptance({
            "confirm": "voice-acceptance",
            "voice_key": "custom_demo",
            "text": "验收",
            "write_local_evidence": True,
        })
    with pytest.raises(ValueError, match="write_local_evidence=true"):
        console._voice_clone_acceptance({
            "confirm": "voice-acceptance",
            "voice_key": "custom_demo",
            "text": "验收",
            "allow_external_write": True,
        })


def test_voice_clone_acceptance_runs_v3_tts_stt_timeline_and_writes_evidence(monkeypatch, tmp_path):
    catalog = {
        "version": "test",
        "voices": [{
            "voice_key": "custom_demo",
            "display_name": "真实验收旁白",
            "voice_type": "custom",
            "speaker_id": "custom_speaker_id",
            "custom_speaker_id": "custom_zh_demo01",
            "resource_id": "seed-icl-2.0",
            "model": "seed-tts-2.0-standard",
            "training_status": "Training",
            "authorization_status": "pending",
            "enabled": False,
            "verified": False,
            "preview_url": "https://example.test/demo.mp3",
        }],
    }
    updates = []
    tts_calls = []
    audio_path = tmp_path / "tts" / "voice.mp3"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"real-audio")

    class Store:
        def load(self):
            return catalog

        def update(self, voice_key, patch):
            updates.append((voice_key, dict(patch)))
            next(item for item in catalog["voices"] if item["voice_key"] == voice_key).update(patch)
            return catalog

    monkeypatch.setattr(console, "VOICE_ACCEPTANCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(console, "_voice_catalog_snapshot", lambda: catalog)
    monkeypatch.setattr(console, "_voice_catalog_store", lambda: Store())
    monkeypatch.setattr(console, "_runtime_auth_document", lambda: tmp_path / "page-credentials-only.md")
    status_calls = []

    def fake_voice_status(payload):
        status_calls.append(dict(payload))
        training_status = "Training" if len(status_calls) == 1 else "Active"
        return {
            "status": "ready",
            "results": [{
                "action": "get_voice",
                "speaker_id": "custom_speaker_id",
                "custom_speaker_id": "custom_zh_demo01",
                "training_status": training_status,
                "demo_audio": "https://example.test/demo.mp3" if training_status == "Active" else "",
            }],
        }

    monkeypatch.setattr(console, "_voice_clone_status_readonly", fake_voice_status)
    monkeypatch.setattr(console.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        console,
        "_voice_clone_download_demo_audio",
        lambda url, key: ("/api/voice-preview/custom-demo.mp3", 1234),
    )

    def fake_generate_tts(manifest, auth_document, output_dir, *, voice_override=None):
        tts_calls.append({
            "manifest": manifest,
            "auth_document": auth_document,
            "output_dir": output_dir,
            "voice_override": dict(voice_override or {}),
        })
        return {
            "tts_contract": {
                "schema_version": "tts-voice-binding-v1",
                "voice": {
                    "voice_key": "custom_demo",
                    "speaker_id": "custom_zh_demo01",
                    "provider_speaker_id": "custom_speaker_id",
                    "custom_speaker_id": "custom_zh_demo01",
                    "resource_id": "seed-icl-2.0",
                    "model": "seed-tts-2.0-standard",
                    "voice_type": "custom",
                },
                "audio": {
                    "paths": [str(audio_path)],
                    "sha256": ["hash"],
                    "actual_durations_s": [1.2],
                },
                "auth_slots": ["primary"],
                "voice_bindings": [],
                "timing_authority": "capcut_stt_required",
            }
        }

    monkeypatch.setattr(console, "generate_tts", fake_generate_tts)
    monkeypatch.setattr(console, "_probe_written_audio_duration", lambda path: 1.2)
    monkeypatch.setattr(console, "run_live_subtitle_pipeline", lambda payload: {
        "caption_count": 1,
        "captions": [{
            "caption_id": "c1",
            "group_id": "g01",
            "start_us": 0,
            "end_us": 1_100_000,
            "text": "这是一次真实验收",
        }],
        "group_timelines": [{"start": 0, "end": 1_100_000}],
        "total_timeline": {"start": 0, "end": 1_100_000},
    })

    result = console._voice_clone_acceptance({
        "confirm": "voice-acceptance",
        "voice_key": "custom_demo",
        "text": "这是一次真实验收",
        "allow_external_write": True,
        "write_local_evidence": True,
    })

    assert result["status"] == "succeeded"
    assert result["speaker_id"] == "custom_zh_demo01"
    assert result["actual_duration_seconds"] == 1.2
    assert result["timeline_validation"]["timeline_within_audio"] is True
    assert len(status_calls) == 2
    assert tts_calls and tts_calls[0]["voice_override"]["speaker_id"] == "custom_speaker_id"
    assert tts_calls[0]["voice_override"]["custom_speaker_id"] == "custom_zh_demo01"
    assert updates and updates[-1][1]["verified"] is True and updates[-1][1]["enabled"] is True
    evidence_dir = Path(result["evidence_dir"])
    assert evidence_dir.is_dir()
    for name in (
        "request_summary.json", "voice_clone_response_summary.json",
        "tts_response_summary.json", "stt_response_summary.json",
        "timeline_validation.json", "acceptance_result.json", "events.jsonl",
    ):
        assert (evidence_dir / name).is_file(), name
    event_text = (evidence_dir / "events.jsonl").read_text(encoding="utf-8")
    assert all(stage in event_text for stage in ("voice_clone_status", "preview", "tts", "stt", "timeline", "catalog"))


def test_voice_clone_acceptance_http_route_dispatches(monkeypatch):
    monkeypatch.setattr(console, "DEV_READ_ONLY", False)
    monkeypatch.setattr(console, "_voice_clone_acceptance", lambda payload: {
        "status": "succeeded",
        "voice_key": payload["voice_key"],
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/voices/acceptance",
            data=json.dumps({"voice_key": "custom_demo"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert payload == {"status": "succeeded", "voice_key": "custom_demo"}
    finally:
        server.shutdown()
        server.server_close()


def test_voice_clone_status_route_uses_new_get_voice_and_syncs_catalog(monkeypatch):
    catalog = {"version": "v1", "voices": [{
        "voice_key": "custom_demo", "speaker_id": "S_CUSTOM", "display_name": "我的旁白",
        "voice_type": "custom", "resource_id": "seed-icl-2.0", "model": "seed-tts-2.0-standard",
        "training_status": "Training", "authorization_status": "pending",
    }]}
    writes = []
    queried_speakers = []

    class Store:
        def load(self):
            return catalog

        def save(self, value):
            writes.append(value)
            return value

    class Transport:
        @classmethod
        def from_env(cls):
            return cls()

        def query(self, speaker_id):
            queried_speakers.append(speaker_id)
            return {"action": "get_voice", "speaker_id": speaker_id, "status": 4, "training_status": "Active", "demo_audio": "https://example.test/demo.mp3"}

    monkeypatch.setattr(console, "_voice_catalog_store", lambda: Store())
    monkeypatch.setattr(console, "VoiceCloneTransport", Transport)
    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda *_: None)

    result = console._voice_clone_status_readonly({
        "confirm": "readonly-voice-status", "speaker_ids": ["S_CUSTOM"],
    })
    assert result["evidence_level"] == "live_readonly_voice_clone"
    assert result["catalog_sync"]["updated_voice_keys"] == ["custom_demo"]
    assert catalog["voices"][0]["training_status"] == "Active"
    assert writes and "response" in result["result"]
    assert queried_speakers == ["S_CUSTOM"]
    assert result["result"]["action"] == "get_voice"


def test_voice_clone_status_route_forwards_custom_speaker_pair(monkeypatch):
    calls = []

    class Transport:
        @classmethod
        def from_env(cls):
            return cls()

        def query(self, speaker_id, *, custom_speaker_id=""):
            calls.append((speaker_id, custom_speaker_id))
            return {
                "action": "get_voice",
                "speaker_id": speaker_id,
                "custom_speaker_id": custom_speaker_id,
                "status": 1,
                "training_status": "Training",
            }

    monkeypatch.setattr(console, "VoiceCloneTransport", Transport)
    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda *_: None)
    monkeypatch.setattr(console, "_sync_voice_catalog_statuses", lambda result: {
        "updated_voice_keys": [], "status_count": 1, "catalog_written": False,
    })

    result = console._voice_clone_status_readonly({
        "confirm": "readonly-voice-status",
        "speaker_ids": ["custom_speaker_id"],
        "custom_speaker_id": "custom_zh_status01",
    })

    assert result["status"] == "ready"
    assert calls == [("custom_speaker_id", "custom_zh_status01")]


def test_voice_clone_train_updates_existing_speaker_id_and_registers_that_target(monkeypatch):
    calls = []

    class Transport:
        @classmethod
        def from_env(cls):
            return cls()

        def train(self, audio, audio_format, **kwargs):
            calls.append((audio, audio_format, kwargs))
            return {
                "action": "voice_clone",
                "speaker_id": kwargs["speaker_id"],
                "status": 1,
                "training_status": "Training",
                "demo_audio": "https://example.test/demo.mp3",
            }

    monkeypatch.setattr(console, "VoiceCloneTransport", Transport)
    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda *_: None)
    monkeypatch.setattr(console, "_voice_clone_upsert_catalog", lambda **kwargs: {"voice_key": "custom_new", "catalog": {"voices": []}})

    result = console._voice_clone_train({
        "confirm": "voice-clone-training",
        "mode": "prepaid_train",
        "display_name": "新旁白",
        "audio_base64": "YXVkaW8=",
        "filename": "sample.mp3",
        "speaker_id": "S_TARGET",
        "reference_text": "参考文本",
    })

    assert calls == [(b"audio", "mp3", {
        "speaker_id": "S_TARGET",
        "custom_speaker_id": "",
        "allow_provider_generated_speaker_id": False,
        "language": 0,
        "reference_text": "参考文本",
        "demo_text": "",
        "enable_audio_denoise": False,
        "disable_volume_normalization": False,
    })]
    assert result["speaker_id"] == "S_TARGET"
    assert result["voice_key"] == "custom_new"
    assert result["creation_mode"] == "trained"
    assert result["creation_variant"] == "prepaid_slot"


def test_voice_clone_train_requires_explicit_external_write_confirmation():
    with pytest.raises(ValueError, match="voice-clone-training"):
        console._voice_clone_train({"display_name": "旁白", "audio_base64": "YQ==", "filename": "a.wav"})


def test_voice_clone_train_postpaid_create_generates_custom_speaker_id(monkeypatch):
    """新版 V3 后付费创建可在本地上传，并自动生成实际音色代号。"""
    calls = []

    class Transport:
        def train(self, audio, audio_format, **kwargs):
            calls.append((audio, audio_format, kwargs))
            return {
                "action": "voice_clone",
                "speaker_id": "custom_speaker_id",
                "custom_speaker_id": kwargs["custom_speaker_id"],
                "status": 2,
                "training_status": "Success",
                "demo_audio": "https://example.test/demo.mp3",
            }

    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda *_: None)
    monkeypatch.setattr(console, "_voice_clone_transport_from_page", lambda: Transport())
    monkeypatch.setattr(console, "_voice_clone_upsert_catalog", lambda **kwargs: {
        "voice_key": "custom_new", "catalog": {"voices": []},
    })

    result = console._voice_clone_train({
        "confirm": "voice-clone-training",
        "mode": "postpaid_create",
        "display_name": "新旁白",
        "audio_base64": "YXVkaW8=",
        "filename": "sample.mp3",
    })

    assert calls and calls[0][2]["speaker_id"] == "custom_speaker_id"
    assert calls[0][2]["custom_speaker_id"].startswith("custom_zh_")
    assert result["speaker_id"] == calls[0][2]["custom_speaker_id"]
    assert result["custom_speaker_id"] == calls[0][2]["custom_speaker_id"]
    assert result["creation_mode"] == "created"
    assert result["creation_variant"] == "postpaid_custom"
    assert result["custom_speaker_id_generated"] is True


def test_import_official_prepaid_voice_queries_before_local_registration(monkeypatch):
    calls = []

    class Transport:
        def query(self, speaker_id):
            calls.append(speaker_id)
            return {
                "action": "get_voice",
                "speaker_id": speaker_id,
                "status": 2,
                "training_status": "Success",
                "demo_audio": "https://example.test/prepaid-demo.mp3",
            }

    registered = {}
    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda groups: calls.append(tuple(groups)))
    monkeypatch.setattr(console, "_voice_clone_transport_from_page", lambda: Transport())
    monkeypatch.setattr(console, "_voice_clone_upsert_catalog", lambda **kwargs: registered.update(kwargs) or {
        "voice_key": "prepaid_voice", "catalog": {"voices": []},
    })

    result = console._voice_clone_import_official_prepaid({
        "confirm": "register-official-prepaid-voice",
        "speaker_id": "S_PREPAID_REAL",
        "display_name": "我的旁白",
    })

    assert calls == [("tts",), "S_PREPAID_REAL"]
    assert registered["speaker_id"] == "S_PREPAID_REAL"
    assert registered.get("custom_speaker_id", "") == ""
    assert registered["display_name"] == "我的旁白"
    assert result["status"] == "registered"
    assert result["voice_key"] == "prepaid_voice"
    assert result["provider"]["demo_audio"] == "https://example.test/prepaid-demo.mp3"


def test_import_official_prepaid_rejects_unverified_clipboard_id_before_registration(monkeypatch):
    class Transport:
        def query(self, _speaker_id):
            return {"speaker_id": "S_DIFFERENT", "status": 2}

    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda *_: None)
    monkeypatch.setattr(console, "_voice_clone_transport_from_page", lambda: Transport())
    monkeypatch.setattr(console, "_voice_clone_upsert_catalog", lambda **kwargs: pytest.fail("不应登记未核验 ID"))

    with pytest.raises(ValueError, match="未确认与剪贴板一致"):
        console._voice_clone_import_official_prepaid({
            "confirm": "register-official-prepaid-voice",
            "speaker_id": "S_PREPAID_REAL",
        })


def test_voice_clone_train_prepaid_create_with_explicit_slot_registers_it(monkeypatch):
    calls = []

    class Transport:
        def train(self, audio, audio_format, **kwargs):
            calls.append((audio, audio_format, kwargs))
            return {
                "action": "voice_clone",
                "speaker_id": "S_PREPAID_SLOT",
                "provider_speaker_id": "S_PREPAID_SLOT",
                "status": 1,
                "training_status": "Training",
            }

    registered = {}
    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda *_: None)
    monkeypatch.setattr(console, "_voice_clone_transport_from_page", lambda: Transport())
    monkeypatch.setattr(console, "_voice_clone_upsert_catalog", lambda **kwargs: registered.update(kwargs) or {
        "voice_key": "prepaid_created", "catalog": {"voices": []},
    })

    result = console._voice_clone_train({
        "confirm": "voice-clone-training",
        "mode": "prepaid_create",
        "display_name": "首次预付费旁白",
        "audio_base64": "YXVkaW8=",
        "filename": "sample.wav",
        "speaker_id": "S_PREPAID_SLOT",
    })

    assert calls[0][2]["speaker_id"] == "S_PREPAID_SLOT"
    assert calls[0][2]["custom_speaker_id"] == ""
    assert calls[0][2]["allow_provider_generated_speaker_id"] is False
    assert registered["speaker_id"] == "S_PREPAID_SLOT"
    assert registered["custom_speaker_id"] == ""
    assert result["speaker_id"] == "S_PREPAID_SLOT"
    assert result["speaker_id_generated_by_provider"] is False
    assert result["creation_variant"] == "prepaid_slot"


def test_voice_clone_train_prepaid_create_without_slot_uses_provider_allocation(monkeypatch):
    calls = []

    class Transport:
        def train(self, audio, audio_format, **kwargs):
            calls.append((audio, audio_format, kwargs))
            return {
                "action": "voice_clone",
                "speaker_id": "S_PROVIDER_ALLOCATED",
                "provider_speaker_id": "S_PROVIDER_ALLOCATED",
                "status": 1,
                "training_status": "Training",
            }

    registered = {}
    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda *_: None)
    monkeypatch.setattr(console, "_voice_clone_transport_from_page", lambda: Transport())
    monkeypatch.setattr(console, "_voice_clone_upsert_catalog", lambda **kwargs: registered.update(kwargs) or {
        "voice_key": "prepaid_created", "catalog": {"voices": []},
    })

    result = console._voice_clone_train({
        "confirm": "voice-clone-training",
        "mode": "prepaid_create",
        "display_name": "首次自动分配旁白",
        "audio_base64": "YXVkaW8=",
        "filename": "sample.wav",
    })

    assert calls[0][2]["speaker_id"] == ""
    assert calls[0][2]["allow_provider_generated_speaker_id"] is True
    assert registered["speaker_id"] == "S_PROVIDER_ALLOCATED"
    assert result["speaker_id"] == "S_PROVIDER_ALLOCATED"
    assert result["speaker_id_generated_by_provider"] is True
    assert result["speaker_id_auto_selected"] is True


def test_voice_clone_train_prepaid_update_without_imported_slot_explains_import(monkeypatch):
    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda *_: None)
    with pytest.raises(ValueError, match="必须提供.*speaker_id"):
        console._voice_clone_train({
            "confirm": "voice-clone-training",
            "mode": "prepaid_update",
            "display_name": "预付费旁白",
            "audio_base64": "YXVkaW8=",
            "filename": "sample.mp3",
        })


def test_voice_clone_train_prepaid_uses_explicit_selected_slot(monkeypatch):
    calls = []

    class Transport:
        @classmethod
        def from_env(cls):
            return cls()

        def train(self, audio, audio_format, **kwargs):
            calls.append((audio, audio_format, kwargs))
            return {
                "action": "voice_clone",
                "speaker_id": kwargs["speaker_id"],
                "status": 1,
                "training_status": "Training",
            }

    monkeypatch.setattr(console, "VoiceCloneTransport", Transport)
    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda *_: None)
    monkeypatch.setattr(console, "_voice_clone_prepaid_slot_candidates", lambda: [
        {"voice_key": "prepaid_old", "speaker_id": "S_PREPAID_OLD"},
        {"voice_key": "prepaid_new", "speaker_id": "S_PREPAID_NEW"},
    ])
    monkeypatch.setattr(console, "_voice_clone_upsert_catalog", lambda **kwargs: {
        "voice_key": "prepaid_old", "catalog": {"voices": []},
    })

    result = console._voice_clone_train({
        "confirm": "voice-clone-training",
        "mode": "prepaid_train",
        "display_name": "自动槽位旁白",
        "audio_base64": "YXVkaW8=",
        "filename": "sample.mp3",
        "speaker_id": "S_PREPAID_OLD",
    })

    assert calls[0][2]["speaker_id"] == "S_PREPAID_OLD"
    assert result["speaker_id"] == "S_PREPAID_OLD"
    assert result["prepaid_slot_voice_key"] == ""
    assert result["speaker_id_auto_selected"] is False
    assert result["creation_variant"] == "prepaid_slot"


def test_voice_clone_train_rejects_mismatched_selected_prepaid_slot(monkeypatch):
    monkeypatch.setattr(console, "_api_management_require_runtime_credentials", lambda *_: None)
    monkeypatch.setattr(console, "_voice_clone_prepaid_slot_candidates", lambda: [
        {"voice_key": "prepaid_one", "speaker_id": "S_PREPAID_ONE"},
    ])

    with pytest.raises(ValueError, match="speaker_id 与所选预付费槽位不一致"):
        console._voice_clone_train({
            "confirm": "voice-clone-training",
            "mode": "prepaid_train",
            "display_name": "不一致旁白",
            "speaker_id": "S_OTHER",
            "prepaid_slot_voice_key": "prepaid_one",
            "audio_base64": "YXVkaW8=",
            "filename": "sample.mp3",
        })


def test_import_prepaid_slots_persists_existing_console_ids_without_order(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "VOICE_CATALOG_PATH", tmp_path / "voice_catalog.json")
    ids = [f"S_PREPAID_{index:02d}" for index in range(1, 13)]

    result = console._voice_clone_import_prepaid_slots({
        "confirm": "import-prepaid-voice-slots",
        "speaker_ids_text": "\n".join(ids),
    })

    assert result["status"] == "ready"
    assert result["imported_count"] == 12
    assert result["updated_count"] == 0
    assert result["slot_count"] == 12
    catalog = result["catalog"]
    slots = [item for item in catalog["voices"] if item.get("billing_mode") == "prepaid_slot"]
    assert [item["speaker_id"] for item in slots] == ids
    assert all(item["custom_speaker_id"] == "" for item in slots)
    assert all(item["training_status"] == "slot_imported" for item in slots)
    assert all(item["authorization_status"] == "console_slot" for item in slots)
    assert all(item["enabled"] is True and item["verified"] is False for item in slots)


def test_import_prepaid_slots_is_idempotent_and_does_not_overwrite_verified_state(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "VOICE_CATALOG_PATH", tmp_path / "voice_catalog.json")
    first = console._voice_clone_import_prepaid_slots({
        "confirm": "import-prepaid-voice-slots",
        "slots": [{"speaker_id": "S_PREPAID_ONE", "name": "我的槽位"}],
    })
    voice_key = first["imported_voice_keys"][0]
    console._voice_catalog_store().update(voice_key, {"verified": True, "training_status": "Active"})

    second = console._voice_clone_import_prepaid_slots({
        "confirm": "import-prepaid-voice-slots",
        "speaker_ids": ["S_PREPAID_ONE"],
    })

    assert second["imported_count"] == 0
    assert second["updated_count"] == 1
    item = next(item for item in second["catalog"]["voices"] if item["voice_key"] == voice_key)
    assert item["display_name"] == "我的槽位"
    assert item["verified"] is True
    assert item["training_status"] == "Active"
    assert item["billing_mode"] == "prepaid_slot"


def test_import_prepaid_slots_http_route_is_local_only(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "DEV_READ_ONLY", False)
    monkeypatch.setattr(console, "VOICE_CATALOG_PATH", tmp_path / "voice_catalog.json")
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({
            "confirm": "import-prepaid-voice-slots",
            "speaker_ids_text": "S_ROUTE_01\nS_ROUTE_02",
        }).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/voices/import-slots",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert payload["slot_count"] == 2
        assert payload["action"] == "import_prepaid_voice_slots"
    finally:
        server.shutdown()
        server.server_close()


def test_legacy_voice_resource_routes_are_removed_without_supplier_call(monkeypatch):
    """旧槽位查询/下单路由只返回 410，不再构造旧 transport。"""

    monkeypatch.setattr(console, "DEV_READ_ONLY", False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for route in ("/api/voices/resource-status", "/api/voices/resource-order"):
            request = Request(
                f"http://127.0.0.1:{server.server_port}{route}",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(HTTPError) as raised:
                urlopen(request)
            assert raised.value.code == 410
            body = json.loads(raised.value.read().decode("utf-8"))
            assert body["status"] == "disabled"
    finally:
        server.shutdown()
        server.server_close()


def test_voice_clone_profile_update_requires_patch(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "VOICE_CATALOG_PATH", tmp_path / "voice_catalog.json")
    created = console._register_voice_profile({
        "display_name": "我的定制旁白",
        "speaker_id": "S_PATCH_001",
    })
    monkeypatch.setattr(console, "DEV_READ_ONLY", False)
    voice_key = created["voice_key"]

    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        post_request = Request(
            f"http://127.0.0.1:{server.server_port}/api/voices/{voice_key}",
            data=b'{"enabled":true}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as raised:
            urlopen(post_request)
        assert raised.value.code == 404
        assert json.loads(raised.value.read().decode("utf-8"))["status"] == "not_found"

        patch_request = Request(
            f"http://127.0.0.1:{server.server_port}/api/voices/{voice_key}",
            data='{"display_name":"更新后的定制旁白","enabled":true}'.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urlopen(patch_request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["status"] == "updated"
        assert payload["voice_key"] == voice_key
        updated = next(item for item in payload["catalog"]["voices"] if item["voice_key"] == voice_key)
        assert updated["display_name"] == "更新后的定制旁白"
        assert updated["enabled"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_voice_clone_status_sync_keeps_new_icl_resource_version(monkeypatch):
    catalog = {"version": "v1", "voices": [{
        "voice_key": "custom_demo", "speaker_id": "S_CUSTOM", "display_name": "我的旁白",
        "voice_type": "custom", "resource_id": "seed-icl-2.0", "model": "seed-tts-2.0-standard",
        "training_status": "Training", "authorization_status": "pending",
        "enabled": True, "verified": True,
    }]}
    writes = []

    class Store:
        def load(self):
            return catalog

        def save(self, value):
            writes.append(value)
            return value

    monkeypatch.setattr(console, "_voice_catalog_store", lambda: Store())

    result = console._sync_voice_catalog_statuses({
        "response": {"Result": {"Statuses": [{
            "SpeakerID": "S_CUSTOM", "State": "Active",
            "ModelTypeDetails": [{"ResourceID": "seed-icl-1.0"}],
        }]}},
    })

    assert result["updated_voice_keys"] == ["custom_demo"]
    assert catalog["voices"][0]["resource_id"] == "seed-icl-2.0"
    assert catalog["voices"][0]["enabled"] is True
    assert catalog["voices"][0]["verified"] is True
    assert writes


def test_official_voice_catalog_route_reads_local_catalog_without_legacy_openapi(monkeypatch):
    catalog = {"version": "v1", "voices": []}
    class Store:
        def load(self):
            return catalog
        def save(self, value):
            return value

    monkeypatch.setattr(console, "_voice_catalog_store", lambda: Store())
    with pytest.raises(ValueError, match="confirm=readonly-voice-catalog-list"):
        console._voice_catalog_list_official({})

    result = console._voice_catalog_list_official({
        "confirm": "readonly-voice-catalog-list",
    })
    assert result["status"] == "ready"
    assert result["evidence_level"] == "local_voice_catalog"
    assert result["catalog_sync"]["listed_count"] == 0
    assert result["catalog_sync"]["catalog_written"] is False


def test_official_voice_catalog_post_route_dispatches_without_exposing_secrets(monkeypatch):
    monkeypatch.setattr(console, "DEV_READ_ONLY", False)
    monkeypatch.setattr(
        console,
        "_voice_catalog_list_official",
        lambda payload: {"status": "ready", "received_confirm": payload.get("confirm", "")},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"confirm": "readonly-voice-catalog-list"}).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/voices/list-official",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload == {
            "status": "ready",
            "received_confirm": "readonly-voice-catalog-list",
        }
        assert "api_key" not in json.dumps(payload).lower()
        assert "secret" not in json.dumps(payload).lower()
    finally:
        server.shutdown()
        server.server_close()
