import time
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from threading import Thread
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from http.server import ThreadingHTTPServer
import pytest

import tools.video_production_console as console
from workflow_1256.style_package_store import StylePackageStore


def test_auto_asset_resume_is_disabled_without_explicit_opt_in(monkeypatch):
    """服务启动默认不得扫描并恢复历史素材任务。"""

    monkeypatch.delenv(console.AUTO_RESUME_ASSETS_ENV, raising=False)
    monkeypatch.setattr(
        console,
        "_persisted_task_snapshots",
        lambda: pytest.fail("默认启动不应扫描历史素材任务"),
    )

    assert console._automatic_asset_resume_enabled() is False
    assert console._auto_resume_asset_tasks() == []


def test_auto_asset_resume_only_runs_with_explicit_opt_in(monkeypatch):
    """显式设置 VIDEO_CONSOLE_AUTO_RESUME_ASSETS=1 后才允许恢复。"""

    monkeypatch.setenv(console.AUTO_RESUME_ASSETS_ENV, "1")
    task = {
        "task_id": "asset-resume-opt-in",
        "task_type": "asset_production",
        "state": "interrupted",
        "error_code": "executor_restarted",
    }
    resumed = []
    monkeypatch.setattr(console, "_persisted_task_snapshots", lambda: [task])
    monkeypatch.setattr(console, "_asset_checkpoint_has_resumable_grid_handles", lambda _task: True)
    monkeypatch.setattr(
        console,
        "_resume_asset_production_task",
        lambda task_id, *, automatic: resumed.append((task_id, automatic)),
    )

    assert console._automatic_asset_resume_enabled() is True
    assert console._auto_resume_asset_tasks() == ["asset-resume-opt-in"]
    assert resumed == [("asset-resume-opt-in", True)]


def test_asset_checkpoint_requires_every_expected_grid_handle(monkeypatch, tmp_path):
    checkpoint = tmp_path / "first_frame_checkpoint.json"
    checkpoint.write_text(json.dumps({
        "version": 1,
        "batches": {
            "first_frame_grid_001": {"task_id": "task-1", "status": "submitted"},
            "first_frame_grid_002": {"status": "creating"},
            "first_frame_grid_003": {"task_id": "stale-extra", "status": "submitted"},
        },
    }, ensure_ascii=False), encoding="utf-8")
    task = {
        "task_type": "asset_production",
        "result": {
            "output_dir": str(tmp_path),
            "categories": {
                "first_frame_images": {"total": 2},
            },
        },
    }
    monkeypatch.setattr(console, "_asset_checkpoint_path", lambda _task: checkpoint)

    assert console._asset_checkpoint_has_resumable_grid_handles(task) is False


def test_asset_checkpoint_ignores_stale_extra_grid_when_required_grids_complete(monkeypatch, tmp_path):
    checkpoint = tmp_path / "first_frame_checkpoint.json"
    checkpoint.write_text(json.dumps({
        "version": 1,
        "batches": {
            "first_frame_grid_001": {"task_id": "task-1", "status": "submitted"},
            "first_frame_grid_002": {"image_url": "https://img.test/grid-2.png", "status": "completed"},
            "first_frame_grid_003": {"status": "creating"},
        },
    }, ensure_ascii=False), encoding="utf-8")
    task = {
        "task_type": "asset_production",
        "result": {
            "output_dir": str(tmp_path),
            "categories": {
                "first_frame_images": {"total": 2},
            },
        },
    }
    monkeypatch.setattr(console, "_asset_checkpoint_path", lambda _task: checkpoint)

    assert console._asset_checkpoint_has_resumable_grid_handles(task) is True


def test_api_management_snapshot_reports_api_types_without_exposing_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setenv("DIRECTORS_V2_API_KEY", "test-secret-should-not-render")
    monkeypatch.setenv("DIRECTORS_V2_API_URL", "https://example.test/v1/chat/completions?token=hidden")
    monkeypatch.setenv("DIRECTORS_V2_MODEL_TEXT", "test-model")
    snapshot = console._api_management_snapshot()

    assert [item["id"] for item in snapshot["groups"]] == [
        "language-model", "image-generation", "tikhub", "video-generation",
        "digital-human", "tts", "sound-effect", "music-generation", "tos",
    ]
    group = next(item for item in snapshot["groups"] if item["id"] == "language-model")
    channel = next(item for item in group["channels"] if item["id"] == "visual-guidance")
    assert group["status"] in {"ready", "partial"}
    assert group["channel_count"] == 3
    assert channel["status"] == "ready"
    assert channel["endpoint"] == "https://example.test/v1/chat/completions"
    assert channel["primary_model"] == "test-model"
    assert channel["provider_names"] == ["directors_v2 编导插件"]
    assert any(node["id"] == "assets.visual-guidance" for node in group["nodes"])
    assert "test-secret-should-not-render" not in json.dumps(snapshot, ensure_ascii=False)

    tos = next(item for item in snapshot["groups"] if item["id"] == "tos")
    tos_channel = tos["channels"][0]
    assert tos_channel["model_slot_title"] == "存储桶"
    assert tos_channel["model_slot_count_label"] == "存储桶"


def test_sound_effect_snapshot_reuses_page_tts_key_without_exposing_it(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(
        console,
        "_api_management_load_secrets",
        lambda *args, **kwargs: {"tts": {"primary_api_key": "stored-tts-key"}},
    )
    monkeypatch.setattr(
        console,
        "_api_management_secret_health_snapshot",
        lambda: {"state": "loaded", "scope": "machine", "message": "", "recovery_backup_path": ""},
    )

    snapshot = console._api_management_snapshot()
    sound_effect = next(group for group in snapshot["groups"] if group["id"] == "sound-effect")
    channel = sound_effect["channels"][0]

    assert sound_effect["status"] == "ready"
    assert channel["status_label"] == "复用 TTS 主 API Key"
    assert channel["source"] == "复用 TTS 主 API Key"
    assert channel["capabilities"]["shared_credential_channel"] == "tts"
    assert "stored-tts-key" not in json.dumps(snapshot, ensure_ascii=False)


def test_api_management_runtime_audit_matches_real_node_entrypoints(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    snapshot = console._api_management_snapshot()
    audit = snapshot["runtime_audit"]
    nodes = {item["node_id"]: item for item in audit["nodes"]}

    assert nodes["director.cinematic-story"]["effective_channel_id"] == "director-seed21"
    assert nodes["director.cinematic-story"]["status"] in {
        "document_fallback", "env_fallback", "wired", "blocked", "partial"
    }
    assert nodes["assets.bgm-merge"]["status"] == "local"
    assert nodes["assets.bgm-merge"]["auth_source"] == "无需外部鉴权"
    assert nodes["assets.bgm-merge"]["auth_verification_status"] == "不适用"
    assert nodes["director.cinematic-story"]["auth_verification_status"] == "未执行供应商调用验证"
    assert nodes["assets.first-frame-grid"]["dependency_channel_ids"] == [
        "image-generation", "audio-storage"
    ]
    assert nodes["assets.bgm-generation"]["dependency_channel_ids"] == [
        "bgm-audio", "audio-storage"
    ]
    image_channel = next(
        item for item in next(group for group in snapshot["groups"] if group["id"] == "image-generation")["channels"]
        if item["id"] == "image-generation"
    )
    assert all(node["id"] != "assets.first-frame-publish" for node in image_channel["nodes"])
    assert "assets.first-frame-publish" not in nodes
    assert audit["business_plan_unapplied"] <= audit["auth_unverified"]


def test_api_management_tos_runtime_publisher_includes_backup_bucket(monkeypatch):
    values = {
        "ARK_TTS_TOS_ACCESS_KEY": "primary-ak",
        "ARK_TTS_TOS_SECRET_KEY": "primary-sk",
        "ARK_TTS_TOS_BUCKET": "primary-bucket",
        "ARK_TTS_TOS_ENDPOINT": "https://tos.example.test",
        "ARK_TTS_TOS_REGION": "cn-test-1",
        "ARK_TTS_TOS_BACKUP_ACCESS_KEY": "backup-ak",
        "ARK_TTS_TOS_BACKUP_SECRET_KEY": "backup-sk",
        "ARK_TTS_TOS_BACKUP_BUCKET": "backup-bucket",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    publisher = console._tos_publisher_from_runtime_auth(
        {"tos_access_key": "document-ak", "tos_secret_key": "document-sk", "tos_bucket": "document-bucket"},
        prefix="test",
    )

    assert [item.bucket for item in publisher.buckets] == ["primary-bucket", "backup-bucket"]
    assert publisher.failover_strategy == "primary_then_backup"


def test_api_management_route_preview_is_read_only_and_uses_saved_orders(monkeypatch, tmp_path):
    config_path = tmp_path / "api_management.json"
    config_path.write_text(json.dumps({
        "version": 1,
        "groups": {
            "story-writing": {
                "endpoint": "https://story.example.test/responses",
                "model_order": ["STORY_WRITER_ARK_MODEL", "CONTENT_ANALYZER_ARK_MODEL"],
            },
            "director-seed21": {
                "endpoint": "https://director.example.test/chat/completions",
                "model_order": ["VOICE_DIRECTOR_PRIMARY_MODEL"],
            },
        },
        "api_groups": {"language-model": {"order": ["story-writing", "director-seed21"]}},
        "api_functions": {
            "language-model": {
                "director.cinematic-story": {"priority": "api_first", "max_attempts": 3},
            },
        },
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", config_path)

    preview = console._api_management_route_preview("director.cinematic-story")

    assert preview["mode"] == "dry_run"
    assert preview["message"] == "仅计算调用顺序，不发送请求，不产生第三方费用。"
    assert preview["function"]["business_plan_applied"] is False
    assert preview["function"]["priority"] == "api_first"
    assert "max_attempts" not in preview["function"]
    assert preview["plan"]
    api_ids = [item["api_id"] for item in preview["plan"]]
    assert api_ids[0] == "story-writing"
    assert "director-seed21" in api_ids
    assert api_ids.index("story-writing") < api_ids.index("director-seed21")


def test_api_management_controlled_auth_verify_requires_explicit_confirmation():
    with pytest.raises(ValueError, match="controlled-auth-verification"):
        console._api_management_controlled_auth_verify({})


def test_api_management_controlled_auth_verify_marks_paid_channels_unchecked(monkeypatch):
    channels = [
        {"id": channel_id, "name": channel_id, "nodes": []}
        for channel_id in (
            "story-writing", "director-seed21", "tts", "audio-storage",
            "image-generation", "video-generation", "digital-human", "bgm-audio",
        )
    ]
    monkeypatch.setattr(console, "_api_management_snapshot", lambda: {
        "groups": [{"channels": channels}],
    })
    monkeypatch.setattr(console, "_api_management_verify_story_channel", lambda channel: [{
        "channel_id": "story-writing", "status": "passed", "auth_verified": True,
    }])
    monkeypatch.setattr(console, "_api_management_verify_director_channel", lambda channel: [{
        "channel_id": "director-seed21", "status": "passed", "auth_verified": True,
    }])
    monkeypatch.setattr(console, "_api_management_verify_tts_channel", lambda channel: [{
        "channel_id": "tts", "status": "passed", "auth_verified": True,
    }])
    monkeypatch.setattr(console, "_api_management_verify_tos_channel", lambda channel: [{
        "channel_id": "audio-storage", "status": "passed", "auth_verified": True,
    }])

    result = console._api_management_controlled_auth_verify({
        "confirm": "controlled-auth-verification",
        "channels": [
            "story-writing", "director-seed21", "tts", "audio-storage",
            "image-generation", "video-generation", "digital-human", "bgm-audio",
        ],
    })

    assert result["mode"] == "controlled_live"
    assert result["summary"]["passed"] == 4
    assert result["summary"]["not_checked"] == 4
    assert all(
        item["status"] == "not_checked"
        for item in result["results"]
        if item.get("channel_id") in {"image-generation", "video-generation", "digital-human", "bgm-audio"}
    )


def test_api_management_recognizes_markdown_escaped_auth_document(monkeypatch, tmp_path):
    document = tmp_path / "auth.md"
    document.write_text('export ARK\\_API\\_KEY="masked"\n', encoding="utf-8")
    monkeypatch.setenv("DIRECTOR_AUTH_DOCUMENT", str(document))

    spec = next(item for item in console.API_MANAGEMENT_SPECS if item["id"] == "story-model")

    configured, path = console._api_management_document_state(spec)

    assert configured is True
    assert path == str(document)


def test_api_management_exposes_multiple_model_slots_and_applies_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    for env_name in (
        "STORY_WRITER_ARK_MODEL",
        "CONTENT_ANALYZER_ARK_MODEL",
        "STYLE_ADAPTER_ARK_MODEL",
        "DIRECTORS_V2_ARK_MODEL",
        "API_MANAGEMENT_MODEL_FALLBACKS_STORY_WRITING",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("DIRECTORS_V2_API_KEY", "model-slot-test-key")
    monkeypatch.setenv("DIRECTORS_V2_API_URL", "https://example.test/v1/chat/completions")
    monkeypatch.setenv("DIRECTORS_V2_MODEL_TEXT", "text-model")
    monkeypatch.setenv("DIRECTORS_V2_MODEL_VISION", "vision-model")
    monkeypatch.setenv("DIRECTORS_V2_MODEL_ROUTER", "router-model")

    snapshot = console._api_management_snapshot()
    channel = next(
        item for item in next(group for group in snapshot["groups"] if group["id"] == "language-model")["channels"]
        if item["id"] == "visual-guidance"
    )
    assert channel["model_count"] == 3
    assert channel["provider_names"] == ["directors_v2 编导插件"]
    assert [item["source"] for item in channel["model_slots"]] == [
        "DIRECTORS_V2_MODEL_TEXT", "DIRECTORS_V2_MODEL_VISION", "DIRECTORS_V2_MODEL_ROUTER"
    ]
    assert [item["model_id"] for item in channel["model_slots"]] == [
        "text-model", "vision-model", "router-model"
    ]
    assert [item["model_name"] for item in channel["model_slots"]] == ["", "", ""]
    assert [item["usage_label"] for item in channel["model_slots"]] == [
        "文本模型", "视觉模型", "路由模型"
    ]
    story_channel = next(
        item for item in next(group for group in snapshot["groups"] if group["id"] == "language-model")["channels"]
        if item["id"] == "story-writing"
    )
    assert story_channel["model_count"] == 1
    assert [item["source"] for item in story_channel["model_slots"]] == [
        "STORY_WRITER_ARK_MODEL"
    ]

    updated_snapshot = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": "visual-guidance",
        "settings": {
            "endpoint": "https://example.test/v1/chat/completions",
            "model_slots": {
                "DIRECTORS_V2_MODEL_TEXT": "text-model-v2",
                "DIRECTORS_V2_MODEL_VISION": "vision-model-v2",
                "DIRECTORS_V2_MODEL_ROUTER": "router-model-v2",
            },
            "model_names": {
                "DIRECTORS_V2_MODEL_TEXT": "文本生成模型",
                "DIRECTORS_V2_MODEL_VISION": "视觉理解模型",
                "DIRECTORS_V2_MODEL_ROUTER": "模型路由器",
            },
            "strategy": "retry_same",
            "max_attempts": 2,
            "timeout_seconds": 30,
        },
    })
    assert os.environ["DIRECTORS_V2_MODEL_TEXT"] == "text-model-v2"
    assert os.environ["DIRECTORS_V2_MODEL_VISION"] == "vision-model-v2"
    assert os.environ["DIRECTORS_V2_MODEL_ROUTER"] == "router-model-v2"
    updated_channel = next(
        item for item in next(group for group in updated_snapshot["groups"] if group["id"] == "language-model")["channels"]
        if item["id"] == "visual-guidance"
    )
    assert [item["model_id"] for item in updated_channel["model_slots"]] == [
        "text-model-v2", "vision-model-v2", "router-model-v2"
    ]
    assert [item["model_name"] for item in updated_channel["model_slots"]] == [
        "文本生成模型", "视觉理解模型", "模型路由器"
    ]


def test_api_management_persists_model_order_for_failover_plan(monkeypatch, tmp_path):
    config_path = tmp_path / "api_management.json"
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", config_path)
    monkeypatch.setenv("DIRECTORS_V2_MODEL_TEXT", "text-model")
    monkeypatch.setenv("DIRECTORS_V2_MODEL_VISION", "vision-model")
    monkeypatch.setenv("DIRECTORS_V2_MODEL_ROUTER", "router-model")

    snapshot = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": "visual-guidance",
        "settings": {
            "model_slots": {
                "DIRECTORS_V2_MODEL_TEXT": "text-model",
                "DIRECTORS_V2_MODEL_VISION": "vision-model",
                "DIRECTORS_V2_MODEL_ROUTER": "router-model",
            },
            "model_names": {
                "DIRECTORS_V2_MODEL_TEXT": "文本模型",
                "DIRECTORS_V2_MODEL_VISION": "视觉模型",
                "DIRECTORS_V2_MODEL_ROUTER": "路由模型",
            },
            "model_order": [
                "DIRECTORS_V2_MODEL_ROUTER",
                "DIRECTORS_V2_MODEL_VISION",
                "DIRECTORS_V2_MODEL_TEXT",
            ],
        },
    })

    group = next(item for item in snapshot["groups"] if item["id"] == "language-model")
    channel = next(item for item in group["channels"] if item["id"] == "visual-guidance")
    assert [item["source"] for item in channel["model_slots"]] == [
        "DIRECTORS_V2_MODEL_ROUTER",
        "DIRECTORS_V2_MODEL_VISION",
        "DIRECTORS_V2_MODEL_TEXT",
    ]
    visual_function = next(item for item in group["functions"] if item["id"] == "assets.visual-guidance")
    visual_plan = [step for step in visual_function["failover"]["plan"] if step["api_id"] == "visual-guidance"]
    assert [step["model_source"] for step in visual_plan] == [
        "DIRECTORS_V2_MODEL_ROUTER",
        "DIRECTORS_V2_MODEL_VISION",
        "DIRECTORS_V2_MODEL_TEXT",
    ]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["groups"]["visual-guidance"]["model_order"] == [
        "DIRECTORS_V2_MODEL_ROUTER",
        "DIRECTORS_V2_MODEL_VISION",
        "DIRECTORS_V2_MODEL_TEXT",
    ]


def test_api_management_can_assign_content_and_style_models_separately(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setenv("STORY_WRITER_ARK_API_KEY", "story-model-test-key")
    monkeypatch.delenv("STORY_WRITER_ARK_MODEL", raising=False)
    monkeypatch.delenv("DIRECTORS_V2_ARK_MODEL", raising=False)
    monkeypatch.delenv("CONTENT_ANALYZER_ARK_MODEL", raising=False)
    monkeypatch.delenv("STYLE_ADAPTER_ARK_MODEL", raising=False)
    updated = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": "story-writing",
        "settings": {
            "model_slots": {
                "STORY_WRITER_ARK_MODEL": "story-model",
                "CONTENT_ANALYZER_ARK_MODEL": "content-model",
                "STYLE_ADAPTER_ARK_MODEL": "style-model",
                "DIRECTORS_V2_ARK_MODEL": "director-model",
            },
            "model_names": {
                "CONTENT_ANALYZER_ARK_MODEL": "内容分析器",
                "STYLE_ADAPTER_ARK_MODEL": "风格适配器",
            },
        },
    })
    channel = next(
        item for item in next(group for group in updated["groups"] if group["id"] == "language-model")["channels"]
        if item["id"] == "story-writing"
    )
    slots = {item["source"]: item for item in channel["model_slots"]}
    assert slots["CONTENT_ANALYZER_ARK_MODEL"]["model_id"] == "content-model"
    assert slots["CONTENT_ANALYZER_ARK_MODEL"]["model_name"] == "内容分析器"
    assert slots["STYLE_ADAPTER_ARK_MODEL"]["model_id"] == "style-model"
    assert slots["STYLE_ADAPTER_ARK_MODEL"]["model_name"] == "风格适配器"
    assert os.environ["CONTENT_ANALYZER_ARK_MODEL"] == "content-model"
    assert os.environ["STYLE_ADAPTER_ARK_MODEL"] == "style-model"


def test_api_management_adds_custom_model_slots_under_one_api(monkeypatch, tmp_path):
    config_path = tmp_path / "api_management.json"
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", config_path)
    monkeypatch.setenv("DIRECTORS_V2_MODEL_TEXT", "text-model")
    monkeypatch.setenv("DIRECTORS_V2_MODEL_VISION", "vision-model")
    monkeypatch.setenv("DIRECTORS_V2_MODEL_ROUTER", "router-model")

    updated_snapshot = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": "visual-guidance",
        "settings": {
            "model_slots": {
                "DIRECTORS_V2_MODEL_TEXT": "text-model",
                "DIRECTORS_V2_MODEL_VISION": "vision-model",
                "DIRECTORS_V2_MODEL_ROUTER": "router-model",
                "custom_model_third": "custom-model-3",
            },
            "model_names": {
                "DIRECTORS_V2_MODEL_TEXT": "文本模型",
                "DIRECTORS_V2_MODEL_VISION": "视觉模型",
                "DIRECTORS_V2_MODEL_ROUTER": "路由模型",
                "custom_model_third": "第三模型",
            },
        },
    })

    channel = next(
        item for item in next(group for group in updated_snapshot["groups"] if group["id"] == "language-model")["channels"]
        if item["id"] == "visual-guidance"
    )
    assert channel["model_count"] == 4
    assert channel["model_slots"][-1]["source"] == "custom_model_third"
    assert channel["model_slots"][-1]["model_id"] == "custom-model-3"
    assert channel["model_slots"][-1]["model_name"] == "第三模型"
    function = next(item for item in next(group for group in updated_snapshot["groups"] if group["id"] == "language-model")["functions"] if item["id"] == "writing.case-rewrite")
    assert any(step["model_id"] == "custom-model-3" and step["model_name"] == "第三模型" for step in function["failover"]["plan"])

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["groups"]["visual-guidance"]["model_slots"]["custom_model_third"] == "custom-model-3"


def test_api_management_adds_another_api_credential_group_and_orders_it(monkeypatch, tmp_path):
    config_path = tmp_path / "api_management.json"
    secret_path = tmp_path / "api_management_secrets.bin"
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", config_path)
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", secret_path)

    custom_channel_id = "story-writing__custom_test"
    updated_snapshot = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": custom_channel_id,
        "channel_definition": {
            "base_channel_id": "story-writing",
            "label": "故事备用 API",
        },
        "group_settings": {
            "api_failover": {
                "order": [custom_channel_id, "story-writing", "director-seed21", "visual-guidance"],
            },
        },
        "settings": {
            "endpoint": "https://backup.example.test/v1/responses",
            "model_slots": {"custom_model_story_alt": "story-alt-model"},
            "model_names": {"custom_model_story_alt": "故事备用模型"},
            "strategy": "primary_then_backup",
            "max_attempts": 2,
            "timeout_seconds": 60,
        },
        "secrets": {"primary_api_key": "custom-channel-secret"},
    })

    group = next(item for item in updated_snapshot["groups"] if item["id"] == "language-model")
    custom = next(item for item in group["channels"] if item["id"] == custom_channel_id)
    assert group["api_failover"]["order"][0] == custom_channel_id
    assert custom["credential_name"] == "故事备用 API"
    assert custom["endpoint"] == "https://backup.example.test/v1/responses"
    assert custom["model_count"] == 1
    assert custom["model_slots"][0]["model_id"] == "story-alt-model"
    assert custom["secret_status"]["primary_api_key"] is True
    assert any(step["api_id"] == custom_channel_id for step in next(item for item in group["functions"] if item["id"] == "writing.case-rewrite")["failover"]["plan"])

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["api_channels"]["language-model"][0]["id"] == custom_channel_id
    assert saved["groups"][custom_channel_id]["endpoint"] == "https://backup.example.test/v1/responses"

    deleted_snapshot = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": custom_channel_id,
        "delete_channel": True,
    })
    deleted_group = next(item for item in deleted_snapshot["groups"] if item["id"] == "language-model")
    assert all(item["id"] != custom_channel_id for item in deleted_group["channels"])
    assert custom_channel_id not in deleted_group["api_failover"]["order"]
    assert custom_channel_id not in console._api_management_load_secrets()
    saved_after_delete = json.loads(config_path.read_text(encoding="utf-8"))
    assert all(item["id"] != custom_channel_id for item in saved_after_delete["api_channels"]["language-model"])
    deleted_builtin_snapshot = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": "visual-guidance",
        "delete_channel": True,
    })
    deleted_builtin_group = next(item for item in deleted_builtin_snapshot["groups"] if item["id"] == "language-model")
    assert all(item["id"] != "visual-guidance" for item in deleted_builtin_group["channels"])
    assert "visual-guidance" not in deleted_builtin_group["api_failover"]["order"]
    saved_after_builtin_delete = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved_after_builtin_delete["removed_channels"]["language-model"] == ["visual-guidance"]


def test_api_management_exposes_and_saves_api_to_api_failover_order(monkeypatch, tmp_path):
    config_path = tmp_path / "api_management.json"
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", config_path)
    for env_name in (
        "STORY_WRITER_ARK_MODEL", "CONTENT_ANALYZER_ARK_MODEL",
        "STYLE_ADAPTER_ARK_MODEL", "DIRECTORS_V2_ARK_MODEL",
    ):
        monkeypatch.delenv(env_name, raising=False)
    snapshot = console._api_management_snapshot()
    group = next(item for item in snapshot["groups"] if item["id"] == "language-model")
    assert group["api_failover"]["order"] == ["story-writing", "director-seed21", "visual-guidance"]
    assert "priority" not in group["api_failover"]
    function = next(item for item in group["functions"] if item["id"] == "writing.case-rewrite")
    assert function["failover"]["priority"] == "api_first"
    assert [item["api_id"] for item in function["failover"]["plan"]] == [
        "story-writing", "director-seed21",
        "visual-guidance", "visual-guidance", "visual-guidance",
    ]
    assert function["failover"]["plan"][0]["model_id"] == "ep-20260609123759-6sv2j"
    assert function["failover"]["plan"][0]["model_name"] == "未命名模型"
    assert "usage_label" not in function["failover"]["plan"][0]

    updated = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": "director-seed21",
        "group_settings": {
            "api_failover": {
                "order": ["visual-guidance", "story-writing", "director-seed21"],
            },
        },
        "function_settings": {
            "writing.case-rewrite": {
                "priority": "model_first",
                "max_attempts": 3,
            },
        },
        "settings": {
            "endpoint": "https://example.test/v1/chat/completions",
            "model_slots": {},
            "model_names": {
                "VOICE_DIRECTOR_PRIMARY_MODEL": "编导主模型",
            },
            "strategy": "primary_then_backup",
            "max_attempts": 2,
            "timeout_seconds": 30,
        },
    })
    updated_group = next(item for item in updated["groups"] if item["id"] == "language-model")
    assert updated_group["api_failover"]["order"] == ["visual-guidance", "story-writing", "director-seed21"]
    updated_function = next(item for item in updated_group["functions"] if item["id"] == "writing.case-rewrite")
    assert updated_function["failover"]["priority"] == "model_first"
    assert "max_attempts" not in updated_function["failover"]
    assert [item["api_id"] for item in updated_function["failover"]["plan"]] == [
        "visual-guidance", "story-writing", "director-seed21",
        "visual-guidance", "visual-guidance",
    ]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["api_groups"]["language-model"]["order"] == ["visual-guidance", "story-writing", "director-seed21"]
    assert "priority" not in saved["api_groups"]["language-model"]
    assert saved["api_functions"]["language-model"]["writing.case-rewrite"] == {
        "priority": "model_first",
    }
    assert saved["groups"]["director-seed21"]["endpoint"] == "https://example.test/v1/chat/completions"
    assert saved["groups"]["director-seed21"]["model_names"] == {
        "VOICE_DIRECTOR_PRIMARY_MODEL": "编导主模型",
    }


def test_api_management_save_config_persists_non_secret_settings_and_applies_runtime(monkeypatch, tmp_path):
    config_path = tmp_path / "api_management.json"
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", config_path)
    monkeypatch.setenv("DIRECTORS_V2_API_KEY", "test-secret")
    monkeypatch.setenv("DIRECTORS_V2_API_URL", "https://before.example/v1/chat/completions")

    snapshot = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": "visual-guidance",
        "settings": {
            "endpoint": "https://example.test/v1/chat/completions?token=hidden",
            "primary_model": "visual-model",
            "strategy": "retry_same",
            "max_attempts": 4,
            "timeout_seconds": 45,
        },
    })

    group = next(item for item in snapshot["groups"] if item["id"] == "language-model")
    channel = next(item for item in group["channels"] if item["id"] == "visual-guidance")
    assert channel["endpoint"] == "https://example.test/v1/chat/completions"
    assert channel["primary_model"] == "visual-model"
    assert channel["strategy"] == "retry_same"
    assert os.environ["DIRECTORS_V2_API_URL"] == "https://example.test/v1/chat/completions"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "test-secret" not in json.dumps(saved, ensure_ascii=False)


def test_api_management_tos_uses_storage_fields_and_bucket_failover(monkeypatch, tmp_path):
    config_path = tmp_path / "api_management.json"
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", config_path)
    for env_name in (
        "ARK_TTS_TOS_ACCESS_KEY",
        "ARK_TTS_TOS_BACKUP_ACCESS_KEY",
        "ARK_TTS_TOS_BUCKET",
        "ARK_TTS_TOS_BACKUP_BUCKET",
        "ARK_TTS_TOS_REGION",
        "ARK_TTS_TOS_BACKUP_REGION",
    ):
        monkeypatch.delenv(env_name, raising=False)

    updated = console._api_management_save_config({
        "group_id": "tos",
        "channel_id": "audio-storage",
        "group_settings": {"api_failover": {"order": ["audio-storage"]}},
        "settings": {
            "endpoint": "https://tos.example.test",
            "region": "cn-test-1",
            "model_slots": {
                "ARK_TTS_TOS_BUCKET": "audio-main",
                "ARK_TTS_TOS_BACKUP_BUCKET": "audio-backup",
            },
            "model_order": ["ARK_TTS_TOS_BUCKET", "ARK_TTS_TOS_BACKUP_BUCKET"],
            "strategy": "primary_then_backup",
        },
    })

    group = next(item for item in updated["groups"] if item["id"] == "tos")
    channel = group["channels"][0]
    assert channel["resource_kind"] == "object_storage"
    assert channel["region"] == "cn-test-1"
    assert channel["model_slot_title"] == "存储桶"
    assert [item["model_id"] for item in channel["model_slots"]] == ["audio-main", "audio-backup"]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["groups"]["audio-storage"]["region"] == "cn-test-1"
    assert saved["groups"]["audio-storage"]["model_order"] == [
        "ARK_TTS_TOS_BUCKET",
        "ARK_TTS_TOS_BACKUP_BUCKET",
    ]


def test_api_management_saves_channel_key_encrypted_and_never_returns_plaintext(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.delenv("DIRECTORS_V2_API_KEY", raising=False)

    snapshot = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": "visual-guidance",
        "settings": {
            "endpoint": "https://example.test/v1/chat/completions",
            "primary_model": "visual-model",
            "strategy": "retry_same",
            "max_attempts": 3,
            "timeout_seconds": 30,
        },
        "secrets": {"primary_api_key": "redacted-channel-key"},
    })

    channel = next(
        item for item in next(group for group in snapshot["groups"] if group["id"] == "language-model")["channels"]
        if item["id"] == "visual-guidance"
    )
    assert channel["secret_status"]["primary_api_key"] is True
    assert os.environ["DIRECTORS_V2_API_KEY"] == "redacted-channel-key"
    encrypted = (tmp_path / "api_management_secrets.bin").read_bytes()
    assert b"redacted-channel-key" not in encrypted
    assert "redacted-channel-key" not in json.dumps(snapshot, ensure_ascii=False)


def test_api_management_adds_multi_level_fallback_keys_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", tmp_path / "api_management.json")
    monkeypatch.setattr(console, "API_MANAGEMENT_SECRET_PATH", tmp_path / "api_management_secrets.bin")
    monkeypatch.delenv("API_MANAGEMENT_EXTRA_KEYS_STORY_WRITING", raising=False)

    snapshot = console._api_management_save_config({
        "group_id": "language-model",
        "channel_id": "story-writing",
        "settings": {
            "endpoint": "https://example.test/v1/responses",
            "primary_model": "story-model",
            "strategy": "primary_then_backup",
            "max_attempts": 2,
            "timeout_seconds": 60,
        },
        "secrets": {
            "extra_api_keys": ["level-3-key", "level-4-key", "level-5-key"],
        },
    })

    channel = next(
        item for item in next(group for group in snapshot["groups"] if group["id"] == "language-model")["channels"]
        if item["id"] == "story-writing"
    )
    assert channel["extra_key_supported"] is True
    assert channel["extra_key_count"] == 3
    assert json.loads(os.environ["API_MANAGEMENT_EXTRA_KEYS_STORY_WRITING"]) == [
        "level-3-key", "level-4-key", "level-5-key"
    ]
    encrypted = (tmp_path / "api_management_secrets.bin").read_bytes()
    assert all(value.encode() not in encrypted for value in ("level-3-key", "level-4-key", "level-5-key"))
    assert all(value not in json.dumps(snapshot, ensure_ascii=False) for value in ("level-3-key", "level-4-key", "level-5-key"))


def test_api_management_page_is_registered_and_opens_real_config_panel():
    page = console._api_management_page().decode("utf-8")

    assert console.PAGES["/api-management"] is console._api_management_page
    assert "/api/api-management" in page
    assert "/api/api-management/config" in page
    assert "/api/api-management/route-preview?node_id=" in page
    assert "apiAuditPlan" in page
    assert "查看执行计划" in page
    assert "API 管理" in page
    assert "凭据不回显" in page
    assert "待配置 API" in page
    assert "按 API 类型管理" in page
    assert "apiConfigModal" in page
    assert "apiConfigChannelPicker" not in page
    assert "apiConfigApiList" not in page
    assert "apiConfigChannel" not in page
    assert "apiConfigApiFailover" not in page
    assert "apiConfigApiOrder" not in page
    assert "data-api-channel-select" not in page
    assert "api-failover-panel" not in page
    assert "apiConfigFunctionFailover" in page
    assert "apiConfigFunctionList" in page
    assert "data-api-function-model-first" in page
    assert "data-api-function-priority" not in page
    assert "api-failover-schemes" not in page
    assert "api-business-selection" in page
    assert "方案一：API 优先" not in page
    assert "方案二：模型优先" not in page
    assert "业务选择降级方案" in page
    assert "data-api-function-attempts" not in page
    assert "function_settings" in page
    assert "apiConfigApiPriority" not in page
    assert "apiConfigApiPlan" not in page
    assert "group_settings" in page
    assert "API 与模型调用顺序" not in page
    assert "业务降级配置" in page
    assert "API 凭据集合" in page
    assert "一套 API 凭据 → 多个接入点" in page
    assert "共用当前 API 凭据" in page
    assert "api-provider-group" in page
    assert "API 与接入点配置组" in page
    assert "apiConfigAddChannel" in page
    assert "data-api-add-channel" in page
    assert "apiProviderConfigCards" in page
    assert "data-api-config-card" in page
    assert "pending_config" in page
    assert "saveOrder" in page
    assert "当前调用通道" not in page
    assert "model_slots" in page
    assert "model_order" in page
    assert "model_name" in page
    assert "usage_label" in page
    assert "模型名称" in page
    assert "模型 ID" in page
    assert "data-api-model-name-source" in page
    assert "data-api-model-id-source" in page
    assert "节点用途" in page
    assert "所属模块：" in page
    assert "调用用途：" in page
    assert "channel.nodes" in page
    assert "data-api-config-secrets" in page
    assert "primary_api_key" in page
    assert "data-api-add-secret" not in page
    assert "extra_api_keys" not in page
    assert "data-api-add-model" in page
    assert "data-api-remove-model" in page
    assert "data-api-channel-move" in page
    assert "data-api-channel-delete" in page
    assert "删除 API 配置组" in page
    assert "data-api-credential-name" not in page
    assert "API 名称" not in page
    assert "channelDisplayLabel" in page
    assert "data-api-model-move" in page
    assert "API 配置组内卡片从上到下决定凭据集合之间的降级顺序" in page
    assert "同一套 API 下的接入点按列表顺序逐级尝试" in page
    assert "data-api-channel-confirm" in page
    assert "保存这套 API 配置并折叠当前框体" in page
    assert "saveAllChannels" in page
    assert "全部保存中" in page
    assert "resource_kind" in page
    assert "apiRegion_" in page
    assert "存储桶降级策略" in page
    assert "一套 TOS 凭据 → 主桶 / 备用桶" in page
    assert "备用 Access Key" in page
    assert "自定义模型" in page
    assert "data-api-card" in page
    assert "配置此 API 类型" in page
    assert "api-card-details" not in page
    assert "bindCardInteractions" not in page


def test_api_management_uses_top_gear_instead_of_process_navigation_slot():
    page = console._layout("writing", "文案创作", "<p>content</p>").decode("utf-8")

    assert 'href="/api-management" aria-label="API 管理"' in page
    assert "settings-link" in page
    assert '>4<\u002fspan>API 管理' not in page
    assert 'href="/api-management" class="nav"' not in page


def test_layout_exposes_fifth_parallel_video_production_tab():
    page = console._layout("production", "视频制作", "<p>content</p>").decode("utf-8")

    assert page.count('class="nav ') == 6
    assert 'href="/production"' in page
    assert '>视频制作<' in page
    assert "唤醒服务、依赖预检与制作入口" in page
    assert ".side{display:grid;grid-template-columns:repeat(6,minmax(0,1fr))" in page


def test_video_production_page_registers_preflight_gate_without_fake_executor():
    page = console._video_production_page().decode("utf-8")

    assert console.PAGES["/production"] is console._video_production_page
    assert console.PAGES["/video-production"] is console._video_production_page
    assert 'id="wakeProduction"' in page
    assert 'id="startVideoProduction"' in page
    assert 'id="jianyingExePath"' in page
    assert 'id="detectJianying"' in page
    assert "/api/video-production/preflight?" in page
    assert "/api/video-production/jianying-paths" in page
    assert "/api/video-production/wake" in page
    assert "/api/video-production/tasks" in page
    assert "最终视频制作 transport 尚未接入" in page
    assert page.count("<script") == page.count("</script>")


def test_video_production_preflight_blocks_without_confirmed_editing_task():
    result = console._video_production_preflight("")

    assert result["status"] == "blocked"
    assert result["can_start"] is False
    assert any(item["id"] == "editing_task" and item["state"] == "blocked" for item in result["checks"])
    assert any(item["id"] == "video_executor" and item["state"] == "blocked" for item in result["checks"])


def test_video_production_preflight_uses_selected_jianying_exe_path(tmp_path):
    executable = tmp_path / "JianyingPro.exe"
    executable.write_bytes(b"test")

    result = console._video_production_preflight("", str(executable))

    check = next(item for item in result["checks"] if item["id"] == "jianying_exe")
    assert check["state"] == "ready"
    assert result["jianying_exe_path"] == str(executable.resolve())
    assert check["path"] == str(executable.resolve())


def test_jianying_path_candidates_report_existing_executables(monkeypatch, tmp_path):
    executable = tmp_path / "JianyingPro.exe"
    executable.write_bytes(b"test")
    monkeypatch.setattr(console, "JIANYING_EXE_PATH", executable)

    snapshot = console._jianying_exe_candidates_snapshot()

    assert snapshot["status"] == "ready"
    assert snapshot["selected"] == str(executable.resolve())
    assert any(item["path"] == str(executable.resolve()) and item["exists"] for item in snapshot["candidates"])


def test_wake_capcut_mate_prefers_pythonw_and_hides_console_window(monkeypatch, tmp_path):
    root = tmp_path / "capcut-mate"
    scripts = root / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    python_path = scripts / "python.exe"
    pythonw_path = scripts / "pythonw.exe"
    main_path = root / "main.py"
    python_path.write_text("", encoding="utf-8")
    pythonw_path.write_text("", encoding="utf-8")
    main_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(console, "CAPCUT_MATE_ROOT", root)
    probe_states = iter(("blocked", "ready"))
    monkeypatch.setattr(console, "_probe_capcut_mate", lambda: {"state": next(probe_states)})
    monkeypatch.setattr(console.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        console,
        "_video_production_preflight",
        lambda task_id, jianying_exe_path=None, confirmed_draft_path=None: {"status": "blocked", "can_start": False, "checks": [], "editing_task_id": task_id},
    )
    calls = []
    monkeypatch.setattr(console.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = console._wake_capcut_mate("editing-task")

    assert calls[0][0][0][0] == str(pythonw_path)
    assert calls[0][0][0][1] == str(main_path)
    assert calls[0][1]["creationflags"] & getattr(console.subprocess, "CREATE_NO_WINDOW", 0)
    assert calls[0][1]["startupinfo"] is not None
    assert "后台唤醒" in result["wake_message"]


def test_director_page_has_balanced_script_tags():
    page = console._director_page().decode("utf-8")

    assert page.count("<script") == page.count("</script>")
    assert "页面切换恢复器" in page


def test_style_task_reports_a_terminal_failure_without_network_access(monkeypatch, tmp_path):
    # 该测试验证任务状态机，不重复启动真实独立 worker；独立 worker 另有
    # process metadata/快照测试覆盖。
    monkeypatch.setattr(
        console,
        "_start_style_worker",
        lambda task_id: {"kind": "style_collection_worker", "task_id": task_id, "pid": 1230, "state": "running"},
    )
    monkeypatch.setattr(console, "STYLE_TASK_DIR", tmp_path / "tasks")
    task = console._create_style_task(
        {"creator_home_url": "not-a-supported-homepage", "sample_target": 30}
    )
    console._run_style_task(task["task_id"], {"creator_home_url": "not-a-supported-homepage", "sample_target": 30})
    for _ in range(20):
        snapshot = console._task_snapshot(task["task_id"])
        if snapshot and snapshot["state"] == "failed":
            break
        time.sleep(0.05)
    assert snapshot is not None
    assert snapshot["state"] == "failed"
    assert snapshot["stage"] == "collection_blocked"
    assert snapshot["progress"] == 5


def test_style_collection_worker_runs_as_a_separate_process(tmp_path):
    local_app_data = tmp_path / "local-app-data"
    task_dir = local_app_data / "VideoProductionConsole" / "tasks"
    task_dir.mkdir(parents=True)
    task_id = "real-independent-worker"
    (task_dir / f"{task_id}.json").write_text(
        json.dumps({
            "task_id": task_id,
            "task_type": "style_collection",
            "input": {"creator_home_url": "not-a-supported-homepage", "sample_target": 30},
            "state": "queued",
            "stage": "queued",
            "progress": 0,
            "message": "任务正在排队",
            "created_at": 1,
            "updated_at": 1,
        }),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local_app_data)
    environment["PYTHONPATH"] = str(console.ROOT / "src")

    completed = subprocess.run(
        [sys.executable, str(console.STYLE_WORKER_ENTRY), "--task-id", task_id],
        cwd=str(console.ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    saved = json.loads((task_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    assert completed.returncode == 0, completed.stderr
    assert saved["state"] == "failed"
    assert saved["stage"] == "collection_blocked"
    assert saved["worker"]["state"] == "exited"
    assert saved["worker"]["final_state"] == "failed"
    assert int(saved["worker"]["pid"]) != os.getpid()


def test_platform_detection_accepts_douyin_share_text_without_expanding_short_link():
    shared_text = "长按复制此条消息，打开抖音搜索，查看 TA 的更多作品。 https://v.douyin.com/LXcmxpG28jQ/"
    assert console._platform_from_url(shared_text) == "douyin"


def test_partial_douyin_collection_with_enough_samples_continues_to_style_package(tmp_path, monkeypatch):
    store = StylePackageStore(tmp_path / "style-packages")
    items = [
        {"video_id": str(index), "transcript": "有效转写样本。" * 20, "source_url": f"https://www.douyin.com/video/{index}"}
        for index in range(30)
    ]
    report = {
        "collection_status": "partial",
        "platform": "douyin",
        "creator_url": "https://www.douyin.com/user/MS4wLjABAAAA",
        "creator_id": "MS4wLjABAAAA",
        "creator_name": "部分采集博主",
        "transcript_ready_count": 30,
        "items": items,
        "warnings": ["超时后保留部分结果"],
        "diagnostics": {"fetch_status": "partial_timeout", "partial_result": True},
    }
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    monkeypatch.setattr(console, "collect_douyin_creator", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        console,
        "distill_style_package",
        lambda **kwargs: ({"profile_name": "待审核风格", "overview": "部分采集测试"}, items),
    )

    status, result = console._create_style_package(
        {"creator_home_url": report["creator_url"], "sample_target": 30}
    )

    # A newly saved style package waits for explicit user review, so the
    # creation endpoint intentionally returns HTTP 201 rather than 200.
    assert status == 201
    assert result["report"]["collection_status"] == "partial"
    assert result["record"]["sample_count"] == 30


def test_partial_douyin_collection_below_reference_target_still_distills(tmp_path, monkeypatch):
    store = StylePackageStore(tmp_path / "style-packages")
    items = [
        {"video_id": str(index), "transcript": "现有有效转写样本。" * 20, "source_url": f"https://www.douyin.com/video/{index}"}
        for index in range(16)
    ]
    report = {
        "collection_status": "completed",
        "platform": "douyin",
        "creator_url": "https://www.douyin.com/user/MS4wLjABAAAA",
        "creator_id": "MS4wLjABAAAA",
        "creator_name": "少量样本博主",
        "transcript_ready_count": 16,
        "items": items,
        "warnings": ["部分作品超过 10 分钟，已跳过"],
        "diagnostics": {},
    }
    captured = {}
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    monkeypatch.setattr(console, "collect_douyin_creator", lambda *args, **kwargs: report)

    def fake_distill(**kwargs):
        captured.update(kwargs)
        return {"profile_name": "少量样本风格", "overview": "使用现有样本生成"}, items

    monkeypatch.setattr(console, "distill_style_package", fake_distill)

    status, result = console._create_style_package(
        {"creator_home_url": report["creator_url"], "sample_target": 30}
    )

    assert status == 201
    assert result["record"]["sample_count"] == 16
    assert result["report"]["sample_target_reached"] is False
    assert result["report"]["sample_target_policy"] == "soft_cap"
    assert captured["allow_partial_samples"] is True
    assert captured["fallback_profile_name"] == "少量样本博主"
    assert captured["fallback_overview"]


def test_bgm_merge_keeps_all_generated_segments_and_transitions():
    script_result = {
        "director_lock": {
            "groups": [
                {"group_id": "g01", "timeline": {"start_us": 0, "end_us": 42_000_000}, "segment_text": "第一段"},
                {"group_id": "g02", "timeline": {"start_us": 42_000_000, "end_us": 90_000_000}, "segment_text": "第二段"},
            ]
        }
    }
    batch = {
        "bgm_timelines": [
            {"start": 0, "end": 42_000_000},
            {"start": 42_000_000, "end": 90_000_000},
        ],
        "transition_schemes": ["crossfade"],
    }
    params = console._build_bgm_merge_params(
        script_result,
        ["https://example.test/bgm-1.mp3", "https://example.test/bgm-2.mp3"],
        source_type="generated",
        generated_batch=batch,
    )
    assert params["audio_urls"] == ["https://example.test/bgm-1.mp3", "https://example.test/bgm-2.mp3"]
    assert params["timelines"] == batch["bgm_timelines"]
    assert params["transition_schemes"] == ["crossfade"]


def test_task_snapshot_falls_back_to_persisted_file(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    task_id = "persisted-task"
    console._persist_task({"task_id": task_id, "state": "interrupted", "updated_at": 1})
    assert console._task_snapshot(task_id) == {"task_id": task_id, "state": "interrupted", "updated_at": 1}


def test_set_task_rehydrates_persisted_task_before_updating(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task_id = "retry-after-restart"
    console._persist_task({"task_id": task_id, "state": "failed", "updated_at": 1})

    console._set_task(task_id, state="running", message="正在补跑失败视频")

    saved = console._load_persisted_task(task_id)
    assert saved is not None
    assert saved["state"] == "running"
    assert saved["message"] == "正在补跑失败视频"


def test_external_style_worker_snapshot_refreshes_from_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    task_id = "external-style-worker"
    worker = {
        "kind": "style_collection_worker",
        "task_id": task_id,
        "pid": 1236,
        "state": "running",
    }
    in_memory = {"task_id": task_id, "task_type": "style_collection", "state": "running", "worker": worker, "updated_at": 1}
    console._persist_task(in_memory)
    monkeypatch.setattr(console, "STYLE_TASKS", {task_id: dict(in_memory)})
    console._persist_task({**in_memory, "state": "succeeded", "stage": "completed", "updated_at": 2})

    snapshot = console._task_snapshot(task_id)

    assert snapshot is not None
    assert snapshot["state"] == "succeeded"


def test_mark_orphaned_tasks_keeps_verified_style_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    task_id = "active-style-worker"
    task = {
        "task_id": task_id,
        "task_type": "style_collection",
        "state": "running",
        "worker": {"kind": "style_collection_worker", "task_id": task_id, "pid": 1237, "state": "running"},
        "updated_at": 1,
    }
    console._persist_task(task)
    command = f"python {console.STYLE_WORKER_ENTRY} --task-id {task_id}"
    monkeypatch.setattr(console, "process_snapshot", lambda: {1237: {"command_line": command}})

    console._mark_orphaned_tasks()

    assert console._load_persisted_task(task_id)["state"] == "running"


def test_mark_orphaned_tasks_marks_missing_style_worker_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    task_id = "missing-style-worker"
    console._persist_task({
        "task_id": task_id,
        "task_type": "style_collection",
        "state": "running",
        "worker": {"kind": "style_collection_worker", "task_id": task_id, "pid": 1238, "state": "running"},
        "updated_at": 1,
    })
    monkeypatch.setattr(console, "process_snapshot", lambda: {9999: {"command_line": "other-process"}})

    console._mark_orphaned_tasks()

    saved = console._load_persisted_task(task_id)
    assert saved is not None
    assert saved["state"] == "interrupted"
    assert saved["stage"] == "style_worker_exited"


def test_mark_orphaned_tasks_uses_fresh_heartbeat_when_process_listing_omits_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path) / "tasks")
    monkeypatch.setattr(console, "STYLE_WORKER_HEARTBEAT_DIR", Path(tmp_path) / "workers")
    task_id = "heartbeat-process-omits-pid"
    console._persist_task({
        "task_id": task_id,
        "task_type": "style_collection",
        "state": "running",
        "worker": {"kind": "style_collection_worker", "task_id": task_id, "pid": 1242, "state": "running"},
        "updated_at": 1,
    })
    workers = Path(tmp_path) / "workers"
    workers.mkdir(parents=True)
    (workers / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "pid": 1242, "state": "running", "heartbeat_unix": time.time()}),
        encoding="utf-8",
    )
    # 进程表可读，但受限枚举漏掉真实 worker PID。
    monkeypatch.setattr(console, "process_snapshot", lambda: {9999: {"pid": 9999, "name": "python", "command_line": ""}})

    console._mark_orphaned_tasks()

    assert console._load_persisted_task(task_id)["state"] == "running"


def test_mark_orphaned_tasks_uses_recent_worker_heartbeat_when_process_table_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path) / "tasks")
    monkeypatch.setattr(console, "STYLE_WORKER_HEARTBEAT_DIR", Path(tmp_path) / "workers")
    task_id = "heartbeat-style-worker"
    console._persist_task({
        "task_id": task_id,
        "task_type": "style_collection",
        "state": "running",
        "worker": {"kind": "style_collection_worker", "task_id": task_id, "pid": 1240, "state": "running"},
        "updated_at": 1,
    })
    (Path(tmp_path) / "workers").mkdir(parents=True)
    (Path(tmp_path) / "workers" / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "pid": 1240, "state": "running", "heartbeat_unix": time.time()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(console, "process_snapshot", lambda: {})

    console._mark_orphaned_tasks()

    assert console._load_persisted_task(task_id)["state"] == "running"


def test_mark_orphaned_tasks_uses_heartbeat_when_process_listing_omits_command_line(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path) / "tasks")
    monkeypatch.setattr(console, "STYLE_WORKER_HEARTBEAT_DIR", Path(tmp_path) / "workers")
    task_id = "heartbeat-without-command-line"
    console._persist_task({
        "task_id": task_id,
        "task_type": "style_collection",
        "state": "running",
        "worker": {"kind": "style_collection_worker", "task_id": task_id, "pid": 1241, "state": "running"},
        "updated_at": 1,
    })
    (Path(tmp_path) / "workers").mkdir(parents=True)
    (Path(tmp_path) / "workers" / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "pid": 1241, "state": "running", "heartbeat_unix": time.time()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(console, "process_snapshot", lambda: {1241: {"pid": 1241, "name": "python", "command_line": ""}})

    console._mark_orphaned_tasks()

    assert console._load_persisted_task(task_id)["state"] == "running"


def test_start_style_worker_records_real_pid_from_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path) / "tasks")
    monkeypatch.setattr(console, "STYLE_WORKER_HEARTBEAT_DIR", Path(tmp_path) / "workers")
    monkeypatch.setattr(console, "TASK_RUNTIME_DIR", Path(tmp_path) / "runtime")
    monkeypatch.setattr(
        console,
        "SERVICE_SPEC",
        type("ServiceSpec", (), {"worktree_root": str(tmp_path)})(),
    )
    console.STYLE_WORKER_HEARTBEAT_DIR.mkdir(parents=True)
    task_id = "launcher-pid-diff"
    (console.STYLE_WORKER_HEARTBEAT_DIR / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "pid": 2222, "state": "running"}),
        encoding="utf-8",
    )

    class FakeProcess:
        pid = 1111

    monkeypatch.setattr(console.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    metadata = console._start_style_worker(task_id)

    assert metadata["pid"] == 2222
    assert metadata["launcher_pid"] == 1111


def test_style_task_resume_reuses_persisted_checkpoint_after_executor_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task_id = "style-resume-after-restart"
    task = {
        "task_id": task_id,
        "task_type": "style_collection",
        "input": {
            "creator_home_url": "https://www.douyin.com/user/example",
            "creator_name": "测试博主",
            "sample_target": 50,
        },
        "state": "interrupted",
        "stage": "executor_restarted",
        "progress": 42,
        "updated_at": 1,
        "result": {
            "checkpoint": {
                "candidates": [{"video_id": "1", "video_url": "https://cdn.example.com/1.mp4"}],
                "processed_count": 1,
                "transcripts": {"1": "已经完成的转写"},
            }
        },
    }
    console._persist_task(task)
    calls = []
    monkeypatch.setattr(
        console,
        "_start_style_worker",
        lambda value: calls.append(value) or {
            "kind": "style_collection_worker",
            "task_id": value,
            "pid": 1234,
            "state": "running",
        },
    )

    resumed = console._resume_style_task(task_id)

    assert resumed["state"] == "queued"
    assert resumed["stage"] == "resume_queued"
    assert calls == [task_id]
    assert resumed["worker"]["pid"] == 1234


def test_style_task_resume_rejects_manual_stop_or_missing_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    console._persist_task({
        "task_id": "manual-style-stop",
        "task_type": "style_collection",
        "input": {"creator_home_url": "https://www.douyin.com/user/example", "sample_target": 50},
        "state": "interrupted",
        "stage": "user_stopped",
        "updated_at": 1,
        "result": {"checkpoint": {"candidates": [{"video_id": "1"}]}},
    })

    with pytest.raises(ValueError, match="仅服务意外退出"):
        console._resume_style_task("manual-style-stop")


def test_legacy_interrupted_style_task_can_restart_with_current_form_input(tmp_path, monkeypatch):
    """旧版本没有保存输入和断点时，继续按钮也必须有明确可执行的恢复路径。"""
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task_id = "legacy-style-resume"
    console._persist_task({
        "task_id": task_id,
        "state": "interrupted",
        "stage": "executor_restarted",
        "progress": 43,
        "updated_at": 1,
    })
    calls = []
    monkeypatch.setattr(
        console,
        "_start_style_worker",
        lambda value: calls.append(value) or {
            "kind": "style_collection_worker",
            "task_id": value,
            "pid": 1235,
            "state": "running",
        },
    )

    resumed = console._resume_style_task(
        task_id,
        {"creator_home_url": "https://www.douyin.com/user/example", "sample_target": 50},
    )

    assert resumed["task_type"] == "style_collection"
    assert resumed["state"] == "queued"
    assert "重新采集" in resumed["message"]
    assert calls == [task_id]
    assert resumed["worker"]["pid"] == 1235


def test_legacy_interrupted_style_task_without_input_reports_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task_id = "legacy-style-without-input"
    console._persist_task({
        "task_id": task_id,
        "state": "interrupted",
        "stage": "executor_restarted",
        "progress": 43,
        "updated_at": 1,
    })

    with pytest.raises(ValueError, match="请先粘贴达人主页链接"):
        console._resume_style_task(task_id)

    assert console._load_persisted_task(task_id)["state"] == "interrupted"


def test_style_task_resume_api_and_writing_page_offer_the_real_resume_path(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task_id = "style-resume-api"
    console._persist_task({
        "task_id": task_id,
        "task_type": "style_collection",
        "input": {
            "creator_home_url": "https://www.douyin.com/user/example",
            "creator_name": "测试博主",
            "sample_target": 50,
        },
        "state": "interrupted",
        "stage": "executor_restarted",
        "progress": 40,
        "updated_at": 1,
        "result": {"checkpoint": {"candidates": [{"video_id": "1"}], "processed_count": 1}},
    })
    calls = []

    monkeypatch.setattr(console, "_run_style_task", lambda *args: calls.append(args))
    monkeypatch.setattr(
        console,
        "_start_style_worker",
        lambda value: {
            "kind": "style_collection_worker",
            "task_id": value,
            "pid": 1239,
            "state": "running",
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/style-tasks/{task_id}/resume",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["state"] == "queued"
        assert not calls
        assert payload["worker"]["pid"] == 1239
        page = console._writing_page_live().decode("utf-8")
        assert 'id="resumeStyleTask"' in page
        assert "/resume" in page
        assert "restart_without_checkpoint" in page
        assert "请先粘贴达人主页链接" in page
        assert "sample_insufficient" in page
        assert "distillation_failed" in page
        assert "使用已有样本生成" in page
        assert "采集上限（参考）" in page
    finally:
        server.shutdown()
        server.server_close()


def test_style_task_partial_sample_retry_reuses_saved_report(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task_id = "partial-sample-retry"
    report = {
        "creator_url": "https://www.douyin.com/user/example",
        "creator_name": "少量样本博主",
        "collection_status": "completed",
        "transcript_ready_count": 16,
        "items": [{"video_id": str(index), "transcript": "有效样本。" * 20} for index in range(16)],
    }
    console._persist_task({
        "task_id": task_id,
        "task_type": "style_collection",
        "input": {"creator_home_url": report["creator_url"], "sample_target": 30},
        "state": "failed",
        "stage": "sample_insufficient",
        "progress": 72,
        "result": {"collection_id": "collection_partial", "report": report, "checkpoint": {"processed_count": 16}},
    })
    monkeypatch.setattr(
        console,
        "_start_style_worker",
        lambda value: {"kind": "style_collection_worker", "task_id": value, "pid": 1241, "state": "running"},
    )

    resumed = console._resume_style_task(task_id)

    assert resumed["state"] == "queued"
    assert resumed["stage"] == "partial_distill_queued"
    assert resumed["message"] == "已保留 16 条有效样本，直接继续蒸馏，不重新采集"
    checkpoint = resumed["result"]["checkpoint"]
    assert checkpoint["precollected_collection_id"] == "collection_partial"
    assert checkpoint["precollected_report"]["transcript_ready_count"] == 16


def test_style_task_distillation_failure_reuses_saved_report(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task_id = "distillation-failure-retry"
    report = {
        "creator_url": "https://www.douyin.com/user/example",
        "creator_name": "格式失败博主",
        "collection_status": "partial",
        "transcript_ready_count": 16,
        "items": [{"video_id": str(index), "transcript": "有效样本。" * 20} for index in range(16)],
    }
    console._persist_task({
        "task_id": task_id,
        "task_type": "style_collection",
        "input": {"creator_home_url": report["creator_url"], "sample_target": 30},
        "state": "failed",
        "stage": "distillation_failed",
        "message": "风格包缺少 profile_name",
        "progress": 80,
        "result": {"collection_id": "collection_distill_failed", "report": report},
    })
    monkeypatch.setattr(
        console,
        "_start_style_worker",
        lambda value: {"kind": "style_collection_worker", "task_id": value, "pid": 1242, "state": "running"},
    )

    resumed = console._resume_style_task(task_id)

    assert resumed["state"] == "queued"
    assert resumed["stage"] == "partial_distill_queued"
    assert resumed["result"]["checkpoint"]["precollected_collection_id"] == "collection_distill_failed"
    assert resumed["result"]["checkpoint"]["precollected_report"]["transcript_ready_count"] == 16


def test_interrupted_style_task_with_saved_report_uses_partial_distill_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task_id = "interrupted-partial-distill-retry"
    report = {
        "creator_url": "https://www.douyin.com/user/example",
        "creator_name": "中断后重试博主",
        "collection_status": "partial",
        "transcript_ready_count": 16,
        "items": [{"video_id": str(index), "transcript": "有效样本。" * 20} for index in range(16)],
    }
    console._persist_task({
        "task_id": task_id,
        "task_type": "style_collection",
        "input": {"creator_home_url": report["creator_url"], "sample_target": 30},
        "state": "interrupted",
        "stage": "style_worker_exited",
        "progress": 80,
        "result": {"checkpoint": {"precollected_report": report, "precollected_collection_id": "collection_saved"}},
    })
    monkeypatch.setattr(
        console,
        "_start_style_worker",
        lambda value: {"kind": "style_collection_worker", "task_id": value, "pid": 1243, "state": "running"},
    )

    resumed = console._resume_style_task(task_id)

    assert resumed["state"] == "queued"
    assert resumed["stage"] == "partial_distill_queued"
    assert resumed["message"] == "已保留 16 条有效样本，直接继续蒸馏，不重新采集"


def test_writing_page_keeps_progress_ring_for_resumable_interruption():
    page = console._writing_page_live_with_selection_guard().decode("utf-8")

    assert "style-task-ring" in page
    assert "startWatchingTask" in page
    assert "watchedTaskIds" in page
    assert "nativeFetch" in page
    assert "resilientWatchStyleTask" in page
    assert "页面与后台连接暂时中断，正在自动重试" in page
    assert "ensureStyleAddCard" in page
    assert "hasPendingProfile" in page
    assert "staleRetryable" in page
    assert "taskRenderKey" in page
    assert "MutationObserver" in page
    assert "style_worker_exited" in page
    assert "蒸馏已中断，可恢复" in page
    assert "当前进度 '+value+'%，可恢复" in page
    assert "style-card-approve" in page
    assert "确认当前风格包" in page
    assert "expression_habit_library" in page
    assert "style-habit-item" in page


def test_writing_page_exposes_confirmed_archivable_style_profile_delete():
    page = console._writing_page_live_with_selection_guard().decode("utf-8")

    assert "style-card-delete" in page
    assert "/api/style-packages/" in page and "/delete" in page
    assert "原文件、采集记录和历史文案会归档保留" in page
    assert "再次点击确认" in page
    assert "document.addEventListener('click'" in page
    assert "if(typeof loadProfiles==='function')await loadProfiles();" in page


def test_writing_style_cards_preserve_independent_rule_state_across_refreshes():
    page = console._writing_page_live_with_selection_guard().decode("utf-8")

    assert "style-card-detail-toggle" in page
    assert "style-card-details" in page
    assert "captureExpandedCards" in page
    assert "restoreExpandedCards" in page
    assert "window.__writingHasStyleProfile===true" in page
    assert "style-rules-modal" in page
    assert "style-rules-dialog-close" in page
    assert "openRulesModal" in page
    assert "closeRulesModal();" in page
    assert "activeRulesModal" in page


def test_writing_page_exposes_recent_style_package_marker():
    page = console._writing_page_live_with_selection_guard().decode("utf-8")

    assert "/api/style-packages" in page
    assert "last_used_at" in page
    assert "最近使用" in page


def test_writing_style_package_cards_wrap_without_horizontal_scroller():
    page = console._writing_page_live_with_selection_guard().decode("utf-8")

    assert ".style-card-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr))" in page
    assert ".style-card-list{display:flex;gap:12px;overflow-x:auto" not in page


def test_style_profile_delete_removes_only_active_profile_and_keeps_archive(tmp_path, monkeypatch):
    store = StylePackageStore(tmp_path / "style-packages")
    collection_id = store.save_collection({"platform": "douyin", "items": []})
    record = store.save_profile(
        profile={"profile_name": "待删除风格", "overview": "说明"},
        creator_url="https://www.douyin.com/user/example",
        collection_id=collection_id,
        sample_count=30,
    )
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    monkeypatch.setattr(console, "STYLE_TASK_DIR", tmp_path / "tasks")
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/style-packages/{record['style_profile_id']}/delete",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        response = json.loads(urlopen(request).read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert response["status"] == "style_profile_archived"
    assert store.list_profiles() == []
    assert list((tmp_path / "style-packages" / "archived_profiles").glob("*/" + record["style_profile_id"] + ".json"))


def test_archived_story_is_available_to_editing_handoff_without_reappearing_in_global_list(tmp_path, monkeypatch):
    story_id = "archived-story"
    archive_root = tmp_path / "archives" / "20260819_archived-story_test"
    (archive_root / "executor_tasks").mkdir(parents=True)
    (archive_root / "director_story_reviews" / story_id).mkdir(parents=True)
    (archive_root / "archive_manifest.json").write_text(
        json.dumps({
            "source_story_task_id": story_id,
            "archive_id": archive_root.name,
            "archived_at": 123,
            "status": "completed",
        }),
        encoding="utf-8",
    )
    old_review_path = console.DIRECTOR_REVIEW_DIR / story_id / "story_review.json"
    (archive_root / "executor_tasks" / f"{story_id}.json").write_text(
        json.dumps({
            "task_id": story_id,
            "task_type": "director_story_review",
            "state": "succeeded",
            "result": {"source_input": str(old_review_path)},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(console, "RUNTIME_ARCHIVE_DIR", tmp_path / "archives")
    monkeypatch.setattr(console, "STYLE_TASK_DIR", tmp_path / "active_tasks")
    monkeypatch.setattr(console, "STYLE_TASKS", {})

    recovered = console._handoff_story_task_snapshot(story_id)
    assert recovered is not None
    assert recovered["runtime_data_archived"] is True
    assert recovered["result"]["source_input"] == str(
        archive_root / "director_story_reviews" / story_id / "story_review.json"
    )
    assert console._list_style_tasks(limit=20) == []


def test_runtime_archive_keeps_failed_style_task_visible_for_recovery(tmp_path, monkeypatch):
    from types import SimpleNamespace

    task_id = "failed-style-task"
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    task = {
        "task_id": task_id,
        "task_type": "style_collection",
        "state": "failed",
        "stage": "distillation_failed",
        "progress": 80,
        "updated_at": 20,
        "result": {"checkpoint": {"candidates": [{"video_id": "sample-1"}]}},
    }
    (task_dir / f"{task_id}.json").write_text(json.dumps(task), encoding="utf-8")
    monkeypatch.setattr(console, "STYLE_TASK_DIR", task_dir)
    monkeypatch.setattr(console, "RUNTIME_ARCHIVE_DIR", tmp_path / "archives")
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path / "reviews")
    monkeypatch.setattr(
        console,
        "STYLE_PACKAGE_STORE",
        SimpleNamespace(rewrites_dir=tmp_path / "rewrites", collections_dir=tmp_path / "collections"),
    )
    monkeypatch.setattr(console, "STYLE_TASKS", {task_id: dict(task)})

    report = console._archive_runtime_data(
        source_story_task_id="story-1",
        completed_task_id="editing-1",
    )

    assert report["status"] == "completed"
    assert (task_dir / f"{task_id}.json").is_file()
    assert not list((tmp_path / "archives").glob("*/executor_tasks/*.json"))
    assert task_id in console.STYLE_TASKS


def test_stop_director_story_task_persists_terminal_interrupted_state(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task_id = "director-stop-test"
    console.STYLE_TASKS[task_id] = {
        "task_id": task_id,
        "task_type": "director_story_review",
        "state": "running",
        "stage": "tts_timeline",
        "progress": 40,
        "message": "正在调用 TTS",
        "created_at": 1,
        "updated_at": 1,
    }
    stopped = console._stop_director_story_task(task_id)

    assert stopped["state"] == "interrupted"
    assert stopped["stage"] == "user_stopped"
    assert stopped["progress"] == 40
    assert "手动停止" in stopped["message"]
    persisted = console._load_persisted_task(task_id)
    assert persisted is not None
    assert persisted["state"] == "interrupted"

    # 外部请求返回后的迟到状态更新不得把停止任务重新打开。
    console._set_task(task_id, state="succeeded", stage="review_ready", progress=100, message="迟到的成功")
    latest = console._task_snapshot(task_id)
    assert latest is not None
    assert latest["state"] == "interrupted"
    assert latest["stage"] == "user_stopped"
    assert latest["progress"] == 40
    assert console._stop_director_story_task(task_id)["state"] == "interrupted"


def test_background_update_does_not_reopen_terminal_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task_id = "parallel-branch-after-failure"
    console.STYLE_TASKS[task_id] = {
        "task_id": task_id,
        "state": "failed",
        "stage": "asset_production_failed",
        "progress": 0,
        "message": "首帧失败",
        "result": {"categories": {}},
    }
    console._set_task(
        task_id,
        state="running",
        stage="digital_humans",
        progress=35,
        message="数字人仍在查询",
        result={"categories": {"digital_humans": {"state": "running"}}},
        _preserve_terminal=True,
    )

    saved = console._task_snapshot(task_id)
    assert saved is not None
    assert saved["state"] == "failed"
    assert saved["stage"] == "asset_production_failed"
    assert saved["message"] == "首帧失败"
    assert saved["result"]["categories"]["digital_humans"]["state"] == "running"


def test_digital_human_cache_roundtrip_and_input_fingerprint(tmp_path):
    image = tmp_path / "digital.png"
    audio = tmp_path / "voice.mp3"
    image.write_bytes(b"image-v1")
    audio.write_bytes(b"audio-v1")
    shot = {
        "shot_id": "g05_s02",
        "group_id": "g05",
        "narration": "同一段旁白",
        "shot_content": "人物自然口播",
        "aspect_ratio": "9:16",
    }
    script = {"aspect_ratio": "9:16"}
    first_key = console._digital_human_cache_key(shot, script, image, audio)
    cache_path = tmp_path / "digital_human_asset_cache.json"
    console._save_digital_human_cache(
        cache_path,
        {
            "version": 1,
            "entries": {
                first_key: {
                    "shot_id": "g05_s02",
                    "task_id": "paid-task-1",
                    "status": "succeeded",
                    "video_path": str(tmp_path / "g05_s02.mp4"),
                }
            },
        },
    )

    restored = console._load_digital_human_cache(cache_path)
    assert restored["entries"][first_key]["task_id"] == "paid-task-1"

    audio.write_bytes(b"audio-v2")
    assert console._digital_human_cache_key(shot, script, image, audio) != first_key


def test_digital_human_auth_failure_uses_existing_grid_image_without_paid_retry(tmp_path, monkeypatch):
    review_root = tmp_path / "reviews"
    task_root = tmp_path / "tasks"
    review_root.mkdir()
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", review_root)
    monkeypatch.setattr(console, "STYLE_TASK_DIR", task_root)
    monkeypatch.setattr(console, "STYLE_TASKS", {})

    first_frame_a = review_root / "first_frame_a.jpg"
    first_frame_b = review_root / "first_frame_b.jpg"
    first_frame_a.write_bytes(b"image-a")
    first_frame_b.write_bytes(b"image-b")
    story_id = "story-auth-fallback"
    asset_task_id = "asset-auth-fallback"
    console.STYLE_TASKS[asset_task_id] = {
        "task_id": asset_task_id,
        "task_type": "asset_production",
        "source_story_task_id": story_id,
        "state": "running",
        "result": {
            "source_story_task_id": story_id,
            "first_frame_prompt_trace": [
                {"shot_id": "g01_s01", "prompt": "已有首帧"},
                {"shot_id": "g01_s03", "prompt": "已有首帧"},
            ],
            "first_frame_assets": {
                "image_paths": [str(first_frame_a), str(first_frame_b)],
                "grid_batches": [{
                    "grid_id": "first_frame_grid_001",
                    "cells": [{"shot_id": "g01_s01"}, {"shot_id": "g01_s03"}],
                }],
            },
            "digital_human_assets": {
                "shot_results": [{"shot_id": "g01_s02", "status": "failed", "error": "HTTP 401 unauthorized"}],
            },
            "digital_human_fallback_pending": {
                "shot_ids": ["g01_s02"],
                "reason": "RunningHub HTTP 401 unauthorized",
            },
            "categories": {"digital_humans": {"state": "running"}},
        },
    }
    monkeypatch.setattr(
        console,
        "_latest_shot_script_task",
        lambda _story_id: {"result": {"shot_video_script": [
            {"shot_id": "g01_s01", "media_type": "aigc_video"},
            {"shot_id": "g01_s02", "media_type": "digital_human_video"},
            {"shot_id": "g01_s03", "media_type": "static_image"},
        ]}},
    )

    applied = console._apply_pending_digital_human_grid_fallback(asset_task_id)

    assert applied["applied"] == 1
    saved = console._task_snapshot(asset_task_id)
    assert saved is not None
    result = saved["result"]
    assert result["media_type_overrides"]["g01_s02"] == "static_image"
    fallback = result["digital_human_fallbacks"]["g01_s02"]
    assert Path(fallback["image_path"]).is_file()
    assert fallback["source_grid_id"] == "first_frame_grid_001"
    assert fallback["selection_policy"] == "stable_random_existing_grid_cell"
    assert result["digital_human_assets"]["fallback_count"] == 1
    assert result["categories"]["digital_humans"]["state"] == "succeeded"
    assert "digital_human_fallback_pending" not in result
    assert console._asset_preview_path(asset_task_id, "g01_s02") == Path(fallback["image_path"]).resolve()
    assert console._is_digital_human_auth_error(RuntimeError("HTTP 401 unauthorized"))
    assert not console._is_digital_human_auth_error(RuntimeError("RunningHub queue limit"))


def test_asset_readiness_accepts_digital_human_grid_fallback(tmp_path, monkeypatch):
    review_root = tmp_path / "reviews"
    review_root.mkdir()
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", review_root)
    first_frame = review_root / "g01_s01.jpg"
    fallback_frame = review_root / "g01_s02.jpg"
    bgm = review_root / "bgm.mp3"
    for path in (first_frame, fallback_frame, bgm):
        path.write_bytes(b"ready")
    result = {
        "first_frame_prompt_trace": [{"shot_id": "g01_s01"}],
        "first_frame_assets": {"image_paths": [str(first_frame)]},
        "media_type_overrides": {"g01_s02": "static_image"},
        "digital_human_fallbacks": {"g01_s02": {"image_path": str(fallback_frame)}},
        "aigc_video_assets": {"shot_results": []},
        "digital_human_assets": {"shot_results": [{"shot_id": "g01_s02", "status": "fallback_image"}]},
        "bgm_assets": {"status": "succeeded", "audio_path": str(bgm)},
    }
    shots = [
        {"shot_id": "g01_s01", "media_type": "static_image"},
        {"shot_id": "g01_s02", "media_type": "digital_human_video"},
    ]

    ready, reason = console._asset_result_readiness(result, shots)

    assert ready, reason


def test_digital_human_audio_binds_to_small_shot_window_not_whole_group(tmp_path, monkeypatch):
    source = tmp_path / "group-tts.mp3"
    source.write_bytes(b"group-audio" * 256)
    shot = {
        "shot_id": "g01_s02",
        "group_id": "g01",
        "narration": "第二个小镜头对白",
        "timeline": {"start_us": 6_500_000, "end_us": 9_500_000},
    }
    narration = {
        "group_id": "g01",
        "local_path": str(source),
        "timeline": {"start_us": 1_000_000, "end_us": 12_000_000},
    }
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"shot-audio" * 256)
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(console, "_resolve_digital_audio_ffmpeg", lambda: "ffmpeg-test")
    monkeypatch.setattr(console.subprocess, "run", fake_run)

    output, window = console._slice_digital_human_audio(shot, narration, tmp_path / "segments")

    assert output.is_file()
    assert window["relative_start_us"] == 5_500_000
    assert window["duration_us"] == 3_000_000
    assert commands[0][commands[0].index("-ss") + 1] == "5.500000"
    assert commands[0][commands[0].index("-t") + 1] == "3.000000"

    invalid_shot = {**shot, "timeline": {"start_us": 11_000_000, "end_us": 13_000_000}}
    with pytest.raises(RuntimeError, match="超出 g01 剪映 STT 分段时间线"):
        console._slice_digital_human_audio(invalid_shot, narration, tmp_path / "segments")


def test_digital_human_audio_window_accepts_flat_locked_shot_script_shape():
    narration = {
        "group_id": "g01",
        "timeline": {"start_us": 0, "end_us": 15_120_000},
        "local_path": "C:/tts/voice_01.mp3",
    }
    shot = {
        "shot_id": "g01_s04",
        "group_id": "g01",
        "start_us": 11_715_497,
        "end_us": 15_120_000,
    }

    window = console._digital_human_audio_window(shot, narration)

    assert window["relative_start_us"] == 11_715_497
    assert window["duration_us"] == 3_404_503


def test_video_preview_uses_record_path_after_an_earlier_video_failure(tmp_path, monkeypatch):
    """失败记录不能让后续成功视频的预览下标整体错位。"""
    output_dir = tmp_path / "asset"
    video = output_dir / "videos" / "video_03.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path)
    monkeypatch.setattr(
        console,
        "_task_snapshot",
        lambda _task_id: {
            "result": {
                "aigc_video_assets": {
                    "shot_results": [
                        {"shot_id": "g01_s01", "status": "succeeded", "video_path": str(output_dir / "videos" / "video_01.mp4")},
                        {"shot_id": "g01_s02", "status": "failed", "video_path": ""},
                        {"shot_id": "g02_s01", "status": "succeeded", "video_path": str(video)},
                    ]
                }
            }
        },
    )

    assert console._asset_video_preview_path("asset-task", "g02_s01") == video.resolve()
    assert console._asset_video_preview_path("asset-task", "g01_s02") is None


def test_assets_page_prompts_before_reusing_older_story_data():
    page = console._assets_page().decode("utf-8")

    assert "newestStoryForSameRewrite" in page
    assert "askReuseChoice" in page
    assert "继续使用当前旧数据" in page
    assert "前往最新审核稿" in page


def test_story_scoped_task_listing_keeps_archived_paid_assets_outside_global_recent_window(tmp_path, monkeypatch):
    """当前故事的归档任务不能因无关任务超过全局最近 20 条而消失。"""
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    story_id = "story-with-paid-assets"
    console._persist_task({
        "task_id": "archived-paid-asset",
        "task_type": "asset_production",
        "source_story_task_id": story_id,
        "state": "failed",
        "updated_at": 1,
        "result": {
            "source_story_task_id": story_id,
            "aigc_video_assets": {"shot_results": [{"shot_id": "g01_s01", "status": "succeeded"}]},
        },
    })
    for index in range(25):
        console._persist_task({
            "task_id": f"unrelated-{index}",
            "task_type": "style_package",
            "state": "succeeded",
            "updated_at": 100 + index,
        })

    tasks = console._list_style_tasks(limit=20, source_story_task_id=story_id)

    assert [item["task_id"] for item in tasks] == ["archived-paid-asset"]


def test_story_scoped_task_listing_recovers_local_video_retry_after_executor_restart(tmp_path, monkeypatch):
    """素材页刷新走列表接口时，也必须恢复本机已完成的补跑视频。"""
    task_dir = tmp_path / "tasks"
    output_dir = tmp_path / "asset-production"
    retry_video = output_dir / "video_retries" / "retry-1" / "videos" / "video_01.mp4"
    retry_video.parent.mkdir(parents=True)
    retry_video.write_bytes(b"0" * 60_000)
    monkeypatch.setattr(console, "STYLE_TASK_DIR", task_dir)
    story_id = "story-restart-recovery"
    console._persist_task({
        "task_id": "asset-restart-recovery",
        "task_type": "asset_production",
        "source_story_task_id": story_id,
        "state": "failed",
        "stage": "asset_production_failed",
        "updated_at": 1,
        "result": {
            "source_story_task_id": story_id,
            "output_dir": str(output_dir),
            "aigc_video_assets": {
                "shot_results": [{"shot_id": "g02_s02", "status": "failed", "video_path": ""}],
            },
        },
    })

    tasks = console._list_style_tasks(limit=20, source_story_task_id=story_id)

    recovered = tasks[0]
    record = recovered["result"]["aigc_video_assets"]["shot_results"][0]
    assert recovered["state"] == "succeeded"
    assert recovered["stage"] == "asset_production_partial"
    assert recovered["result"]["categories"]["aigc_videos"]["state"] == "succeeded"
    assert record["status"] == "succeeded"
    assert record["recovered_from_local_file"] is True
    assert record["video_path"] == str(retry_video.resolve())


def test_director_page_locks_shot_script_to_the_current_story_task():
    page = console._director_page().decode("utf-8")

    assert "const chooseStory=tasks=>" in page
    assert "item.source_story_task_id===storyId" in page
    assert "api/style-tasks?summary=1&limit=20" in page
    assert "window.__directorDoneTaskId===done.task_id" in page
    assert "确认最终视频脚本并进入素材生产" in page
    assert "/assets" in page


def test_task_list_includes_persisted_terminal_tasks_after_executor_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", Path(tmp_path))
    task_id = "completed-before-restart"
    console._persist_task({"task_id": task_id, "state": "succeeded", "updated_at": 9_999_999_999})
    assert any(task["task_id"] == task_id for task in console._list_style_tasks())


def test_unknown_get_path_returns_utf8_json_404_without_connection_error():
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as raised:
            urlopen(f"http://127.0.0.1:{server.server_port}/missing-page")
        assert raised.value.code == 404
        payload = raised.value.read().decode("utf-8")
        assert '"status": "not_found"' in payload
        assert "页面不存在" in payload
    finally:
        server.shutdown()
        server.server_close()


def test_case_rewrite_api_restores_persisted_review_record(tmp_path, monkeypatch):
    store = StylePackageStore(tmp_path)
    record = store.save_rewrite(
        style_profile_id="style_example",
        case_item={"source_url": "https://www.douyin.com/video/example", "title": "案例", "transcript": "这是用于审核比对的原案例文案。"},
        result={"rewritten_copy": "待审核二创文案", "creation_outline": {"summary": "测试"}},
    )
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/api/case-rewrites"
        listing = json.loads(urlopen(base_url).read().decode("utf-8"))
        assert listing["rewrites"][0]["rewrite_id"] == record["rewrite_id"]
        restored = json.loads(urlopen(base_url + "/" + record["rewrite_id"]).read().decode("utf-8"))
        assert restored["result"]["rewritten_copy"] == "待审核二创文案"
        assert restored["case_source"]["transcript"] == "这是用于审核比对的原案例文案。"
    finally:
        server.shutdown()
        server.server_close()


def test_case_rewrite_approve_api_persists_locked_copy(tmp_path, monkeypatch):
    from urllib.request import Request

    store = StylePackageStore(tmp_path)
    record = store.save_rewrite(
        style_profile_id="style_example",
        case_item={"source_url": "https://www.douyin.com/video/example", "title": "案例"},
        result={"rewritten_copy": "确认后的二创文案"},
    )
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/case-rewrites/{record['rewrite_id']}/approve"
        request = Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        approved = json.loads(urlopen(request).read().decode("utf-8"))
        assert approved["review_status"] == "APPROVED"
        assert approved["approved_copy"] == "确认后的二创文案"
        assert store.get_rewrite(record["rewrite_id"])["approved_payload"]["style_profile_id"] == "style_example"
    finally:
        server.shutdown()
        server.server_close()


def test_case_rewrite_runs_content_style_and_review_models_in_order(tmp_path, monkeypatch):
    store = StylePackageStore(tmp_path / "store")
    profile = {
        "profile_name": "测试风格",
        "overview": "测试用表达风格",
        "hook_patterns": ["具体提问"],
        "structure_patterns": ["问题—证据—结论"],
        "rhythm_rules": ["短句推进"],
        "voice_rules": ["口语化"],
        "rhetorical_devices": ["类比"],
        "avoid_rules": ["不复用独特口头禅"],
        "originality_guardrails": ["重新组织事实"],
        "quality_checklist": ["核验事实"],
        "expression_habit_library": {"habits": [{
            "category": "俗语化类比",
            "usage_pattern": "解释抽象观点后用生活类比落地",
            "formula": "抽象观点 → 生活场景 → 通俗收束",
            "generic_example": "把复杂问题讲成日常选择",
        }]},
    }
    pending = store.save_profile(
        profile=profile,
        creator_url="https://www.douyin.com/user/example",
        collection_id="collection_test",
        sample_count=30,
        creator_name="测试风格",
    )
    approved = store.approve_profile(pending["style_profile_id"])
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    monkeypatch.setattr(console, "collect_douyin_case_video", lambda _url: {
        "source_url": "https://www.douyin.com/video/example",
        "title": "案例",
        "transcript_status": "local_whisper",
        "transcript": "案例字幕内容。" * 15,
    })
    calls = []
    content_result = {"content_blueprint": {
        "topic": "效率",
        "core_claim": "效率差距来自系统设计",
        "fact_units": [{"id": "fact_1", "statement": "案例事实", "source_support": "原文证据"}],
        "argument_chain": [{"id": "step_1", "role": "conclusion", "point": "回扣观点", "fact_ids": ["fact_1"]}],
        "structure": [{"id": "part_1", "part": "hook", "purpose": "提出反差", "key_points": ["效率问题"]}],
    }}
    style_result = {
        "source_summary": "效率案例",
        "creation_outline": {"hook_3s": "为什么努力不等于高效？", "acts": ["解释系统"], "ending": "回到观点"},
        "rewritten_copy": "风格适配后的完整文案。",
        "expression_habits_applied": [{"category": "俗语化类比", "reason": "帮助解释抽象观点"}],
        "style_application": [{"target": "body", "habit_category": "俗语化类比", "transformation": "落到生活场景"}],
        "risk_report": {"facts_to_verify": [], "similarity_guardrails": [], "style_rules_applied": ["类比"]},
    }
    review_result = {
        "status": "PASS", "audit_summary": "通过", "style_score": 9,
        "fact_risks": [], "similarity_risks": [], "structure_issues": [], "language_issues": [],
        "revision_instructions": [], "revised_copy": "", "expression_habit_issues": [],
        "expression_habits_used": ["俗语化类比"], "dna_sections_used": ["expression_habit_library"],
    }

    class FakeTransport:
        def __init__(self, name, result):
            self.name = name
            self.result = result

        def __call__(self, _system, _user):
            calls.append(self.name)
            return json.dumps(self.result, ensure_ascii=False)

    content_transport = FakeTransport("content", content_result)
    style_transport = FakeTransport("style", style_result)
    review_transport = FakeTransport("review", review_result)
    monkeypatch.setattr(
        console.ArkStoryFailoverHTTPTransport,
        "from_content_runtime_config",
        classmethod(lambda cls: content_transport),
    )
    monkeypatch.setattr(
        console.ArkStoryFailoverHTTPTransport,
        "from_style_runtime_config",
        classmethod(lambda cls: style_transport),
    )
    monkeypatch.setattr(
        console.ArkStoryFailoverHTTPTransport,
        "from_review_runtime_config",
        classmethod(lambda cls: review_transport),
    )
    progress = []

    status, payload = console._create_case_rewrite(
        {
            "style_profile_id": approved["style_profile_id"],
            "case_video_url": "https://www.douyin.com/video/example",
            "rewrite_goal": "保留事实与观点，重新表达",
            "publish_format": "知识口播短视频",
            "target_duration": "60 秒",
            "personalized_slogan": "让普通人也能看懂复杂问题",
            "creative_direction": "这条视频主要针对在家创业女性，强调低成本试错和第一步行动。",
        },
        progress_reporter=lambda stage, value, message: progress.append((stage, value, message)),
    )

    assert status == 201
    assert calls == ["content", "style", "review"]
    saved = store.get_rewrite(payload["record"]["rewrite_id"])
    result = saved["result"]
    assert result["content_blueprint"]["core_claim"] == "效率差距来自系统设计"
    assert result["creative_brief"]["personalized_slogan"] == "让普通人也能看懂复杂问题"
    assert "在家创业女性" in result["creative_brief"]["creative_direction"]
    assert result["pipeline"]["content_analysis"] == "COMPLETED"
    assert result["pipeline"]["style_adaptation"] == "COMPLETED"
    assert result["pipeline"]["review"] == "PASS"
    assert [item[0] for item in progress if item[0] in {"content_analysis", "style_adaptation", "reviewing"}] == [
        "content_analysis", "style_adaptation", "reviewing",
    ]


def test_writing_page_has_mobile_style_package_cards():
    page = console._writing_page_live().decode("utf-8")
    assert "style-package-card" in page
    assert "style-add-card" in page
    assert "styleAddModal" in page
    assert "openAdvancedStyleDialog" in page
    assert "rewrite-compare" not in page
    assert "文案左右对比审核" not in page
    assert "data-enter-director" not in page
    assert "window.location.assign" in page
    assert "rewriteReview" not in page
    assert "rewrite-approval-actions" not in page
    assert "approveRewrite" not in page
    assert "确认文案，进入编导" not in page
    assert "确认文案并进入编导" not in page
    assert 'id="enterDirector" href="/director"' not in page
    assert "rewrite-collapsible" not in page
    assert "新增风格包" in page
    assert "style-rule-grid" in page
    assert 'id="personalizedSlogan"' in page
    assert 'id="creativeDirection"' in page
    assert "personalized_slogan" in page
    assert "creative_direction" in page
    assert "单击卡片查看规则；双击已审核卡片即可选中用于二创" in page
    assert "addEventListener('dblclick'" in page
    assert "@media(max-width:780px)" in page
    assert page.index('id="styleReview"') > page.index("</details>")


def test_writing_page_keeps_rewrite_fields_after_style_cards_refresh():
    page = console._writing_page_live_with_selection_guard().decode("utf-8")

    assert "const valueOf=(id,fallback='')" in page
    assert "请先填写案例视频链接" in page
    assert "请先选择已审核风格包" in page
    assert "const ensureRewriteFields=()=>" in page
    assert "['styleProfileSelect','']" in page
    assert "['rewriteGoal','保留事实与观点，重新表达']" in page
    assert "保留事实与观点，重新表达" in page
    assert "getSelect()" in page


def test_writing_page_shows_rewrite_progress_and_workload():
    page = console._writing_page_live_with_selection_guard().decode("utf-8")

    assert "rewriteProgressSteps" in page
    assert "setRewriteProgress" in page
    assert "正在提取案例字幕" in page
    assert "正在分析核心观点和内容结构" in page
    assert "正在执行博主风格适配" in page
    assert "正在审核风格化文案" in page
    assert "工作量：案例视频 1 个" in page
    assert "工作量：1 份新文案 · 自动进入编导" in page
    assert "文案已生成，正在进入编导" in page
    assert "工作量：1 份二创草案，等待人工审核" not in page
    assert "rewriteProgressWork" in page


def test_writing_page_exposes_safe_cookie_status_and_qr_dialog():
    page = console._writing_page_live().decode("utf-8")

    assert 'id="douyinAuthButton"' in page
    assert 'id="douyinAuthModal"' in page
    assert 'id="douyinAuthQr"' in page
    assert "/api/douyin-auth/status" in page
    assert "/api/douyin-auth/start" in page
    assert "/api/douyin-auth/qr" in page
    assert "不展示 Cookie 内容" in page

    production = console._writing_page_production_impl().decode("utf-8")
    assert "window.__ensureDouyinLogin" in production
    assert "登录二维码会在本页弹出" in production


def test_douyin_auth_status_returns_state_without_cookie_contents(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "DOUYIN_LOGIN_STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(console, "DOUYIN_SESSION_COOKIE_PATH", tmp_path / "Cookies")
    monkeypatch.setattr(console, "DOUYIN_LOGIN_QR_PATH", tmp_path / "qr.png")
    monkeypatch.setattr(console, "_douyin_service_inspection", lambda: {"overall": "current"})
    monkeypatch.setattr(
        console,
        "_douyin_monitor_json",
        lambda *_args, **_kwargs: (200, {"status": "error", "message": "登录态已失效"}),
    )
    result = console._douyin_auth_status()

    assert result["state"] == "unavailable"
    assert result["label"] == "Cookie 不可用"
    assert set(result) <= {
        "available", "state", "label", "message", "qr_available",
        "service_state", "service_reachable",
    }
    assert "cookie_value" not in json.dumps(result, ensure_ascii=False).lower()
    assert "session_data" not in json.dumps(result, ensure_ascii=False).lower()


def test_douyin_auth_status_rejects_stale_success(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "DOUYIN_LOGIN_STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(console, "DOUYIN_SESSION_COOKIE_PATH", tmp_path / "Cookies")
    monkeypatch.setattr(console, "DOUYIN_LOGIN_QR_PATH", tmp_path / "qr.png")
    monkeypatch.setattr(console, "_douyin_service_inspection", lambda: {"overall": "current"})
    monkeypatch.setattr(
        console,
        "_douyin_monitor_json",
        lambda *_args, **_kwargs: (200, {"status": "success", "message": "历史成功状态"}),
    )
    (tmp_path / "status.json").write_text(
        json.dumps({"status": "success", "updated_at": time.time() - console.DOUYIN_LOGIN_STATUS_MAX_AGE_SECONDS - 1}),
        encoding="utf-8",
    )
    (tmp_path / "Cookies").write_bytes(b"stale")

    result = console._douyin_auth_status()

    assert result["state"] == "unavailable"
    assert result["label"] == "登录态需重新确认"


def test_login_qr_helper_saves_visible_canvas_screenshot(monkeypatch, tmp_path):
    pytest.importorskip("playwright.async_api")
    helper_path = Path(console.ROOT) / "services" / "douyin-monitor" / "login_qr_helper.py"
    spec = importlib.util.spec_from_file_location("login_qr_helper_test", helper_path)
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(helper_path.parent))
    spec.loader.exec_module(helper)
    qr_path = tmp_path / "douyin_login_qr.png"
    monkeypatch.setattr(helper, "QR_PATH", qr_path)

    class FakeCandidate:
        async def is_visible(self):
            return True

        async def screenshot(self, *, path):
            Path(path).write_bytes(b"fake-png")

    class FakeLocator:
        async def count(self):
            return 1

        def nth(self, _index):
            return FakeCandidate()

    class FakePage:
        def locator(self, _selector):
            return FakeLocator()

    assert __import__("asyncio").run(helper.save_login_qr(FakePage())) is True
    assert qr_path.read_bytes() == b"fake-png"


def test_director_page_loads_approved_copy_from_writing_layer():
    page = console._director_page().decode("utf-8")
    assert 'id="approvedCopy"' in page
    assert 'id="directorSourceStatus"' in page
    assert "/api/case-rewrites/" in page
    assert "approved_payload?.approved_copy" in page
    assert "window.directorInput" in page
    assert "已载入文案创作层审核通过版本" in page
    assert 'id="directorPackagePicker"' in page
    assert 'id="directorPackageOptions"' in page
    assert 'id="directorPackageSelection"' in page
    assert 'data-director-package="v1"' in page
    assert 'data-director-package="v2"' in page
    assert 'data-director-package="v3"' in page
    assert 'data-director-package="nv3"' not in page
    assert page.count('data-director-package="') == 4
    assert "justify-content:center" in page
    assert "编导包待选区" in page
    assert "没有可用 v3 编导包，因此不显示" not in page
    assert "window.directorInput.director_package=packageId" in page
    assert "director_package:directorInput.director_package||'v3'" in page
    assert "本次运行加载" in page
    assert 'id="ratioCurrent"' in page
    assert 'data-ratio="1:1"' in page
    assert "当前画幅：" in page
    assert ".chip.active:after" in page
    assert 'id="storyReview"' in page
    assert "/api/director/story-tasks" in page
    assert "真实编导任务已启动" in page
    assert 'id="statusFill"' in page
    assert "director-progress-track" in page
    assert "removeLegacyRenderedScripts" in page
    assert "['视频脚本','大分段对应的故事桥段','逐镜脚本','逐镜视频脚本']" in page
    assert "逐镜脚本" in page
    assert "const sync=async" in page
    assert "镜头坑位" in page
    assert "/api/director/story-tasks/" in page
    assert "确认最终视频脚本并进入素材生产" in page
    assert "shot-script/approve" in page
    assert "/shot-script" in page
    assert "逐镜视频脚本" in page
    assert "narration_aligned_v2" in page
    assert "画面语义" in page
    assert "director-shot-type-legend" in page
    assert "shot-type-static-image" in page
    assert "shot-type-aigc-video" in page
    assert "shot-type-digital-human" in page
    assert "data-shot-type" in page
    assert "stopStoryTask" in page
    assert "停止当前任务" in page
    assert "/stop" in page
    assert "[hidden]{display:none!important}" in page
    assert "window.__directorStoryTaskInFlight=true" in page
    assert "content.innerHTML='';window.__directorStoryTaskInFlight=true" in page
    assert "window.__directorStoryTaskInFlight=false" in page
    assert 'id="directorParameters"' in page
    assert 'id="directorParameters" hidden' not in page
    assert "confirmDirectorCopy" not in page


def test_director_story_task_captures_selected_package_for_this_run(tmp_path, monkeypatch):
    store = StylePackageStore(tmp_path / "style-packages")
    record = store.save_rewrite(
        style_profile_id="style-example",
        case_item={"source_url": "https://example.test/video", "title": "案例"},
        result={"rewritten_copy": "已确认的编导包选择测试文案。"},
    )
    approved = store.approve_rewrite(record["rewrite_id"])
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    monkeypatch.setattr(console, "STYLE_TASK_DIR", tmp_path / "tasks")
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    started = {}

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            started["target"] = target
            started["args"] = args
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(console.threading, "Thread", FakeThread)

    task = console._create_director_story_task({
        "rewrite_id": approved["rewrite_id"],
        "aspect_ratio": "9:16",
        "director_package": "v1",
    })

    assert task["director_package"] == "v1"
    assert started["started"] is True
    assert started["args"][1]["director_package"] == "v1"


def test_selected_director_package_is_loaded_into_runtime_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        console,
        "run_live_director_text",
        lambda *_args: {"director_output": {"segments": ["测试段落"]}},
    )

    result = console._run_selected_director_package(
        "v2", tmp_path / "approved_copy.json", tmp_path / "auth.md"
    )

    assert result["director_package"] == "v2"
    assert result["director_package_source"] == "current_director_runtime"


def test_director_review_actions_remain_inside_the_director_review_panel():
    page = console._director_page().decode("utf-8")

    assert page.index('id="approvedCopy"') < page.index('id="directorParameters"')
    assert page.index('id="directorParameters"') < page.index('id="directorControls"')
    assert "confirmDirectorCopy" not in page
    controls = page[page.index('id="directorControls"') :]
    assert 'id="rewrite"' in controls
    assert "stopStoryTask" in page
    assert "const controls=document.getElementById('directorControls');const actions=controls?.querySelector('.actions');" in page


def test_new_rewrite_archives_previous_rewrites_outside_active_queue(tmp_path):
    store = StylePackageStore(tmp_path)
    old = store.save_rewrite(
        style_profile_id="style_example",
        case_item={"source_url": "https://www.douyin.com/video/old"},
        result={"rewritten_copy": "旧文案"},
    )

    report = store.archive_existing_rewrites()
    assert report["errors"] == []
    assert f"{old['rewrite_id']}.json" in report["moved"]
    assert store.list_rewrites() == []
    archived = list((tmp_path / "archived_rewrites").glob("*"))
    assert len(archived) == 1
    assert (archived[0] / f"{old['rewrite_id']}.json").is_file()
    assert (archived[0] / "archive_manifest.json").is_file()

    new = store.save_rewrite(
        style_profile_id="style_example",
        case_item={"source_url": "https://www.douyin.com/video/new"},
        result={"rewritten_copy": "新文案"},
    )
    assert [item["rewrite_id"] for item in store.list_rewrites()] == [new["rewrite_id"]]


def test_assets_page_creates_a_real_asset_task_and_renders_category_progress():
    page = console._assets_page().decode("utf-8")
    assert "立即执行" in page
    assert 'id="goEditing"' in page
    assert "进入剪辑交付 →" in page
    assert "editing'+(story?'?story_task='+enc(story):'')" in page
    assert "function shotDurationText(shot)" in page
    assert "function renderAllMedia()" in page
    assert "function renderAllMediaWithBgm()" in page
    assert "全部素材预览（AIGC 视频 + 图片 + 数字人）" in page
    assert "点击加载视频预览" not in page
    assert "source_story_task_id=" in page
    assert "preload=\"metadata\"" in page
    assert "时长 '+esc(duration)+'" in page
    assert 'id="aspectRatio"' in page
    assert "继承编导层设置" in page
    assert "本页最终选择" in page
    assert "仅补全数字人（临时）" not in page
    assert 'id="retryDigital"' not in page
    assert ".grid-original-gallery{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))" in page
    assert ".grid-original-cell{grid-column:1/-1;min-height:0}" in page
    assert ".asset-controls{display:grid;grid-template-columns:minmax(190px,1fr) minmax(300px,1.5fr) auto" in page
    assert ".asset-controls>div:last-child{align-self:start;padding-top:25px}" in page
    assert ".asset-video-card .asset-media-image{display:block;width:100%;height:210px" in page
    assert "/asset-production" in page
    assert "canStartAssets=false" in page
    assert "逐镜脚本已就绪，仍可点击“立即执行”" in page
    assert "本次视觉引导未保存，将忽略它并继续创建素材任务" in page
    assert "assetProgress" in page
    assert 'id="resumeAssetStatus"' in page
    assert "继续查询已提交宫格" in page
    assert "不会新建付费任务" in page
    assert "function progress(categories)" in page
    assert "已提交" in page
    assert "assetFourGrid" in page
    assert "/api/video-preview/" in page
    assert "grid_layout:$('gridLayout').value" in page
    assert "aspect_ratio:$('aspectRatio').dataset.userSelected==='true'?$('aspectRatio').value:''" in page
    assert 'data-label="3×3 九宫格"' in page
    assert "function renderAspect()" in page
    assert "data-asset-category=\"digital_humans\"" in page
    assert "数字人登录失败时自动使用已有宫格图片替换" in page
    assert "function renderGrid()" not in page
    assert "function renderAllVideos()" not in page
    assert 'id="assetShotPicker" class="asset-shot-picker" aria-hidden="true"' in page


def test_editing_page_exposes_enabled_windows_draft_execution_route():
    page = console._editing_page().decode("utf-8")
    assert 'id="createWindowsDraft"' in page
    assert "自定义草稿名称" in page
    assert 'id="draftName"' in page
    assert "value=\"" + __import__("time").strftime("%Y%m%d_%H%M%S") + "\"" in page
    assert page.index("草稿信息") < page.index("音频混音")
    assert 'id="editingAspectRatio" type="hidden"' in page
    assert '<select id="editingAspectRatio">' not in page
    assert "最终画幅比例" not in page
    assert "aspect_ratio:ratio.value" in page
    assert "draft_name:draftName?.value?.trim()||''" in page
    assert page.count("editing-collapsible") >= 2
    assert 'for="draftName"' in page
    assert 'for="draftRoot"' in page
    assert "sfx_volume:document.getElementById('sfxVolume')?.value||'45'" in page
    assert "BGM 音量已写入 BGM 音频轨" in page
    assert "转场音效音量已写入其他音频轨" in page
    assert "/production?editing_task=" in page
    assert "/writing?archive_status=completed" not in page
    assert 'id="draftRoot"' in page
    assert "draft_root:document.getElementById('draftRoot')?.value?.trim()" in page
    assert "videoProduction.editing.draftRoot" in page
    assert "生成 Windows 草稿（原型禁用）" not in page
    assert "/api/editing/context?story_task=" in page
    assert "fetch('/api/editing/tasks'" in page
    assert "editing-progress-before-actions" in page
    assert "host.insertBefore(progress,actions)" in page
    assert "host.insertBefore(status,actions)" in page
    assert "body.replace=true" in page
    assert "/api/editing/capabilities?kind=" not in page
    assert "const capabilityCounts=" not in page
    assert "字幕设置" not in page
    assert "镜头转场与画面效果" not in page
    assert "transitionInput" not in page
    assert "filterInput" not in page
    assert "effectInput" not in page
    assert "captionSize" not in page
    # 当前页同时承载已上线的包装包配置；保持上限以防止继续无约束膨胀。
    assert len(page.encode("utf-8")) < 220_000
    assert "EDIT_MAPPING_READY" not in page
    assert "EDIT_PLAN_READY" in console._run_editing_task.__code__.co_consts
    assert "EDIT_MAPPING_READY" not in console._run_editing_task.__code__.co_consts


def test_editing_draft_name_uses_user_value_and_legacy_fallback():
    assert console._resolve_editing_draft_name(
        "  本期视频_审核版  ", "story-123456", "task-abcdef"
    ) == "本期视频_审核版"
    assert console._resolve_editing_draft_name(
        "", "story-123456", "task-abcdef"
    ) == "video_console_story-12_task-abc_win"


def test_create_editing_task_persists_draft_name_without_changing_ratio_contract(monkeypatch, tmp_path):
    story_task_id = "story-for-draft-name"
    started = []

    monkeypatch.setattr(
        console,
        "_handoff_story_task_snapshot",
        lambda _story_task_id: {"task_id": story_task_id, "task_type": "director_story_review", "state": "succeeded"},
    )
    monkeypatch.setattr(console, "_list_style_tasks", lambda **_kwargs: [])
    monkeypatch.setattr(console, "_persist_task", lambda _task: None)

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            started.append(self.args)

    monkeypatch.setattr(console.threading, "Thread", FakeThread)
    task = console._create_editing_task(
        {
            "story_task_id": story_task_id,
            "aspect_ratio": "16:9",
            "draft_name": "  本期视频_审核版  ",
            "replace": True,
            "draft_root": str(tmp_path),
        }
    )
    try:
        assert task["draft_name"] == "本期视频_审核版"
        assert task["aspect_ratio"] == "16:9"
        assert task["replace"] is True
        assert started and started[0][1]["draft_name"] == "  本期视频_审核版  "
        assert started[0][1]["replace"] is True
    finally:
        with console.STYLE_TASKS_LOCK:
            console.STYLE_TASKS.pop(task["task_id"], None)


def test_run_editing_task_writes_requested_draft_name_to_handoff_and_result(monkeypatch, tmp_path):
    import workflow_1256.editing_layer as editing_layer
    import workflow_1256.real_asset_handoff as real_asset_handoff

    class FakeRegistry:
        def to_dict(self):
            return {"assets": []}

    class FakeManifest:
        validation = {"ok": True}

        def to_dict(self):
            return {"status": "EDIT_PLAN_READY"}

    class FakePlan:
        width = 1920
        height = 1080
        fps = 30

        def to_dict(self):
            return {"tracks": [], "native_items": [], "captions": []}

        def to_edit_manifest(self, *, status):
            assert status == "EDIT_PLAN_READY"
            return FakeManifest()

    class FakeDraftResult:
        draft_path = str(tmp_path / "本期视频_审核版")
        duration_us = 1_000_000
        tracks = []
        media_paths_exist = True
        effects_count = 0
        transitions_count = 0
        edit_manifest = FakeManifest()

        def to_dict(self):
            return {"draft_path": self.draft_path, "duration_us": self.duration_us}

    updates = []
    writer_calls = []
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path / "reviews")
    monkeypatch.setattr(
        console,
        "_load_editing_source_inputs",
        lambda _story_task_id: (
            {"task_id": "story-1"},
            {"task_id": "shot-1"},
            {
                "director_lock": {
                    "subtitle_pipeline": {
                        "status": "succeeded",
                        "source": "capcut_stt_doubao_mini",
                    },
                    "captions": [{"text": "测试字幕", "start_us": 0, "end_us": 1_000_000}],
                },
                "shot_video_script": [{"shot_id": "g01_s01"}],
            },
            "asset-1",
            {"output_dir": str(tmp_path / "assets"), "aspect_ratio": "16:9"},
        ),
    )
    monkeypatch.setattr(console, "_set_task", lambda task_id, **changes: updates.append((task_id, changes)))
    monkeypatch.setattr(console, "_archive_runtime_data", lambda **_kwargs: {"status": "skipped"})
    monkeypatch.setattr(real_asset_handoff, "director_from_lock", lambda _lock: {"director": "fake"})
    monkeypatch.setattr(
        real_asset_handoff,
        "build_live_asset_registry",
        lambda *_args, **_kwargs: (FakeRegistry(), {"status": "ok"}),
    )
    monkeypatch.setattr(editing_layer, "compile_editing_layer", lambda *_args, **_kwargs: FakePlan())
    monkeypatch.setattr(editing_layer, "validate_windows_native_plan", lambda _plan: None)
    monkeypatch.setattr(
        editing_layer,
        "write_windows_native_draft",
        lambda *args, **kwargs: writer_calls.append(kwargs) or FakeDraftResult(),
    )

    console._run_editing_task(
        "task-abcdef",
        {
            "story_task_id": "story-123456",
            "aspect_ratio": "",
            "draft_name": "  本期视频_审核版  ",
            "draft_root": str(tmp_path),
        },
    )

    assert writer_calls and writer_calls[0]["draft_name"] == "本期视频_审核版"
    assert writer_calls[0]["replace"] is False
    succeeded = next(changes for _task_id, changes in updates if changes.get("state") == "succeeded")
    assert succeeded["result"]["draft_name"] == "本期视频_审核版"
    handoff_path = Path(succeeded["result"]["handoff_path"])
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert handoff["draft_name"] == "本期视频_审核版"
    assert handoff["aspect_ratio"] == "16:9"


def test_editing_capability_catalog_route_is_removed():
    assert not hasattr(console, "_editing_capability_options")
    assert "/api/editing/capabilities" not in console.Handler.do_GET.__code__.co_consts


def test_opening_template_draft_listing_reads_local_draft_folders(tmp_path, monkeypatch):
    root = tmp_path / "JianyingPro Drafts"
    template = root / "片头模板"
    full = root / "完整草稿"
    template.mkdir(parents=True)
    full.mkdir(parents=True)
    (template / "draft_info.json").write_text(json.dumps({"name": "片头模板", "duration": 5_000_000}), encoding="utf-8")
    (full / "draft_info.json").write_text(json.dumps({"name": "完整草稿", "duration": 178_133_333}), encoding="utf-8")

    result = console._list_jianying_drafts(root)

    assert result["root_exists"] is True
    assert {item["name"] for item in result["drafts"]} == {"片头模板", "完整草稿"}
    assert all(item["template_eligible"] for item in result["drafts"])
    assert all(Path(item["path"]).is_dir() for item in result["drafts"])


def test_draft_listing_includes_new_jianying_folder_without_draft_info(tmp_path):
    root = tmp_path / "JianyingPro Drafts"
    draft = root / "数字人模板"
    draft.mkdir(parents=True)
    (draft / "draft_content.json").write_text(
        json.dumps({"duration": 12_066_666, "tracks": [], "materials": {}}),
        encoding="utf-8",
    )

    result = console._list_jianying_drafts(root)

    assert [item["name"] for item in result["drafts"]] == ["数字人模板"]
    assert result["drafts"][0]["duration_us"] == 12_066_666
    assert result["drafts"][0]["template_eligible"] is True
    assert result["drafts"][0]["packaging_eligible"] is True


def test_draft_listing_prefers_renamed_folder_over_stale_internal_name(tmp_path):
    root = tmp_path / "JianyingPro Drafts"
    draft = root / "片头模板"
    draft.mkdir(parents=True)
    (draft / "draft_info.json").write_text(
        json.dumps({"name": "旧的剪映内部名称", "duration": 5_000_000}),
        encoding="utf-8",
    )
    (draft / "draft_content.json").write_text(
        json.dumps({"duration": 5_000_000, "tracks": [], "materials": {}}),
        encoding="utf-8",
    )

    item = console._list_jianying_drafts(root)["drafts"][0]

    assert item["name"] == "片头模板"
    assert item["draft_internal_name"] == "旧的剪映内部名称"


def test_opening_validation_accepts_new_jianying_folder_without_draft_info(tmp_path):
    root = tmp_path / "JianyingPro Drafts"
    template = root / "片头模板"
    full = root / "数字人模板"
    template.mkdir(parents=True)
    full.mkdir(parents=True)
    for path, duration in ((template, 5_000_000), (full, 12_066_666)):
        (path / "draft_content.json").write_text(
            json.dumps({"duration": duration, "tracks": [], "materials": {}}),
            encoding="utf-8",
        )

    template_info, full_info = console._validate_opening_template_inputs(template, full, root)

    assert template_info["duration"] == 5_000_000
    assert full_info["duration"] == 12_066_666


def test_opening_template_governance_rejects_short_template_and_cross_folder_target(tmp_path):
    root = tmp_path / "drafts"
    root.mkdir()
    template = root / "短模板"
    full = root / "完整草稿"
    external = tmp_path / "其他目录" / "完整草稿"
    for folder in (template, full, external):
        folder.mkdir(parents=True)
    (template / "draft_info.json").write_text(json.dumps({"duration": 4_999_999}), encoding="utf-8")
    (full / "draft_info.json").write_text(json.dumps({"duration": 10_000_000}), encoding="utf-8")
    (external / "draft_info.json").write_text(json.dumps({"duration": 10_000_000}), encoding="utf-8")

    with pytest.raises(ValueError, match="至少为 5 秒"):
        console._validate_opening_template_inputs(template, full, root)
    (template / "draft_info.json").write_text(json.dumps({"duration": 5_000_000}), encoding="utf-8")
    with pytest.raises(ValueError, match="必须位于同一个剪映草稿目录"):
        console._validate_opening_template_inputs(template, external, root)


def test_generated_draft_listing_only_returns_successful_editing_tasks(tmp_path, monkeypatch):
    generated = tmp_path / "console_output"
    generated.mkdir()
    tasks = [
        {
            "task_id": "editing-ok",
            "task_type": "editing_draft",
            "state": "succeeded",
            "updated_at": 20,
            "result": {"draft_path": str(generated), "draft_name": "本模块草稿", "duration_us": 8},
        },
        {
            "task_id": "opening-only",
            "task_type": "opening_template_apply",
            "state": "succeeded",
            "updated_at": 30,
            "result": {"output": str(tmp_path / "opening-output")},
        },
    ]
    monkeypatch.setattr(console, "_list_style_tasks", lambda **_kwargs: tasks)

    result = console._list_editing_generated_drafts(tmp_path)

    assert [item["name"] for item in result["drafts"]] == ["本模块草稿"]
    assert result["drafts"][0]["task_id"] == "editing-ok"


def test_generated_draft_listing_reads_archived_editing_task(tmp_path, monkeypatch):
    generated = tmp_path / "archived_console_output"
    generated.mkdir()
    archive_tasks = tmp_path / "archives" / "run-1" / "executor_tasks"
    archive_tasks.mkdir(parents=True)
    (archive_tasks / "editing-archived.json").write_text(
        json.dumps({
            "task_id": "editing-archived",
            "task_type": "editing_draft",
            "state": "succeeded",
            "updated_at": 40,
            "result": {"draft_path": str(generated), "draft_name": "归档完整草稿"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(console, "RUNTIME_ARCHIVE_DIR", tmp_path / "archives")
    monkeypatch.setattr(console, "STYLE_TASK_DIR", tmp_path / "empty-tasks")
    monkeypatch.setattr(console, "_list_style_tasks", lambda **_kwargs: [])

    result = console._list_editing_generated_drafts(tmp_path)

    assert [item["name"] for item in result["drafts"]] == ["归档完整草稿"]


def test_opening_template_task_runs_data_layer_merge_and_persists_result(tmp_path, monkeypatch):
    import tools.apply_opening_template_to_full_draft as opening

    output_root = tmp_path / "drafts"
    output_root.mkdir()
    template = output_root / "template"
    full = output_root / "full"
    template.mkdir()
    full.mkdir()
    (template / "draft_info.json").write_text(json.dumps({"duration": 5_000_000}), encoding="utf-8")
    (full / "draft_info.json").write_text(json.dumps({"duration": 10_000_000}), encoding="utf-8")
    calls = []
    updates = []
    monkeypatch.setattr(
        opening,
        "build",
        lambda template_dir, full_dir, output_dir: calls.append((template_dir, full_dir, output_dir)) or {
            "output": str(output_dir),
            "missing_media_count": 0,
            "first_5s_tracks": [["video", "主视觉", 1]],
        },
    )
    monkeypatch.setattr(console, "_set_task", lambda task_id, **changes: updates.append((task_id, changes)))

    console._run_opening_template_task(
        "opening-task-1",
        {
            "template_draft": str(template),
            "full_draft": str(full),
            "draft_root": str(output_root),
            "output_name": "套用结果",
        },
    )

    assert calls == [(template.resolve(), full.resolve(), (output_root / "套用结果").resolve())]
    succeeded = next(changes for _task_id, changes in updates if changes.get("state") == "succeeded")
    assert succeeded["stage"] == "opening_template_applied"
    assert succeeded["result"]["output_name"] == "套用结果"


def test_opening_template_task_derives_output_name_from_full_draft(tmp_path, monkeypatch):
    import tools.apply_opening_template_to_full_draft as opening

    template = tmp_path / "template"
    full = tmp_path / "完整目标"
    template.mkdir()
    full.mkdir()
    (template / "draft_info.json").write_text(json.dumps({"duration": 5_000_000}), encoding="utf-8")
    (full / "draft_info.json").write_text(json.dumps({"duration": 10_000_000}), encoding="utf-8")
    monkeypatch.setattr(
        opening,
        "build",
        lambda _template_dir, _full_dir, output_dir: {
            "output": str(output_dir),
            "missing_media_count": 0,
            "first_5s_tracks": [],
        },
    )
    updates = []
    monkeypatch.setattr(console, "_set_task", lambda task_id, **changes: updates.append((task_id, changes)))

    console._run_opening_template_task(
        "opening-derived-name",
        {"template_draft": str(template), "full_draft": str(full), "draft_root": str(tmp_path)},
    )

    succeeded = next(changes for _task_id, changes in updates if changes.get("state") == "succeeded")
    assert succeeded["result"]["output_name"] == "完整目标_套用片头模板"


def test_create_opening_template_task_rejects_existing_output(tmp_path):
    template = tmp_path / "template"
    full = tmp_path / "full"
    output = tmp_path / "result"
    for folder in (template, full, output):
        folder.mkdir()
        (folder / "draft_info.json").write_text(json.dumps({"duration": 5_000_000}), encoding="utf-8")

    with pytest.raises(FileExistsError, match="目标草稿已存在"):
        console._create_opening_template_task(
            {
                "template_draft": str(template),
                "full_draft": str(full),
                "draft_root": str(tmp_path),
                "output_name": "result",
            }
        )


def test_create_opening_template_task_rejects_non_module_target(tmp_path, monkeypatch):
    template = tmp_path / "template"
    full = tmp_path / "full"
    template.mkdir()
    full.mkdir()
    (template / "draft_info.json").write_text(json.dumps({"duration": 5_000_000}), encoding="utf-8")
    (full / "draft_info.json").write_text(json.dumps({"duration": 10_000_000}), encoding="utf-8")
    monkeypatch.setattr(console, "_list_editing_generated_drafts", lambda _root: {"drafts": []})

    with pytest.raises(ValueError, match="必须来自本模块已生成"):
        console._create_opening_template_task(
            {
                "template_draft": str(template),
                "full_draft": str(full),
                "draft_root": str(tmp_path),
                "output_name": "result",
            }
        )


def test_create_editing_task_rejects_missing_draft_root_before_start(monkeypatch):
    story_task_id = "story-for-draft-root-validation"
    monkeypatch.setattr(console, "_task_snapshot", lambda _task_id: {"task_id": story_task_id})
    missing_root = Path(console.__file__).resolve().parent / "__missing_draft_root_for_test__"

    with pytest.raises(ValueError, match="草稿保存目录不存在或不是文件夹"):
        console._create_editing_task(
            {"story_task_id": story_task_id, "draft_root": str(missing_root)}
        )


def test_resolve_asset_task_for_retry_recovers_latest_task_for_story(monkeypatch):
    stale_task_id = "asset-stale"
    current_task = {
        "task_id": "asset-current",
        "task_type": "asset_production",
        "source_story_task_id": "story-1",
        "updated_at": 20,
        "result": {"source_story_task_id": "story-1"},
    }
    monkeypatch.setattr(console, "_task_snapshot", lambda _task_id: None)
    monkeypatch.setattr(
        console,
        "_list_style_tasks",
        lambda **kwargs: [current_task],
    )

    task_id, task = console._resolve_asset_task_for_retry(stale_task_id, "story-1")

    assert task_id == "asset-current"
    assert task == current_task


def test_asset_retry_targets_only_failed_records_and_never_uploaded_bgm():
    shots = [
        {"shot_id": "g01_s01", "first_frame_prompt": "图", "media_type": "aigc_video"},
        {"shot_id": "g01_s02", "first_frame_prompt": "图", "media_type": "aigc_video"},
        {"shot_id": "g01_s03", "first_frame_prompt": None, "media_type": "digital_human_video"},
    ]
    targets = console._asset_retry_targets(
        {
            "categories": {
                "first_frame_images": {"state": "succeeded"},
                "aigc_videos": {"state": "failed"},
                "digital_humans": {"state": "failed"},
                "bgm": {"state": "failed"},
            },
            "aigc_video_assets": {
                "shot_results": [
                    {"shot_id": "g01_s01", "status": "succeeded"},
                    {"shot_id": "g01_s02", "status": "failed"},
                ]
            },
            "digital_human_assets": {
                "shot_results": [{"shot_id": "g01_s03", "status": "failed"}],
            },
            "bgm_assets": {"status": "failed", "source_type": "uploaded"},
        },
        shots,
    )
    assert targets["aigc"] == {"g01_s02"}
    assert targets["digital"] == {"g01_s03"}
    assert targets["bgm"] is False


def test_retry_failed_asset_production_dispatches_only_failed_aigc_without_new_full_task(monkeypatch):
    task = {
        "task_id": "asset-1",
        "task_type": "asset_production",
        "source_story_task_id": "story-1",
        "result": {
            "categories": {"aigc_videos": {"state": "failed"}, "digital_humans": {"state": "failed"}},
            "aigc_video_assets": {"shot_results": [{"shot_id": "g01_s01", "status": "failed"}]},
            "digital_human_assets": {"shot_results": [{"shot_id": "g01_s02", "status": "failed"}]},
        },
    }
    monkeypatch.setattr(console, "_task_snapshot", lambda _task_id: task)
    monkeypatch.setattr(console, "_latest_shot_script_task", lambda _story_id: {"result": {"shot_video_script": [
        {"shot_id": "g01_s01", "first_frame_prompt": "图", "media_type": "aigc_video"},
        {"shot_id": "g01_s02", "first_frame_prompt": None, "media_type": "digital_human_video"},
    ]}})
    calls = []
    monkeypatch.setattr(console, "_retry_failed_asset_videos", lambda task_id: calls.append(task_id) or {"task_id": task_id})
    assert console._retry_failed_asset_production("asset-1") == {"task_id": "asset-1"}
    assert calls == ["asset-1"]


def test_retry_asset_shot_dispatches_only_the_clicked_aigc_shot(monkeypatch):
    task = {
        "task_id": "asset-1",
        "task_type": "asset_production",
        "source_story_task_id": "story-1",
        "state": "failed",
        "result": {
            "categories": {"aigc_videos": {"state": "failed"}},
            "aigc_video_assets": {"shot_results": [
                {"shot_id": "g01_s01", "status": "succeeded"},
                {"shot_id": "g01_s02", "status": "failed"},
            ]},
        },
    }
    monkeypatch.setattr(console, "_task_snapshot", lambda _task_id: task)
    monkeypatch.setattr(console, "_latest_shot_script_task", lambda _story_id: {"result": {"shot_video_script": [
        {"shot_id": "g01_s01", "media_type": "aigc_video"},
        {"shot_id": "g01_s02", "media_type": "aigc_video"},
    ]}})
    calls = []
    monkeypatch.setattr(
        console,
        "_retry_failed_asset_videos",
        lambda task_id, shot_id=None: calls.append((task_id, shot_id)) or {"task_id": task_id},
    )

    assert console._retry_asset_shot("asset-1", "g01_s02") == {"task_id": "asset-1"}
    assert calls == [("asset-1", "g01_s02")]


def test_retry_asset_shot_creates_digital_only_task_for_clicked_digital_shot(monkeypatch):
    task = {
        "task_id": "asset-1",
        "task_type": "asset_production",
        "source_story_task_id": "story-1",
        "state": "failed",
        "grid_layout": "3x3",
        "aspect_ratio": "9:16",
        "result": {
            "categories": {"digital_humans": {"state": "failed"}},
            "digital_human_assets": {"shot_results": [
                {"shot_id": "g01_s04", "status": "failed"},
            ]},
        },
    }
    monkeypatch.setattr(console, "_task_snapshot", lambda _task_id: task)
    monkeypatch.setattr(console, "_latest_shot_script_task", lambda _story_id: {"result": {"shot_video_script": [
        {"shot_id": "g01_s04", "media_type": "digital_human_video"},
    ]}})
    calls = []
    monkeypatch.setattr(
        console,
        "_create_asset_production_task",
        lambda source_story_task_id, **kwargs: calls.append((source_story_task_id, kwargs)) or {"task_id": "digital-retry"},
    )

    assert console._retry_asset_shot("asset-1", "g01_s04") == {"task_id": "digital-retry"}
    assert calls == [("story-1", {
        "grid_layout": "3x3",
        "mode": "digital_only",
        "base_asset_task_id": "asset-1",
        "aspect_ratio": "9:16",
        "retry_shot_id": "g01_s04",
    })]


def test_retry_failed_digital_humans_dispatches_only_failed_ids_and_preserves_success(monkeypatch):
    task = {
        "task_id": "asset-1",
        "task_type": "asset_production",
        "source_story_task_id": "story-1",
        "grid_layout": "3x3",
        "aspect_ratio": "9:16",
        "result": {
            "categories": {"digital_humans": {"state": "failed"}},
            "digital_human_assets": {"shot_results": [
                {"shot_id": "g01_s01", "status": "succeeded"},
                {"shot_id": "g01_s02", "status": "failed"},
                {"shot_id": "g01_s03", "status": "failed"},
            ]},
        },
    }
    monkeypatch.setattr(console, "_task_snapshot", lambda _task_id: task)
    monkeypatch.setattr(console, "_latest_shot_script_task", lambda _story_id: {"result": {"shot_video_script": [
        {"shot_id": "g01_s01", "media_type": "digital_human_video"},
        {"shot_id": "g01_s02", "media_type": "digital_human_video"},
        {"shot_id": "g01_s03", "media_type": "digital_human_video"},
    ]}})
    calls = []
    monkeypatch.setattr(
        console,
        "_create_asset_production_task",
        lambda source_story_task_id, **kwargs: calls.append((source_story_task_id, kwargs)) or {"task_id": "digital-retry"},
    )

    assert console._retry_failed_digital_humans("asset-1") == {"task_id": "digital-retry"}
    assert calls == [("story-1", {
        "grid_layout": "3x3",
        "mode": "digital_only",
        "base_asset_task_id": "asset-1",
        "aspect_ratio": "9:16",
        "retry_shot_ids": ["g01_s02", "g01_s03"],
    })]


def test_asset_categories_follow_locked_media_routes_without_marking_unrun_as_success():
    categories = console._asset_categories([
        {"media_type": "aigc_video", "first_frame_prompt": "图一"},
        {"media_type": "static_image", "first_frame_prompt": "图二"},
        {"media_type": "digital_human_video", "first_frame_prompt": None},
    ])
    assert categories["first_frame_images"]["total"] == 2
    assert categories["first_frame_images"]["submitted"] == 0
    assert categories["aigc_videos"]["total"] == 1
    assert categories["digital_humans"]["total"] == 1
    assert {item["state"] for item in categories.values()} == {"queued"}


def test_digital_tts_preflight_requires_each_locked_group_audio_on_disk(tmp_path):
    ready = tmp_path / "voice_01.mp3"
    ready.write_bytes(b"mp3")
    script_result = {
        "director_lock": {
            "narration_assets": [
                {"group_id": "g01", "local_path": str(ready)},
                {"group_id": "g02", "local_path": str(tmp_path / "missing.mp3")},
            ]
        }
    }
    shots = [
        {"shot_id": "g01_s01", "group_id": "g01", "media_type": "digital_human_video"},
        {"shot_id": "g02_s01", "group_id": "g02", "media_type": "digital_human_video"},
    ]

    assert console._missing_digital_tts_assets(script_result, shots) == [
        f"g02: {tmp_path / 'missing.mp3'}"
    ]


def test_digital_tts_preflight_accepts_existing_locked_audio(tmp_path):
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"mp3")
    script_result = {
        "director_lock": {
            "narration_assets": [{"group_id": "g01", "local_path": str(audio)}]
        }
    }
    assert console._missing_digital_tts_assets(
        script_result,
        [{"group_id": "g01", "media_type": "digital_human_video"}],
    ) == []


def test_parallel_task_result_merge_keeps_each_branch_and_completed_asset():
    current = {
        "categories": {
            "bgm": {"state": "succeeded", "completed": 1, "total": 1},
            "aigc_videos": {"state": "running", "completed": 2, "total": 4},
        },
        "bgm_assets": {"status": "succeeded", "audio_path": "bgm.mp3"},
        "aigc_video_assets": {
            "shot_results": [{"shot_id": "g01_s01", "status": "succeeded", "video_path": "a.mp4"}]
        },
    }
    stale_digital_update = {
        "categories": {
            "bgm": {"state": "running", "completed": 0, "total": 1},
            "aigc_videos": {"state": "running", "completed": 1, "total": 4},
            "digital_humans": {"state": "running", "completed": 1, "total": 2},
        },
        "digital_human_assets": {
            "shot_results": [{"shot_id": "g02_s01", "status": "submitted", "task_id": "rh-1"}]
        },
    }

    merged = console._merge_task_result_snapshots(current, stale_digital_update)

    assert merged["categories"]["bgm"]["state"] == "succeeded"
    assert merged["categories"]["aigc_videos"]["completed"] == 2
    assert merged["categories"]["digital_humans"]["completed"] == 1
    assert merged["bgm_assets"]["audio_path"] == "bgm.mp3"
    assert merged["digital_human_assets"]["shot_results"][0]["task_id"] == "rh-1"


def test_asset_video_generation_selects_first_frames_by_shot_id_not_list_length():
    selected = console._select_first_frames_for_video_shots(
        {
            "image_urls": ["https://image.test/static.jpg", "https://image.test/video.jpg"],
            "image_paths": ["C:/images/static.jpg", "C:/images/video.jpg"],
        },
        [
            {"shot_id": "static_01"},
            {"shot_id": "video_02"},
        ],
        [{"shot_id": "video_02"}],
    )
    assert selected == {
        "image_urls": ["https://image.test/video.jpg"],
        "image_paths": ["C:/images/video.jpg"],
    }


def test_asset_preview_path_uses_task_trace_and_rejects_paths_outside_review_dir(tmp_path, monkeypatch):
    review_dir = tmp_path / "reviews"
    image = review_dir / "task" / "images" / "first_frame_01.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"jpg")
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", review_dir)
    monkeypatch.setattr(console, "_task_snapshot", lambda _task_id: {
        "result": {
            "first_frame_prompt_trace": [{"shot_id": "s01"}],
            "first_frame_assets": {"image_paths": [str(image)]},
        },
    })
    assert console._asset_preview_path("task", "s01") == image.resolve()
    assert console._asset_preview_path("task", "missing") is None

    monkeypatch.setattr(console, "_task_snapshot", lambda _task_id: {
        "result": {
            "output_dir": str(image.parents[1]),
            "first_frame_prompt_trace": [{"shot_id": "s01"}],
        },
    })
    assert console._asset_preview_path("task", "s01") == image.resolve()


def test_director_story_task_runs_locked_chain_and_persists_review(tmp_path, monkeypatch):
    store = StylePackageStore(tmp_path / "store")
    record = store.save_rewrite(
        style_profile_id="style_example",
        case_item={"source_url": "https://example.test/video", "title": "案例"},
        result={"rewritten_copy": "已经审核通过的完整测试文案。"},
    )
    approved = store.approve_rewrite(record["rewrite_id"])
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    monkeypatch.setattr(console, "STYLE_TASK_DIR", tmp_path / "tasks")
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path / "reviews")
    auth_document = tmp_path / "auth.md"
    auth_document.write_text("测试鉴权", encoding="utf-8")
    monkeypatch.setenv("DIRECTOR_AUTH_DOCUMENT", str(auth_document))
    monkeypatch.setattr(console, "run_live_director_text", lambda *_args: {"director_output": {"segments": ["第一段"]}})
    monkeypatch.setattr(console, "run_live_tts", lambda *_args, **_kwargs: {"tts_group_timelines": [{"start": 0, "end": 1_000_000}]})
    subtitle_calls = []
    monkeypatch.setattr(
        console,
        "run_live_subtitle_pipeline",
        lambda result: subtitle_calls.append(result) or {
            "status": "succeeded",
            "source": "capcut_stt_doubao_mini",
            "timeline_unit": "microseconds",
            "captions": [],
        },
    )
    monkeypatch.setattr(console, "run_live_story_draft", lambda *_args, **_kwargs: {"review_status": "PENDING_USER_REVIEW", "story": {"movie_outline": {"protagonist": "林夏", "protagonist_goal": "完成任务", "central_conflict": "资源不足", "character_arc": "主动选择", "ending_hook": "新的清晨"}, "silent_story_text": "林夏在清晨迈出一步。", "supporting_characters": [], "conflict_chain": {}, "scene_groups": [], "narration_mappings": []}})
    def complete_internal_shot_script(task_id):
        parent = console._task_snapshot(task_id)
        assert parent is not None
        result = dict(parent["result"])
        result["review_status"] = "APPROVED"
        console._set_task(
            task_id,
            state="succeeded",
            stage="review_ready",
            progress=100,
            message="最终逐镜脚本已生成，等待素材生产。",
            result=result,
        )
        return {"task_id": "shot-script-test"}

    monkeypatch.setattr(console, "_create_director_shot_script_task", complete_internal_shot_script)

    task = console._create_director_story_task({"rewrite_id": approved["rewrite_id"], "aspect_ratio": "9:16", "voice_key": "m191_2"})
    for _ in range(40):
        snapshot = console._task_snapshot(task["task_id"])
        if snapshot and snapshot["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.03)
    assert snapshot is not None
    assert snapshot["state"] == "succeeded"
    assert snapshot["result"]["review_status"] == "APPROVED"
    assert snapshot["result"]["approved_copy"] == "已经审核通过的完整测试文案。"
    assert snapshot["result"]["voice_resolution"]["voice_key"] == "m191_2"
    assert snapshot["result"]["voice_resolution"]["speaker_id"] == "zh_male_m191_uranus_bigtts"
    assert len(subtitle_calls) == 1
    assert (tmp_path / "reviews" / task["task_id"] / "story_review.json").is_file()
    story_approved = console._approve_director_story_task(task["task_id"])
    assert story_approved["review_status"] == "APPROVED"
    assert console._task_snapshot(task["task_id"])["stage"] == "story_approved"


def test_tts_cache_reuses_identical_segments_without_requiring_tts_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DIRECTOR_TTS_CACHE_DIR", tmp_path / "tts-cache")
    source_audio = tmp_path / "voice.mp3"
    source_audio.write_bytes(b"real-audio-placeholder")
    director_result = {"director_output": {"segments": ["同一段文案"]}}
    tts = {
        "tts_contract": {
            "schema_version": "tts-voice-binding-v1",
            "voice": {"voice_key": "qingcang_2", "speaker_id": "zh_male_qingcang_uranus_bigtts"},
            "audio": {"paths": [str(source_audio)], "sha256": [], "actual_durations_s": [1.25]},
            "auth_slots": [],
            "voice_bindings": [],
            "timing_authority": "capcut_stt_required",
        },
    }
    cache_key = console._tts_cache_key(director_result)
    console._store_tts_cache(cache_key, tts)
    cached = console._load_cached_tts(cache_key, segment_count=1)
    assert cached is not None
    assert cached["cache"]["hit"] is True
    assert cached["timing_authority"] == "capcut_stt_required"
    assert "tts_group_timelines" not in cached
    assert Path(cached["tts_contract"]["audio"]["paths"][0]).is_file()


def test_tts_cache_key_includes_selected_voice():
    director_result = {"director_output": {"segments": ["同一段文案"]}}

    qingcang = {"voice_key": "qingcang_2", "speaker_id": "zh_male_qingcang_uranus_bigtts"}
    m191 = {"voice_key": "m191_2", "speaker_id": "zh_male_m191_uranus_bigtts"}

    assert console._tts_cache_key(director_result, qingcang) != console._tts_cache_key(director_result, m191)


def test_director_shot_script_reuses_existing_tts_and_keeps_locked_microsecond_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_TASK_DIR", tmp_path / "tasks")
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path / "reviews")
    auth_document = tmp_path / "auth.md"
    auth_document.write_text("测试鉴权", encoding="utf-8")
    monkeypatch.setenv("DIRECTOR_AUTH_DOCUMENT", str(auth_document))
    story_task_id = "story-review"
    story_dir = tmp_path / "reviews" / story_task_id
    story_dir.mkdir(parents=True)
    (story_dir / "approved_copy.json").write_text('{"text":"测试文案"}', encoding="utf-8")
    director_result = {
        "director_output": {"segments": ["第一大段"]},
        "tts": {"tts_group_timelines": [{"start": 0, "end": 5_000_000}], "cache": {"hit": True}},
        "subtitle": {"status": "succeeded", "source": "capcut_stt_doubao_mini"},
    }
    story_result = {
        "source_rewrite_id": "rewrite-test",
        "aspect_ratio": "16:9",
        "director_package": "v2",
        "director_result": director_result,
        "story_draft": {"story": {"opening_title": "效率越高，代价越大？", "silent_story_text": "测试故事"}},
    }
    with console.STYLE_TASKS_LOCK:
        console.STYLE_TASKS[story_task_id] = {
            "task_id": story_task_id,
            "task_type": "director_story_review",
            "state": "succeeded",
            "stage": "review_ready",
            "progress": 100,
            "message": "待审核",
            "created_at": 1,
            "updated_at": 1,
            "result": story_result,
        }
    console._persist_task(console.STYLE_TASKS[story_task_id])
    refinement = {"small_shot_count": 2, "output": {"Code_list": [{"group_id": "g01", "shots": [{"source_text": "前半旁白的画面语义", "narration_text": "第一大段"}, {"source_text": "后半旁白的画面语义", "narration_text": "第二大段"}]}]}}
    lock = {"status": "DIRECTOR_LOCKED", "groups": [
        {"group_id": "g01", "segment_text": "第一大段", "timeline": {"start_us": 0, "end_us": 5_000_000}},
    ], "shots": [
        {"shot_id": "g01_s01", "group_id": "g01", "source_text": "前半旁白的画面语义", "production_spec": {"narration_text": "第一大段"}, "timeline": {"start_us": 0, "end_us": 2_000_000}},
        {"shot_id": "g01_s02", "group_id": "g01", "source_text": "后半旁白的画面语义", "production_spec": {"narration_text": "第二大段"}, "timeline": {"start_us": 2_000_000, "end_us": 5_000_000}},
    ]}
    material = {
        "prompt": ["首帧提示词一", "首帧提示词二"],
        "motion_seed": ["动作种子一", "动作种子二"],
        "ref_image": [{"ref_image": []}, {"ref_image": []}],
        "int_duration": [2, 3],
        "director_governance": {"shot_contexts": [
            {"shot_id": "g01_s01", "visual_direction": {"camera_angle": "平视", "camera_motion": "推近", "shot_size": "中景"}, "blocking": "抬手拿起工具", "continuity_in": "工具在桌边", "continuity_out": "工具移向机器"},
            {"shot_id": "g01_s02", "visual_direction": {"camera_angle": "俯视", "camera_motion": "固定", "shot_size": "近景"}, "blocking": "工具落下完成修复", "continuity_in": "工具移向机器", "continuity_out": "灯光亮起"},
        ]},
        "media_route_lock": {"status": "DIRECTOR_MEDIA_LOCKED", "routes": [
            {"shot_id": "g01_s01", "media_type": "aigc_video", "media_label": "AIGC 视频", "media_reason": "全片首镜必须使用 AIGC。", "required_assets": [], "skip": {"skip_first_frame": False, "skip_aigc_video": False, "skip_video_prompt": False}, "editing_track": "main_visual"},
            {"shot_id": "g01_s02", "media_type": "aigc_video", "media_label": "AIGC 视频", "media_reason": "需要动作表现", "required_assets": [], "skip": {"skip_first_frame": False, "skip_aigc_video": False, "skip_video_prompt": False}, "editing_track": "main_visual"},
        ]},
    }
    # 首镜即使只有 2 秒，也必须进入 AIGC 运镜计划；下游按冻结微秒时间线裁回。
    plans = {"shot_ids": ["g01_s01", "g01_s02"], "plan_out_list": [{"plans": [
        {"shot_index": 0, "duration": 2, "stages": [{"time_range": "0-2秒", "camera_motion": "推近", "action": "建立开场视觉锚点"}]},
    ]}, {"plans": [
        {"shot_index": 1, "duration": 3, "stages": [{"time_range": "0-3秒", "camera_motion": "固定机位", "action": "完成修复"}]},
    ]}]}
    monkeypatch.setattr(console, "run_live_shot_refinement", lambda *_args: refinement)
    monkeypatch.setattr(console, "build_approved_director_lock", lambda *_args: lock)
    monkeypatch.setattr(
        console,
        "apply_consecutive_static_video_pacing",
        lambda _director_result, _input_path: {"merged_spans": [], "promoted_single_slot_ids": []},
    )
    material_calls: list[dict] = []
    def fake_material_planning(*_args, **kwargs):
        material_calls.append(kwargs)
        return material
    monkeypatch.setattr(console, "run_live_material_planning", fake_material_planning)
    monkeypatch.setattr(console, "_run_live_video_prompt_script", lambda *_args: plans)
    monkeypatch.setattr(console, "run_live_tts", lambda *_args: pytest.fail("逐镜脚本不得重新调用 TTS"))

    task = console._create_director_shot_script_task(story_task_id)
    for _ in range(40):
        snapshot = console._task_snapshot(task["task_id"])
        if snapshot and snapshot["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.03)
    assert snapshot is not None
    assert snapshot["state"] == "succeeded"
    script = snapshot["result"]["shot_video_script"]
    assert len(script) == 2
    assert script[0]["start_us"] == 0
    assert script[1]["end_us"] == 5_000_000
    assert script[0]["duration_s"] == 2.0
    assert script[0]["media_type"] == "aigc_video"
    assert script[0]["camera_motion"][0]["camera_motion"] == "推近"
    assert script[0]["narration"] == "第一大段"
    assert script[0]["semantic_anchor"] == "前半旁白的画面语义"
    assert snapshot["result"]["script_schema_version"] == "narration_aligned_v2"
    assert script[1]["media_type"] == "aigc_video"
    assert script[1]["camera_motion"][0]["camera_motion"] == "固定机位"
    assert material_calls == [{
        "aspect_ratio": "16:9",
        "director_package": "v2",
        "dynamic_params": {},
    }]
    assert snapshot["result"]["aspect_ratio"] == "16:9"
    assert snapshot["result"]["opening_title"] == "效率越高，代价越大？"
    assert snapshot["result"]["segment_timeline_unit"] == "microseconds"
    assert snapshot["result"]["group_timeline_source"] == "director_lock.groups_from_capcut_stt"
    assert snapshot["result"]["group_timelines"] == [{
        "segment_index": 0,
        "segment_id": "g01",
        "segment_text": "第一大段",
        "start_us": 0,
        "end_us": 5_000_000,
        "duration_us": 5_000_000,
    }]
    assert snapshot["result"]["segment_timeline_unit"] == "microseconds"
    assert snapshot["result"]["segment_timeline_source"] == "director_lock.shots_from_capcut_stt"
    assert snapshot["result"]["segment_timelines"] == snapshot["result"]["secondary_segment_timelines"]
    assert snapshot["result"]["secondary_segment_timeline_unit"] == "microseconds"
    assert snapshot["result"]["secondary_segment_timeline_source"] == "director_lock.shots_from_capcut_stt"
    assert snapshot["result"]["secondary_segment_timelines"] == [
        {
            "secondary_segment_index": 0,
            "shot_index": 0,
            "shot_id": "g01_s01",
            "group_id": "g01",
            "segment_text": "前半旁白的画面语义",
            "start_us": 0,
            "end_us": 2_000_000,
            "duration_us": 2_000_000,
        },
        {
            "secondary_segment_index": 1,
            "shot_index": 1,
            "shot_id": "g01_s02",
            "group_id": "g01",
            "segment_text": "后半旁白的画面语义",
            "start_us": 2_000_000,
            "end_us": 5_000_000,
            "duration_us": 3_000_000,
        },
    ]
    assert snapshot["result"]["transition_timeline_unit"] == "microseconds"
    assert snapshot["result"]["transition_timeline_source"] == "secondary_segment_timelines"
    assert snapshot["result"]["transition_points"] == [{
        "transition_index": 0,
        "at_us": 2_000_000,
        "from_shot_id": "g01_s01",
        "to_shot_id": "g01_s02",
            "from_group_id": "g01",
            "to_group_id": "g01",
            "classification_level": "level_3",
            "default_enabled": False,
        }]
    assert "opening_5s_plan" not in snapshot["result"]
    assert (story_dir / "shot_video_script.json").is_file()


def test_segment_timelines_for_transition_is_v2_only_and_keeps_order():
    lock = {
        "groups": [
            {"group_id": "g01", "segment_text": "第一段", "timeline": {"start_us": 0, "end_us": 2_000_000}},
            {"group_id": "g02", "segment_text": "第二段", "timeline": {"start_us": 2_000_000, "end_us": 5_500_000}},
        ]
    }
    assert console._segment_timelines_for_transition(lock, director_package="v1") == []
    assert console._segment_timelines_for_transition(lock, director_package="v2") == [
        {"segment_index": 0, "segment_id": "g01", "segment_text": "第一段", "start_us": 0, "end_us": 2_000_000, "duration_us": 2_000_000},
        {"segment_index": 1, "segment_id": "g02", "segment_text": "第二段", "start_us": 2_000_000, "end_us": 5_500_000, "duration_us": 3_500_000},
    ]


def test_secondary_segment_timelines_for_transition_uses_refined_shots():
    lock = {
        "groups": [
            {"group_id": "g01", "segment_text": "第一段", "timeline": {"start_us": 0, "end_us": 5_500_000}},
        ],
        "shots": [
            {"shot_id": "g01_s01", "group_id": "g01", "source_text": "第一镜", "timeline": {"start_us": 0, "end_us": 1_234_567}},
            {"shot_id": "g01_s02", "group_id": "g01", "source_text": "第二镜", "timeline": {"start_us": 1_234_567, "end_us": 5_500_000}},
        ],
    }
    assert console._secondary_segment_timelines_for_transition(lock, director_package="v1") == []
    assert console._secondary_segment_timelines_for_transition(lock, director_package="v2") == [
        {
            "secondary_segment_index": 0,
            "shot_index": 0,
            "shot_id": "g01_s01",
            "group_id": "g01",
            "segment_text": "第一镜",
            "start_us": 0,
            "end_us": 1_234_567,
            "duration_us": 1_234_567,
        },
        {
            "secondary_segment_index": 1,
            "shot_index": 1,
            "shot_id": "g01_s02",
            "group_id": "g01",
            "segment_text": "第二镜",
            "start_us": 1_234_567,
            "end_us": 5_500_000,
            "duration_us": 4_265_433,
        },
    ]


def test_transition_points_mark_level_two_and_level_three_defaults():
    timelines = [
        {"shot_id": "g01_s01", "group_id": "g01", "end_us": 1_000_000},
        {"shot_id": "g01_s02", "group_id": "g01", "end_us": 2_000_000},
        {"shot_id": "g02_s01", "group_id": "g02", "end_us": 3_000_000},
    ]
    points = console._transition_points_from_secondary_timelines(timelines)
    assert [(item["classification_level"], item["default_enabled"]) for item in points] == [
        ("level_3", False),
        ("level_2", True),
    ]


def test_video_prompt_script_retries_an_incomplete_model_plan(tmp_path, monkeypatch):
    class FakeModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def __call__(self, *_args):
            return "{}"

    director_result = {
        "shot_refinement": {"output": {"Code_list": [{"group_id": "g01", "shots": [{}, {}]}]}},
        "director_lock": {"status": "DIRECTOR_LOCKED", "shots": [
            {"shot_id": "g01_s01", "timeline": {"start_us": 0, "end_us": 3_000_000}},
            {"shot_id": "g01_s02", "timeline": {"start_us": 3_000_000, "end_us": 6_000_000}},
        ]},
    }
    material = {
        "motion_seed": ["一", "二"], "ref_image": [{"ref_image": []}, {"ref_image": []}], "int_duration": [2, 3],
        "director_governance": {"shot_contexts": [
            {"camera_intent": "接近人物", "visual_direction": {"camera_angle": "平视", "shot_size": "中景", "composition": "斜向纵深", "camera_motion": "推近"}},
            {"camera_intent": "停住观察", "visual_direction": {"camera_angle": "俯视", "shot_size": "近景", "composition": "局部焦点与留白", "camera_motion": "固定"}},
        ]},
        "media_route_lock": {"status": "DIRECTOR_MEDIA_LOCKED", "routes": [
            {"shot_id": "g01_s01", "media_type": "aigc_video", "media_label": "AIGC 视频", "media_reason": "需要动作表现", "required_assets": [], "skip": {"skip_first_frame": False, "skip_aigc_video": False, "skip_video_prompt": False}, "editing_track": "main_visual"},
            {"shot_id": "g01_s02", "media_type": "aigc_video", "media_label": "AIGC 视频", "media_reason": "需要动作表现", "required_assets": [], "skip": {"skip_first_frame": False, "skip_aigc_video": False, "skip_video_prompt": False}, "editing_track": "main_visual"},
        ]},
    }
    calls = {"count": 0}

    def fake_batch(_params, *, transport):
        calls["count"] += 1
        if calls["count"] == 1:
            raise console.VideoPromptDirectorValidationError("plans 数量不足")
        return {"plan_out_list": [{"plans": [{"shot_index": 1, "duration": 2, "stages": [{"action": "推进", "camera_motion": "推近", "time_range": "0-2秒"}]}]}]}

    monkeypatch.setattr(console, "DualSeed21TurboTextTransport", FakeModel)
    monkeypatch.setattr(console, "run_video_prompt_batch", fake_batch)
    result = console._run_live_video_prompt_script(director_result, material, tmp_path / "auth.md")
    assert len(result["plan_out_list"]) == 2
    assert calls["count"] == 3


def test_video_prompt_inputs_split_each_locked_small_shot_without_reordering():
    director_result = {
        "shot_refinement": {"output": {"Code_list": [
            {"group_id": "g01", "shots": [{"shot_id": "g01_s01"}, {"shot_id": "g01_s02"}]},
            {"group_id": "g02", "shots": [{"shot_id": "g02_s01"}]},
        ]}},
        "director_lock": {"status": "DIRECTOR_LOCKED", "shots": [
            {"shot_id": "g01_s01", "timeline": {"start_us": 0, "end_us": 2_000_000}},
            {"shot_id": "g01_s02", "timeline": {"start_us": 2_000_000, "end_us": 5_000_000}},
            {"shot_id": "g02_s01", "timeline": {"start_us": 5_000_000, "end_us": 9_000_000}},
        ]},
    }
    material = {
        "motion_seed": ["种子一", "种子二", "种子三"],
        "ref_image": [{"ref_image": ["one.png"]}, {"ref_image": []}, {"ref_image": ["three.png"]}],
        "int_duration": [2, 3, 4],
        "director_governance": {"shot_contexts": [
            {"camera_intent": "建立", "visual_direction": {"camera_angle": "高机位", "shot_size": "全景", "composition": "边缘压力切入", "camera_motion": "跟拍"}},
            {"camera_intent": "逼近", "visual_direction": {"camera_angle": "平视", "shot_size": "近景", "composition": "局部焦点与留白", "camera_motion": "推近"}},
            {"camera_intent": "揭示", "visual_direction": {"camera_angle": "仰视", "shot_size": "远景", "composition": "斜向纵深", "camera_motion": "拉远"}},
        ]},
        "media_route_lock": {"status": "DIRECTOR_MEDIA_LOCKED", "routes": [
            {"shot_id": "g01_s01", "media_type": "aigc_video", "media_label": "AIGC 视频", "media_reason": "全片首镜必须使用 AIGC。", "required_assets": [], "skip": {"skip_first_frame": False, "skip_aigc_video": False, "skip_video_prompt": False}, "editing_track": "main_visual"},
            {"shot_id": "g01_s02", "media_type": "aigc_video", "media_label": "AIGC 视频", "media_reason": "需要动作表现", "required_assets": [], "skip": {"skip_first_frame": False, "skip_aigc_video": False, "skip_video_prompt": False}, "editing_track": "main_visual"},
            {"shot_id": "g02_s01", "media_type": "aigc_video", "media_label": "AIGC 视频", "media_reason": "需要动作表现", "required_assets": [], "skip": {"skip_first_frame": False, "skip_aigc_video": False, "skip_video_prompt": False}, "editing_track": "main_visual"},
        ]},
    }
    params = console._group_video_prompt_inputs(director_result, material)
    assert [item["group_id"] for item in params["items"]] == ["g01", "g01", "g02"]
    assert [item["clip_duration"] for item in params["clip_duration"]] == [[2], [3], [4]]
    assert params["skipped_shot_ids"] == []
    assert [item["motion_seed"] for item in params["motion_seed"]] == [["种子一"], ["种子二"], ["种子三"]]
    assert params["ref_image"] == [["one.png"], [], ["three.png"]]
    assert params["items"][2]["director_camera_language"]["recommended_camera_motion"] == "拉远"


def test_asset_manifest_keeps_reference_images_and_uses_formal_image2_prompt_template():
    manifest = console._asset_manifest_from_shots([{
        "shot_id": "g01_s01",
        "first_frame_prompt": "首帧画面：9:16竖版，白天修车铺内。硬性画面禁令：不得出现文字。必须可见=林夏擦汗；景别=远景；机位=高机位；构图=边缘压力切入。",
        "first_frame_reference": {"urls": ["https://example.test/style.png"]},
        "duration_s": 3.2,
        "shot_content": "林夏擦汗",
        "motion_seed": "停住观察",
    }], aspect_ratio="16:9")
    shot = manifest["shots"][0]
    assert shot["ref_images"] == ["https://example.test/style.png"]
    assert shot["first_frame_prompt"] != ""
    assert shot["grid_prompt"] == shot["first_frame_prompt"]
    assert "【画幅】16:9 横版" in shot["first_frame_prompt"]
    assert "【场景】白天修车铺内" in shot["first_frame_prompt"]
    assert shot["first_frame_prompt"].count("9:16") == 0
    assert "【参考一致性】严格保持参考图" in shot["first_frame_prompt"]
    assert "【主体与动作】林夏擦汗" in shot["first_frame_prompt"]
    assert "【镜头语言】景别=远景；视角=高机位；构图=边缘压力切入" in shot["first_frame_prompt"]
    assert "【负面约束】" in shot["first_frame_prompt"]
    assert "故事映射锚点" not in shot["first_frame_prompt"]


def test_grid_preview_uses_same_shared_grid_prompt_as_asset_executor():
    preview = console._grid_preview_from_script({
        "aspect_ratio": "16:9",
        "shot_video_script": [
            {
                "shot_id": f"s{index}",
                "first_frame_prompt": (
                    f"首帧画面：16:9横版，场景{index}。"
                    "视觉导演书（必须执行）：景别=远景；机位=高机位；构图=边缘压力切入；"
                    "调度=人物与关键物件完成单一动作。"
                ),
                "duration_s": 3,
            }
            for index in range(1, 6)
        ] + [{"shot_id": "host", "first_frame_prompt": None, "media_type": "digital_human_video"}],
    }, grid_layout="3x3")
    batches = preview["grid_batches"]
    assert preview["aspect_ratio"] == "16:9"
    assert preview["skipped_digital_human_count"] == 1
    # 3x3 的尾批有 5--8 个镜头时仍保留九宫格，裁切后丢弃占位格，
    # 不应回退成两张四宫格任务。
    assert [batch["grid_layout"] for batch in batches] == ["3x3"]
    assert "生成一张3行3列的无缝多宫格首帧图" in batches[0]["prompt"]
    assert "画面采用远景，以高机位视角拍摄，构图突出边缘压力切入" in batches[0]["prompt"]
    assert "人物与关键物件完成单一动作" in batches[0]["prompt"]
    assert "景别=" not in batches[0]["prompt"]
    assert "构图=" not in batches[0]["prompt"]
    assert "调度=" not in batches[0]["prompt"]
    assert "visual_guidance" not in batches[0]["prompt"]
    assert batches[0]["cells"][0]["shot_size"] == "远景"
    assert batches[0]["cells"][0]["viewpoint"] == "高机位"
    assert batches[0]["cells"][0]["composition"] == "边缘压力切入"
    assert batches[0]["cells"][0]["staging"] == "人物与关键物件完成单一动作"
    assert len(batches[0]["cells"]) == 5
    assert batches[0]["unused_cell_count"] == 4


def test_grid_preview_accepts_asset_layer_aspect_ratio_override():
    preview = console._grid_preview_from_script({
        "aspect_ratio": "9:16",
        "shot_video_script": [
            {"shot_id": "g01_s01", "first_frame_prompt": "首帧画面：竖版场景。", "duration_s": 3},
        ],
    }, grid_layout="2x2", aspect_ratio="16:9")
    assert preview["aspect_ratio"] == "16:9"
    assert preview["aspect_ratio_source"] == "asset_user_override"
    assert preview["grid_batches"][0]["aspect_ratio"] == "16:9"


def test_grid_preview_includes_saved_guidance_and_reference_style_without_local_paths():
    preview = console._grid_preview_from_script(
        {
            "aspect_ratio": "16:9",
            "shot_video_script": [
                {"shot_id": "s1", "first_frame_prompt": "【场景】菜场【主体与动作】人物停下", "duration_s": 3},
            ],
        },
        grid_layout="2x2",
        visual_guidance_prompt="注意微缩场景画风和纯白色背景",
        visual_guidance_source="",
        reference_image_count=2,
    )

    prompt = preview["grid_batches"][0]["prompt"]
    assert prompt.splitlines()[0] == "根据参考图的画风、笔触、色调与角色基础连续性，生成一张2行2列的无缝多宫格首帧图。"
    assert prompt.splitlines()[1] == "注意微缩场景画风和纯白色背景"
    assert "C:\\" not in prompt
    assert "reference_images" not in prompt


def test_asset_task_persists_final_asset_layer_aspect_ratio(monkeypatch):
    story_task_id = "story-for-asset-aspect"
    story_task = {
        "task_id": story_task_id,
        "task_type": "director_story_review",
        "state": "succeeded",
        "result": {"review_status": "APPROVED", "aspect_ratio": "9:16"},
    }
    stored = {}

    monkeypatch.setattr(console, "STYLE_TASKS", stored)
    monkeypatch.setattr(
        console,
        "_latest_shot_script_task",
        lambda _task_id: {"task_id": "script-1", "result": {"review_status": "APPROVED", "shot_video_script": []}},
    )
    monkeypatch.setattr(console, "_persist_task", lambda _task: None)
    monkeypatch.setattr(console, "_run_asset_production_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(console, "_task_snapshot", lambda task_id: story_task if task_id == story_task_id else stored.get(task_id))

    task = console._create_asset_production_task(story_task_id, aspect_ratio="16:9")
    assert task["aspect_ratio"] == "16:9"
    assert task["aspect_ratio_source"] == "asset_user_override"


def test_asset_task_creation_is_idempotent_for_same_input(monkeypatch):
    story_task_id = "story-idempotent-assets"
    story_task = {
        "task_id": story_task_id,
        "task_type": "director_story_review",
        "state": "succeeded",
        "result": {"review_status": "APPROVED", "aspect_ratio": "9:16"},
    }
    existing = {
        "task_id": "asset-existing",
        "task_type": "asset_production",
        "source_story_task_id": story_task_id,
        "input": {
            "source_story_task_id": story_task_id,
            "grid_layout": "3x3",
            "aspect_ratio": "9:16",
            "mode": "full",
            "base_asset_task_id": "",
            "retry_shot_ids": [],
            "remotion_settings": {},
        },
        "state": "running",
        "stage": "first_frame_images",
        "progress": 20,
    }
    monkeypatch.setattr(console, "STYLE_TASKS", {"asset-existing": existing})
    monkeypatch.setattr(console, "_persisted_task_snapshots", lambda: [])
    monkeypatch.setattr(console, "_latest_shot_script_task", lambda _task_id: {
        "task_id": "script-1",
        "result": {"review_status": "APPROVED", "shot_video_script": []},
    })
    monkeypatch.setattr(console, "_task_snapshot", lambda task_id: story_task if task_id == story_task_id else existing)

    reused = console._create_asset_production_task(story_task_id, mode="full")

    assert reused["task_id"] == "asset-existing"
    assert reused["deduplicated"] is True


def test_full_asset_mode_is_persisted_without_digital_tts_preflight(monkeypatch):
    story_task_id = "story-first-frame-only"
    story_task = {
        "task_id": story_task_id,
        "task_type": "director_story_review",
        "state": "succeeded",
        "result": {"review_status": "APPROVED", "aspect_ratio": "9:16"},
    }
    stored = {}

    monkeypatch.setattr(console, "STYLE_TASKS", stored)
    monkeypatch.setattr(
        console,
        "_latest_shot_script_task",
        lambda _task_id: {
            "task_id": "script-1",
            "result": {"review_status": "APPROVED", "shot_video_script": []},
        },
    )
    monkeypatch.setattr(console, "_persist_task", lambda _task: None)
    monkeypatch.setattr(console, "_run_asset_production_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        console,
        "_task_snapshot",
        lambda task_id: story_task if task_id == story_task_id else stored.get(task_id),
    )

    task = console._create_asset_production_task(story_task_id, mode="full")

    assert task["mode"] == "full"
    assert task["input"]["mode"] == "full"


def test_asset_auth_scope_includes_all_production_channels():
    assert console._asset_production_auth_channels() == (
        "image-generation", "video-generation", "digital-human", "bgm-audio", "audio-storage"
    )


def test_assets_page_only_renders_shared_grid_prompt_panel():
    page = console._assets_page().decode("utf-8")
    assert "本组共用宫格生图提示词" in page
    assert "③ 本组视频预览" in page
    assert "③ 裁切后的逐格首帧" not in page
    assert "var fresh=preview&&preview.grid_batches" in page
    assert "镜头提示词" in page
    assert "shot_results" in page
    assert "视频尚未生成" in page
    assert ".asset-cell:last-child{grid-column:1/-1" in page
    assert page.count("<script>") == 1
    assert "setInterval(" not in page
    assert "window.fetch=" not in page


def test_assets_page_hides_per_shot_reference_audit_list():
    page = console._assets_page().decode("utf-8")
    assert 'id="promptTrace"' not in page
    assert "first_frame_prompt_trace" not in page
    assert "<b>参考图规则：</b>" not in page


def test_assets_page_auto_saves_selected_materials_when_starting_task():
    page = console._assets_page().decode("utf-8")
    assert 'id="saveMaterialInputs"' not in page
    assert 'id="chooseStyleReferenceImages"' in page
    assert 'id="chooseDigitalHumanImages"' in page
    assert 'href="/image-library?type=style"' in page
    assert 'href="/image-library?type=digital"' in page
    assert 'id="localImageLibraryModal"' not in page
    assert 'id="localImageLibraryGrid"' not in page
    # 图片仍走本机图片库；BGM 是单条可选音频，保留原生文件选择控件。
    assert 'id="bgmFile" type="file"' in page
    assert 'id="styleInlineLibrary"' not in page
    assert "/api/local-image-library" not in page
    assert "/select-local-material-images" not in page
    assert "/upload-material-images" not in page
    assert "/pick-material-images" not in page
    assert "/remove-material-image" in page
    assert "async function restore()" in page
    assert "input.click()" not in page
    assert "window.__saveMaterialInputs" not in page
    assert 'data-asset-mode="full"' in page
    assert "first_frame_only" not in page
    assert "continueAssets" not in page
    assert "visualGuidanceDirty=false" in page
    assert "source:'explicit_user'" in page
    assert "if(visualGuidanceDirty)" in page
    assert "renderVisualGuidance(d.visual_guidance_prompt,d.visual_guidance_source" in page


def test_assets_page_recovers_approved_story_before_shot_script_is_ready():
    page = console._assets_page().decode("utf-8")

    # 直接刷新 /assets 时，不能因为逐镜脚本尚未成功就丢掉已确认故事的
    # story_task；参考图和数字人形象图仍必须能带着任务上下文进入选择页。
    assert "x.task_type==='director_story_review'" in page
    assert "x.result.review_status==='APPROVED'" in page
    assert "当前可先选择画风参考图和数字人形象图" in page
    assert 'id="backDirector"' in page
    assert "director?story_task=" in page


def test_local_image_library_page_uses_web_cards_without_native_file_picker(monkeypatch) -> None:
    monkeypatch.setattr(console, "_handoff_story_task_snapshot", lambda _task_id: {"task_id": "story-123"})
    page = console._image_library_page(
        {"story_task": ["story-123"], "type": ["digital"]}
    ).decode("utf-8")

    assert "本机图片库" in page
    assert 'id="libraryGrid"' in page
    assert 'id="confirmLibrary"' in page
    assert 'method="post" action="/image-library/select"' in page
    assert 'name="story_task" value="story-123"' in page
    assert 'name="type" value="digital"' in page
    assert 'type="checkbox" name="token"' in page or "图片、桌面和下载目录中没有可用图片" in page
    assert 'type="file"' not in page
    assert "选择数字人形象图片" in page
    assert "最多选择 16 张" in page
    assert "<script>" not in page


def test_assets_page_places_total_status_below_category_progress():
    page = console._assets_page().decode("utf-8")
    assert page.index('id="assetProgress"') < page.index('id="assetStatus"')


def test_assets_page_counts_first_frame_progress_by_grid_tasks():
    page = console._assets_page().decode("utf-8")
    assert "button[data-grid-id]" not in page
    assert "已提交 '+submitted+' / '+total" in page


def test_assets_page_restores_optional_bgm_input_and_preview():
    page = console._assets_page().decode("utf-8")
    assert 'id="bgmFile"' in page
    assert "upload-material-bgm" in page
    assert "remove-material-bgm" in page
    assert "背景音乐" in page


def test_material_input_saves_style_and_digital_images_to_story_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path)
    payload = {"style_reference_images": ["data:image/png;base64,aW1hZ2U="], "digital_human_images": ["data:image/jpeg;base64,aW1hZ2U="]}
    result = console._save_material_inputs("story", payload)
    saved = console._load_material_inputs("story")
    assert result == {"style_reference_count": 1, "digital_human_count": 1, "bgm_uploaded": False}
    assert all(Path(path).is_file() for path in saved["style_reference_images"] + saved["digital_human_images"])


def test_visual_guidance_requires_explicit_source_when_saved_with_material_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path)

    console._save_material_inputs(
        "story",
        {"visual_guidance_prompt": "历史文本"},
    )
    assert console._load_material_inputs("story")["visual_guidance_source"] == ""

    console._save_material_inputs(
        "story",
        {
            "visual_guidance_prompt": "用户明确要求",
            "visual_guidance_source": "explicit_user",
        },
    )
    saved = console._load_material_inputs("story")
    assert saved["visual_guidance_prompt"] == "用户明确要求"
    assert saved["visual_guidance_source"] == "explicit_user"


def test_visual_guidance_endpoint_marks_user_text_as_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path)

    result = console._save_visual_guidance("story", {"visual_guidance_prompt": "纯白背景"})
    assert result["visual_guidance_source"] == "explicit_user"


def test_optional_bgm_upload_is_saved_and_can_be_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path)
    result = console._save_material_bgm("story", {"audio": "data:audio/mpeg;base64,Ymdt"})
    saved = console._load_material_inputs("story")

    assert result["bgm"]["name"] == "background_music.mp3"
    assert Path(saved["bgm_audio"]).read_bytes() == b"bgm"
    assert console._material_bgm_path("story").is_file()

    removed = console._remove_material_bgm("story")
    assert removed["bgm"] is None
    assert console._material_bgm_path("story") is None


def test_browser_upload_preserves_the_other_material_group(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path)
    console._save_material_inputs(
        "story",
        {
            "style_reference_images": ["data:image/png;base64,c3R5bGU="],
            "digital_human_images": ["data:image/png;base64,ZGlnaXRhbA=="],
        },
    )

    result = console._upload_material_images(
        "story", "style", {"images": ["data:image/jpeg;base64,bmV3LXN0eWxl"]}
    )
    saved = console._load_material_inputs("story")

    assert result["style_reference_count"] == 1
    assert result["digital_human_count"] == 1
    assert Path(saved["style_reference_images"][0]).suffix == ".jpg"
    assert Path(saved["digital_human_images"][0]).read_bytes() == b"digital"


def test_picker_selected_paths_are_copied_and_manifested(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path / "reviews")
    first = tmp_path / "first.png"
    second = tmp_path / "second.webp"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    result = console._save_material_image_paths("story", "style", [str(first), str(second)])
    saved = console._load_material_inputs("story")

    assert result["style_reference_count"] == 2
    assert result["digital_human_count"] == 0
    assert [item["name"] for item in result["selected"]] == ["style_reference_01.png", "style_reference_02.webp"]
    assert all(Path(path).is_file() for path in saved["style_reference_images"])

    summary = console._material_inputs_public_summary("story")
    assert summary["style_reference_count"] == 2
    assert [item["preview_url"] for item in summary["style"]] == [
        "/api/material-input-preview/story/style/0",
        "/api/material-input-preview/story/style/1",
    ]

    removed = console._remove_material_image("story", "style", 0)
    assert removed["style_reference_count"] == 1
    assert removed["selected"][0]["preview_url"] == "/api/material-input-preview/story/style/0"


def test_reference_image_limit_accepts_16_and_rejects_17(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DIRECTOR_REVIEW_DIR", tmp_path / "reviews")
    paths = []
    for index in range(17):
        image = tmp_path / f"reference-{index:02d}.png"
        image.write_bytes(f"image-{index}".encode("utf-8"))
        paths.append(str(image))

    accepted = console._save_material_image_paths("story", "style", paths[:16])
    assert accepted["style_reference_count"] == 16

    with pytest.raises(ValueError, match="一次最多选择 16 张图片"):
        console._save_material_image_paths("story", "style", paths)


def test_category_template_versions_are_independent_and_listing_keeps_latest(tmp_path, monkeypatch):
    bundle_root = tmp_path / "video_packaging_bundles"
    category_root = bundle_root / "_category_templates"
    monkeypatch.setattr(console, "VIDEO_PACKAGING_BUNDLE_ROOT", bundle_root)

    def write_manifest(directory_name, category, version, source_path):
        directory = category_root / directory_name
        directory.mkdir(parents=True)
        (directory / "bundle_manifest.json").write_text(
            json.dumps(
                {
                    "package_type": "video_packaging_bundle",
                    "bundle_name": f"日更视频包装包_{category}",
                    "version": version,
                    "categories": [
                        {
                            "category": category,
                            "label": category,
                            "source": {"source_path": source_path},
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    write_manifest("日更视频包装包_v0.1_image", "image", "0.1", "D:/drafts/image-v1")
    write_manifest("日更视频包装包_v0.2_image", "image", "0.2", "D:/drafts/image-v2")
    write_manifest("日更视频包装包_v0.1_aigc", "aigc", "0.1", "D:/drafts/aigc-v1")

    result = console._list_video_packaging_category_templates("日更视频包装包", "1.0")
    by_category = {item["category"]: item for item in result["templates"]}

    assert result["history_count"] == 3
    assert by_category["image"]["version"] == "0.2"
    assert by_category["image"]["source_draft"].endswith("image-v2")
    assert by_category["aigc"]["version"] == "0.1"
    assert console._next_video_packaging_version("日更视频包装包", "image") == "0.3"
    assert console._next_video_packaging_version("日更视频包装包", "aigc") == "0.2"


def test_category_template_listing_prefers_selected_full_bundle_snapshot(tmp_path, monkeypatch):
    bundle_root = tmp_path / "video_packaging_bundles"
    bundle = bundle_root / "日更视频包装包_v0.2"
    raw = bundle / "categories" / "digital_human" / "raw_snapshot"
    raw.mkdir(parents=True)
    (raw / "draft_content.json").write_text("{}", encoding="utf-8")
    (raw.parent / "category_manifest.json").write_text(
        json.dumps({"validation": {"collected_components": {"effects": 2, "keyframes": 0, "transitions": 0, "text_templates": 0}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (bundle / "bundle_manifest.json").write_text(
        json.dumps({
            "package_type": "video_packaging_bundle",
            "bundle_name": "日更视频包装包",
            "version": "0.2",
            "categories": [{
                "category": "digital_human",
                "label": "数字人",
                "raw_snapshot": "categories\\digital_human\\raw_snapshot",
                "source": {"source_path": "D:/drafts/数字人模板"},
                "collected_components": {"effects": 2},
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    stale = bundle_root / "_category_templates" / "日更视频包装包_v0.6_digital_human"
    stale.mkdir(parents=True)
    (stale / "bundle_manifest.json").write_text(
        json.dumps({
            "package_type": "video_packaging_bundle",
            "bundle_name": "日更视频包装包_digital_human",
            "version": "0.6",
            "categories": [{"category": "digital_human", "source": {"source_path": "D:/drafts/完整视频"}}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(console, "VIDEO_PACKAGING_BUNDLE_ROOT", bundle_root)

    result = console._list_video_packaging_category_templates("日更视频包装包", "0.2")

    assert result["history_count"] == 1
    assert result["templates"][0]["source_draft"].endswith("数字人模板")
    assert result["templates"][0]["collected_components"]["effects"] == 2


def test_full_draft_style_endpoint_reads_saved_overall_style_without_rescanning(tmp_path, monkeypatch):
    bundle_root = tmp_path / "video_packaging_bundles"
    bundle = bundle_root / "日更视频包装包_v0.1"
    bundle.mkdir(parents=True)
    (bundle / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "package_type": "video_packaging_bundle",
                "bundle_name": "日更视频包装包",
                "version": "0.1",
                "full_draft_style": {
                    "schema_version": "full-draft-style-v1",
                    "audio": {"track_count": 2, "tracks": [{"name": "BGM"}]},
                    "visual": {"overall_effects": [{"material": {"name": "暗角"}}]},
                    "subtitles": {"profiles": [{"profile": {"font_name": "经典雅黑"}}]},
                    "transitions": {"applied_count": 1},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(console, "VIDEO_PACKAGING_BUNDLE_ROOT", bundle_root)

    result = console._get_video_packaging_full_draft_style("日更视频包装包", "0.1")

    assert result["status"] == "ready"
    assert result["full_draft_style"]["audio"]["tracks"][0]["name"] == "BGM"
    assert result["full_draft_style"]["visual"]["overall_effects"][0]["material"]["name"] == "暗角"


def test_full_draft_style_overrides_save_as_separate_manual_layer(tmp_path, monkeypatch):
    bundle_root = tmp_path / "video_packaging_bundles"
    bundle = bundle_root / "日更视频包装包_v0.1"
    bundle.mkdir(parents=True)
    manifest = {
        "package_type": "video_packaging_bundle",
        "bundle_name": "日更视频包装包",
        "version": "0.1",
        "full_draft_style": {
            "schema_version": "full-draft-style-v1",
            "source": {"path": "D:/drafts/full"},
            "audio": {"tracks": [{"name": "BGM"}]},
        },
        "validation": {"source_drafts_modified": False},
    }
    (bundle / "bundle_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(console, "VIDEO_PACKAGING_BUNDLE_ROOT", bundle_root)

    result = console._save_video_packaging_style_overrides(
        {
            "bundle_name": "日更视频包装包",
            "version": "0.1",
            "overrides": {
                "audio_tracks": [{"volume_percent": 999, "muted": True}],
                "subtitle_profiles": [{"font_name": "  经典雅黑  ", "font_size": 42}],
                "unknown": [{"content": "must not be persisted"}],
            },
        }
    )

    saved = json.loads((bundle / "bundle_manifest.json").read_text(encoding="utf-8"))
    overrides = saved["full_draft_style"]["manual_overrides"]
    assert result["status"] == "saved"
    assert overrides["audio_tracks"] == [{"index": 0, "volume_percent": 300.0, "muted": True}]
    assert overrides["subtitle_profiles"] == [{"index": 0, "font_name": "经典雅黑", "font_size": 42.0}]
    assert "unknown" not in overrides
    assert saved["full_draft_style"]["source"]["path"] == "D:/drafts/full"
    assert saved["validation"]["source_drafts_modified"] is False


def test_packaging_application_carries_upstream_transition_points(tmp_path, monkeypatch):
    draft_content = tmp_path / "category" / "raw_snapshot" / "draft_content.json"
    draft_content.parent.mkdir(parents=True)
    draft_content.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        console,
        "_list_video_packaging_category_templates",
        lambda _name, _version: {
            "templates": [{
                "category": "image",
                "version": "0.1",
                "category_output_dir": str(draft_content.parent.parent),
            }]
        },
    )

    resolved = console._resolve_editing_packaging_application(
        {"packaging_bundle": {"bundle_name": "测试包", "version": "0.1"}},
        [{"shot_id": "s1", "media_type": "static_image"}, {"shot_id": "s2", "media_type": "static_image"}],
        [{"transition_index": "0", "at_us": "3000000.4", "from_shot_id": "s1", "to_shot_id": "s2"}],
        "microseconds",
        "看似解决了，代价谁来付？",
    )

    assert resolved is not None
    assert resolved["opening_title"] == "看似解决了，代价谁来付？"
    resolved_long_title = console._resolve_editing_packaging_application(
        {"packaging_bundle": {"bundle_name": "测试包", "version": "0.1"}},
        [{"shot_id": "s1", "media_type": "static_image"}],
        opening_title="效率越高，代价越大？额外标题",
    )
    assert resolved_long_title is not None
    assert resolved_long_title["opening_title"] == "效率越高，代价越大？额外标题"[:12]
    assert resolved["transition_timeline_source"] == "upstream.transition_points"
    assert resolved["transition_points"] == [{
        "transition_index": 0,
        "at_us": 3_000_000,
        "from_shot_id": "s1",
        "to_shot_id": "s2",
        "from_group_id": "",
        "to_group_id": "",
        "classification_level": "level_2",
        "default_enabled": True,
        "enabled": None,
    }]


def test_opening_title_reads_story_field_and_enforces_twelve_character_contract():
    source_title = "效率越高，代价越大？额外标题"

    resolved = console._opening_title_from_story_payload(
        {"story": {"opening_title": source_title}},
    )

    assert resolved == source_title[:12]
    assert len(resolved) <= 12


def test_editing_page_contains_server_refresh_after_category_extraction():
    page = console._editing_page().decode("utf-8")
    director_packages = console._list_director_packages()
    assert [item["director_package_id"] for item in director_packages] == ["v1", "v2", "v3", "v3_2"]
    assert all(item["structure"] == "5+2" for item in director_packages)
    assert all(item["category_order"][-2:] == ["opening", "ending"] for item in director_packages)
    assert "重新从服务端读取当前风格包的最新子模板" in page
    assert "/api/editing/packaging-bundles/category-template" in page
    assert "__videoPackagingBuildRefreshBound" in page
    assert "dialog.close()" in page
    assert "window.location.reload()" in page
    assert "采集：特效" in page
    assert "componentSummary(item)" in page
    assert 'id="packagingExistingEmpty"' not in page
    assert 'id="packagingExistingSection"' in page
    assert "packaging-category-template-summary" in page
    assert 'id="packagingOverallStyle"' in page
    assert "/api/editing/packaging-bundles/full-draft-style" in page
    assert "/api/editing/packaging-bundles/full-draft-style-overrides" in page
    assert "整体覆盖特效（从头到尾）" in page
    assert "整体覆盖滤镜（从头到尾）" in page
    assert "字幕字体、位置、阴影和详细参数" in page
    assert "可直接微调识别值" in page
    assert "二级镜头分类接缝" in page
    assert "三级镜头分类接缝" in page
    assert "音效只跟随已启用的转场接缝" in page
    assert "二级镜头转场" in page
    assert "三级镜头转场" in page
    assert "视频素材内嵌音频" in page
    assert "音效：" in page
    assert "overall-editor-range" in page
    assert "data-ov-range-key" in page
    assert "overall-editor-transition-audio" in page
    assert "audio_companions" in page
    assert "实际生成按上游 transition_points 定位" in page
    assert "data-ov-kind" in page
    assert "packaging-style-edit" in page
    assert "修改这个风格包" in page
    assert "directOpenBound" in page
    assert "stopImmediatePropagation" in page
    assert "nextState" in page
    assert "state.textContent!==nextState" in page
    assert "new MutationObserver(()=>window.setTimeout(decorate,140)).observe" not in page
    assert "new MutationObserver(update).observe(grid" not in page
    assert "sourceConfig.hidden=true" in page
    assert "workspace.hidden=true" in page
    assert 'id="confirmPackagingBundleName"' not in page
    assert 'id="packagingBundleNameStatus"' in page
    assert "nameConfirmed" in page
    assert "/api/editing/packaging-bundles/rename" in page
    assert "selectedBundleName" in page
    assert "old_bundle_name:oldName" in page
    assert "build.addEventListener('click',saveRename,true)" in page
    assert "__packagingOriginalBundleName" in page
    assert "packaging-add-style-card" in page
    assert "已保存风格包" in page
    assert "新增风格包" in page
    assert ".packaging-config-section{margin-top:14px;padding:0;border:0;background:transparent}" in page
    assert "renamePending" in page
    assert "上游编导包" in page
    assert 'id="directorPackageSelect"' in page
    assert "选择编导包" in page
    assert "/api/editing/director-packages" in page
    assert "body.director_package" in page
    assert "__none__" in page
    assert "该分类选择“无”" in page or r"\u8be5\u5206\u7c7b\u9009\u62e9\u201c\u65e0\u201d" in page
    assert "草稿列表已读取，可按分类选择来源草稿；不需要配置时可选“无”。" not in page
    assert "已读取可提取母版候选，请选择新增子模板。" not in page
    assert "status.textContent='';status.classList.remove('show')" in page
    assert 'id="videoStylePackageSelect"' not in page
    assert "/api/editing/video-style-packages" not in page
    assert 'id="buildPackagingBundle" class="right" type="button">确定</button>' in page
    assert 'id="refreshPackagingDrafts"' not in page
    assert ".packaging-name-editor{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;align-items:end}" in page
    assert "build.textContent='确定'" in page or "build.textContent='\\u786e\\u5b9a'" in page
    assert "#videoStyleExtractTemplate,#packagingExtractNew,#buildPackagingBundle" not in page


def test_rename_video_packaging_bundle_updates_full_and_category_manifests(tmp_path, monkeypatch):
    root = tmp_path / "video_packaging_bundles"
    category_root = root / "_category_templates"
    full = root / "旧名称_v0.1"
    category = category_root / "旧名称_v0.5_image"
    full.mkdir(parents=True)
    category.mkdir(parents=True)
    (full / "bundle_manifest.json").write_text(
        json.dumps({"bundle_name": "旧名称", "version": "0.1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (category / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "bundle_name": "旧名称_image",
                "version": "0.5",
                "categories": [{"category": "image"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(console, "VIDEO_PACKAGING_BUNDLE_ROOT", root)

    result = console._rename_video_packaging_bundle(
        {"old_bundle_name": "旧名称", "bundle_name": "新名称", "version": "1.0"}
    )

    assert result["status"] == "renamed"
    assert result["renamed_count"] == 2
    renamed_full = root / "新名称_v0.1"
    renamed_category = category_root / "新名称_v0.5_image"
    assert renamed_full.is_dir()
    assert renamed_category.is_dir()
    assert json.loads((renamed_full / "bundle_manifest.json").read_text(encoding="utf-8"))["bundle_name"] == "新名称"
    assert json.loads((renamed_category / "bundle_manifest.json").read_text(encoding="utf-8"))["bundle_name"] == "新名称_image"


def test_rename_video_packaging_bundle_uses_internal_suffix_for_existing_target(tmp_path, monkeypatch):
    root = tmp_path / "video_packaging_bundles"
    category_root = root / "_category_templates"
    source = category_root / "旧名称_v0.1_image"
    existing = category_root / "v2_v0.1_image"
    source.mkdir(parents=True)
    existing.mkdir(parents=True)
    manifest = {"bundle_name": "旧名称_image", "version": "0.1", "categories": [{"category": "image"}]}
    (source / "bundle_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (existing / "bundle_manifest.json").write_text(
        json.dumps({"bundle_name": "v2_image", "version": "0.1", "categories": [{"category": "image"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(console, "VIDEO_PACKAGING_BUNDLE_ROOT", root)

    result = console._rename_video_packaging_bundle(
        {"old_bundle_name": "旧名称", "bundle_name": "v2", "version": "1.0"}
    )

    assert result["status"] == "renamed"
    renamed = category_root / "v2_v0.1_image__2"
    assert renamed.is_dir()
    assert json.loads((renamed / "bundle_manifest.json").read_text(encoding="utf-8"))["bundle_name"] == "v2_image"
    assert json.loads((existing / "bundle_manifest.json").read_text(encoding="utf-8"))["bundle_name"] == "v2_image"
