from __future__ import annotations

import base64
import io
import os
import json
import sys
from pathlib import Path

import pytest

from tools import run_locked_director_text_live as director_live
from tools import run_token_assets_live as assets


def test_image_failure_detail_preserves_provider_message_and_code():
    response = {
        "data": {
            "status": "failed",
            "error": {
                "code": "task_failed",
                "message": "The requested option isn't supported by the provider.",
            },
        }
    }

    assert assets._image_failure_detail(response) == (
        "The requested option isn't supported by the provider.（task_failed）"
    )


def test_tts_manifest_voice_key_resolves_to_catalogued_speaker_without_legacy_field():
    voice = assets._resolve_manifest_tts_voice({"voice_key": "m191_2"})

    assert voice["voice_key"] == "m191_2"
    assert voice["speaker_id"] == "zh_male_m191_uranus_bigtts"
    with pytest.raises(RuntimeError, match="已废弃的 tts_speaker_id"):
        assets._resolve_manifest_tts_voice({"tts_speaker_id": "zh_male_qingcang_uranus_bigtts"})


def test_generate_tts_page_mode_rejects_generic_story_key(tmp_path, monkeypatch):
    for name in (
        "ARK_TTS_API_KEY",
        "ARK_AUDIO_API_KEY",
        "VOLCENGINE_AUDIO_API_KEY",
        "ARK_TTS_BACKUP_API_KEY",
        "ARK_AUDIO_BACKUP_API_KEY",
        "VOLCENGINE_AUDIO_BACKUP_API_KEY",
        "API_MANAGEMENT_EXTRA_KEYS_TTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_TTS_AUTH_DOCUMENT", "")
    monkeypatch.setenv("ARK_API_KEY", "story-only-key")

    with pytest.raises(RuntimeError, match="TTS 主鉴权缺失"):
        assets.generate_tts(
            {"voice_key": "m191_2", "shots": [{"narration_text": "测试"}]},
            tmp_path / "page-credentials-only.md",
            tmp_path / "out",
        )


def test_generate_tts_ignores_legacy_document_even_when_present(tmp_path, monkeypatch):
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
        "API_MANAGEMENT_EXTRA_KEYS_TTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_TTS_AUTH_DOCUMENT", str(legacy))

    with pytest.raises(RuntimeError, match="TTS 主鉴权缺失"):
        assets.generate_tts(
            {"voice_key": "m191_2", "shots": [{"narration_text": "测试"}]},
            legacy,
            tmp_path / "out",
        )


def test_locked_director_cli_forwards_selected_voice_key_to_tts(tmp_path, monkeypatch):
    director_output = tmp_path / "director.json"
    director_output.write_text(
        json.dumps(
            {
                "evidence_level": "offline_test",
                "source_text_chars": 2,
                "director_output": {"segments": ["测试"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_live_tts(result, auth_document, output_dir, *, voice_key=""):
        captured.update(
            {
                "result": result,
                "auth_document": auth_document,
                "output_dir": output_dir,
                "voice_key": voice_key,
            }
        )
        return {
            "tts_contract": {
                "schema_version": "tts-voice-binding-v1",
                "voice": {"voice_key": voice_key, "speaker_id": "S_CUSTOM", "resource_id": "seed-icl-2.0", "model": "seed-tts-2.0-standard", "voice_type": "custom", "authorization_status": "verified", "training_status": "ready"},
                "audio": {"paths": [], "sha256": [], "actual_durations_s": []},
                "auth_slots": [],
                "voice_bindings": [],
                "timing_authority": "capcut_stt_required",
            },
            "timing_authority": "capcut_stt_required",
        }

    monkeypatch.setattr(director_live, "run_live_tts", fake_run_live_tts)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_locked_director_text_live.py",
            "--input",
            str(tmp_path / "input.txt"),
            "--auth-document",
            str(tmp_path / "auth.md"),
            "--output",
            str(tmp_path / "result.json"),
            "--director-output",
            str(director_output),
            "--tts-output-dir",
            str(tmp_path / "tts"),
            "--voice-key",
            "custom_demo_v1",
        ],
    )

    assert director_live.main() == 0
    assert captured["voice_key"] == "custom_demo_v1"
    saved = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert saved["tts"]["tts_contract"]["voice"]["voice_key"] == "custom_demo_v1"
    assert not any(key.startswith("tts_") for key in saved["tts"] if key != "tts_contract")


def test_generate_tts_records_cross_account_auth_slots_without_leaking_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ARK_TTS_API_KEY", "account-a-key")
    monkeypatch.setenv("ARK_TTS_BACKUP_API_KEY", "account-b-key")
    monkeypatch.setenv("ARK_TTS_RESOURCE_ID", "seed-icl-2.0")
    monkeypatch.setenv("ARK_TTS_AUTH_DOCUMENT", "")
    monkeypatch.setattr(
        assets,
        "_probe_written_audio_duration",
        lambda path: 1.5 if path.name == "voice_01.mp3" else 2.25,
    )
    calls: list[tuple[str, str, str, str]] = []

    class FakeTransport:
        def __init__(self, config):
            calls.append((config.api_key, config.backup_api_key, config.resource_id, config.model))
            self.index = 0

        def synthesize_local(self, request):
            self.index += 1
            calls.append(("request", request.speaker_id, request.resource_id or "", request.model or ""))
            return {
                "audio": f"audio-{self.index}".encode("utf-8"),
                "duration": 1.25,
                "metadata": {
                    "auth_slot": "backup" if self.index == 2 else "primary",
                    "voice_binding": {
                        "speaker_id": request.speaker_id,
                        "resource_id": request.resource_id,
                        "model": request.model,
                    },
                },
            }

    monkeypatch.setattr(assets, "ArkTTSHTTPTransport", FakeTransport)
    result = assets.generate_tts(
        {
            "voice_key": "m191_2",
            "shots": [{"narration_text": "第一句"}, {"narration_text": "第二句"}],
        },
        tmp_path / "auth.md",
        tmp_path / "out",
    )

    assert calls[0] == ("account-a-key", "account-b-key", "seed-tts-2.0", "seed-tts-2.0-standard")
    assert result["tts_contract"]["schema_version"] == "tts-voice-binding-v1"
    assert result["tts_contract"]["voice"] == {
        "voice_key": "m191_2",
        "speaker_id": "zh_male_m191_uranus_bigtts",
        "resource_id": "seed-tts-2.0",
        "model": "seed-tts-2.0-standard",
        "voice_type": "official",
        "authorization_status": "verified",
        "training_status": "ready",
    }
    assert result["tts_contract"]["audio"]["paths"] == [
        str((tmp_path / "out" / "audios" / "voice_01.mp3").resolve()),
        str((tmp_path / "out" / "audios" / "voice_02.mp3").resolve()),
    ]
    assert result["tts_contract"]["audio"]["actual_durations_s"] == [1.5, 2.25]
    assert result["tts_contract"]["auth_slots"] == ["primary", "backup"]
    assert result["tts_contract"]["voice_bindings"] == [
        {"speaker_id": "zh_male_m191_uranus_bigtts", "resource_id": "seed-tts-2.0", "model": "seed-tts-2.0-standard"},
        {"speaker_id": "zh_male_m191_uranus_bigtts", "resource_id": "seed-tts-2.0", "model": "seed-tts-2.0-standard"},
    ]
    assert not any(key.startswith("tts_") for key in result if key != "tts_contract")
    serialized = json.dumps(result, ensure_ascii=False)
    assert "account-a-key" not in serialized and "account-b-key" not in serialized


def test_generate_tts_custom_voice_never_uses_backup_account(tmp_path, monkeypatch):
    monkeypatch.setenv("ARK_TTS_API_KEY", "custom-primary-account")
    monkeypatch.setenv("ARK_TTS_BACKUP_API_KEY", "different-backup-account")
    monkeypatch.setattr(assets, "_probe_written_audio_duration", lambda path: 1.25)
    captured = {}

    class FakeTransport:
        def __init__(self, config):
            captured["config"] = config

        def synthesize_local(self, request):
            captured["request"] = request
            return {
                "audio": b"custom-audio",
                "duration": 1.25,
                "metadata": {
                    "auth_slot": "primary",
                    "voice_binding": {
                        "speaker_id": request.speaker_id,
                        "custom_speaker_id": request.custom_speaker_id,
                        "resource_id": request.resource_id,
                        "model": request.model,
                    },
                },
            }

    monkeypatch.setattr(assets, "ArkTTSHTTPTransport", FakeTransport)
    result = assets.generate_tts(
        {"voice_key": "custom_demo", "shots": [{"narration_text": "定制音色验收"}]},
        tmp_path / "page-credentials-only.md",
        tmp_path / "out",
        voice_override={
            "voice_key": "custom_demo",
            "speaker_id": "custom_speaker_id",
            "custom_speaker_id": "custom_zh_demo01",
            "voice_type": "custom",
            "resource_id": "seed-icl-2.0",
            "model": "seed-tts-2.0-standard",
            "authorization_status": "active",
            "training_status": "Active",
        },
    )

    config = captured["config"]
    assert config.api_key == "custom-primary-account"
    assert config.backup_api_key == ""
    assert config.extra_api_keys == ()
    assert config.failover_strategy == "primary_only"
    assert captured["request"].speaker_id == "custom_zh_demo01"
    assert result["tts_contract"]["auth_slots"] == ["primary"]


def test_locked_manifest_preserves_account_slot_and_voice_binding_without_keys(tmp_path):
    audio_paths = []
    for index in range(3):
        audio = tmp_path / f"voice_{index + 1:02d}.mp3"
        audio.write_bytes(f"audio-{index}".encode("utf-8"))
        audio_paths.append(str(audio))
    input_path = tmp_path / "approved_copy.json"
    input_path.write_text(json.dumps({"text": "第一句。第二句。第三句。"}, ensure_ascii=False), encoding="utf-8")
    segments = ["第一句。", "第二句。", "第三句。"]
    timelines = [{"start": index * 1_000_000, "end": (index + 1) * 1_000_000} for index in range(3)]
    director_result = {
        "director_output": {
            "segments": segments,
            "segment_beats": [
                {"segment_index": index, "segment_text": text, "rhythm": "推进", "segment_goal": "完成段落", "beats": [{"route_candidates": ["scene"]}]}
                for index, text in enumerate(segments)
            ],
        },
        "tts": {
            "tts_paths": audio_paths,
            "tts_voice_key": "custom_demo_v1",
            "tts_speaker_id": "S_CUSTOM",
            "tts_resource_id": "seed-icl-2.0",
            "tts_model": "seed-tts-2.0-standard",
            "tts_audio_sha256": ["hash-a", "hash-b", "hash-c"],
            "tts_actual_durations": [1.0, 1.0, 1.0],
            "tts_auth_slots": ["backup", "backup", "primary"],
            "tts_voice_bindings": [
                {"speaker_id": "S_CUSTOM", "resource_id": "seed-icl-2.0", "model": "seed-tts-2.0-standard"}
            ] * 3,
        },
        "subtitle": {
            "pipeline": {"status": "succeeded", "timeline_source": "capcut_stt"},
            "group_timelines": timelines,
            "total_timeline": {"start": 0, "end": 3_000_000},
            "new_segments": segments,
            "new_timelines": timelines,
        },
        "shot_refinement": {
            "small_shot_count": 3,
            "output": {
                "Code_list": [
                    {
                        "shots": [{"source_text": text, "narration_text": text, "clip_role": "推进", "story_beat": "动作"}],
                        "timelines": [timeline],
                    }
                    for text, timeline in zip(segments, timelines, strict=True)
                ],
            },
        },
        "story_draft": {
            "story": {
                "movie_outline": {
                    "protagonist": "测试角色",
                    "protagonist_goal": "完成任务",
                    "central_conflict": "资源不足",
                    "character_arc": "做出选择",
                    "ending_hook": "灯光亮起",
                },
                "silent_story_text": "测试角色在三段行动中完成任务。",
                "scene_groups": [
                    {"scene_id": f"scene_{index + 1:02d}", "group_ids": [f"g{index + 1:02d}"], "scene_purpose": "推进", "state_before": "开始", "visible_conflict": "阻力", "turn": "继续", "state_after": "前进"}
                    for index in range(3)
                ],
                "narration_mappings": [
                    {"group_id": f"g{index + 1:02d}", "scene_id": f"scene_{index + 1:02d}", "semantic_mapping": "对应行动", "silent_action": "角色行动", "metaphor": "前进", "state_before": "开始", "state_after": "继续", "bridge_to_next": "下一段"}
                    for index in range(3)
                ],
            },
        },
    }

    locked = director_live.build_approved_director_lock(director_result, input_path)

    narration_assets = locked["narration_assets"]
    assert [item["auth_slot"] for item in narration_assets] == ["backup", "backup", "primary"]
    assert all(item["voice_binding"] == {
        "speaker_id": "S_CUSTOM", "resource_id": "seed-icl-2.0", "model": "seed-tts-2.0-standard"
    } for item in narration_assets)
    assert "account-a-key" not in json.dumps(locked, ensure_ascii=False)
    assert "account-b-key" not in json.dumps(locked, ensure_ascii=False)


def test_generate_images_submits_and_queries_in_parallel_with_live_progress(tmp_path, monkeypatch):
    created: list[str] = []
    queried: list[str] = []
    events: list[dict] = []

    def fake_create(_request, _targets):
        task_id = f"task-{len(created) + 1}"
        created.append(task_id)
        return {"task_id": task_id, "state": "submitted", "image_urls": [], "image_b64": []}

    def fake_query(request, _targets):
        task_id = str(request["task_id"])
        queried.append(task_id)
        return {"task_id": task_id, "state": "completed", "image_urls": [f"https://example.test/{task_id}.jpg"], "image_b64": []}

    def fake_download(_url: str, path: Path, _minimum: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")

    monkeypatch.setattr(assets, "_load_image2_runtime_targets", lambda _path: [{"channel_id": "image2", "api_keys": ["key"]}])
    monkeypatch.setattr(assets, "_image2_create_with_failover", fake_create)
    monkeypatch.setattr(assets, "_image2_query_with_failover", fake_query)
    monkeypatch.setattr(assets, "_download", fake_download)

    result = assets.generate_images(
        {"shots": [{"shot_id": f"s{index}", "first_frame_prompt": "画面"} for index in range(1, 6)]},
        tmp_path / "auth.md",
        tmp_path / "out",
        progress_reporter=events.append,
    )

    assert set(queried) == set(created)
    assert len(result["image_task_ids"]) == 5
    assert all(result["image_task_ids"])
    assert len(result["image_paths"]) == 5
    assert [event["submitted"] for event in events if event["stage"] == "submitted"] == [1, 2, 3, 4, 5]
    assert events[-1]["stage"] == "completed"
    assert events[-1]["completed"] == 5


def test_generate_images_as_grids_never_falls_back_to_legacy_tasks(tmp_path, monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(assets, "_load_image2_runtime_targets", lambda _path: [{"channel_id": "image2", "api_keys": ["key"]}])
    monkeypatch.setattr(assets, "_generate_images_as_grids_sync", lambda *args, **kwargs: calls.append("image2") or {})
    monkeypatch.setattr(assets, "_generate_images_as_grids_legacy", lambda *args, **kwargs: calls.append("legacy") or {})

    assets.generate_images_as_grids(
        {"shots": [{"shot_id": "s1", "first_frame_prompt": "画面"}]},
        tmp_path / "auth.md",
        tmp_path / "out",
    )

    assert calls == ["image2"]


def test_async_grid_persists_task_id_and_queries_after_creation(tmp_path, monkeypatch):
    """创建响应返回后必须先落盘句柄，再进入同 task_id 查询。"""
    image = pytest.importorskip("PIL.Image")
    source = tmp_path / "provider-grid.jpg"
    image.new("RGB", (200, 200), (20, 30, 40)).save(source, format="JPEG")
    created: list[str] = []
    created_requests: list[dict] = []
    queried: list[dict] = []

    def fake_create(_request, _targets):
        created_requests.append(dict(_request))
        task_id = "provider-grid-task-1"
        created.append(task_id)
        return {
            "task_id": task_id,
            "state": "submitted",
            "image_urls": [],
            "image_b64": [],
            "channel_id": "image2",
            "endpoint": "https://image2.example/v1/",
            "model_id": "gpt-image-2-all",
            "key_index": 1,
        }

    def fake_query(request, _targets):
        queried.append(dict(request))
        return {
            "task_id": request["task_id"],
            "state": "completed",
            "image_urls": ["https://provider.example/grid.jpg"],
            "image_b64": [],
            "channel_id": request.get("channel_id"),
        }

    monkeypatch.setattr(assets, "_image2_create_with_failover", fake_create)
    monkeypatch.setattr(assets, "_image2_query_with_failover", fake_query)
    monkeypatch.setattr(assets, "_download", lambda _url, path, _minimum: path.write_bytes(source.read_bytes()) if path.parent.mkdir(parents=True, exist_ok=True) is None else None)
    monkeypatch.setattr(
        assets,
        "publish_existing_grid_images",
        lambda paths, *_args, **_kwargs: [f"https://cdn.example/{Path(path).name}" for path in paths],
    )

    checkpoint = tmp_path / "first_frame_checkpoint.json"
    result = assets._generate_images_as_grids_sync(
        {
            "aspect_ratio": "1:1",
            "shots": [{
                "shot_id": "s1",
                "first_frame_prompt": "画面",
                "ref_images": ["https://reference.example/style.png"],
            }],
        },
        tmp_path / "auth.md",
        tmp_path / "out",
        [{"channel_id": "image2", "api_keys": ["key"]}],
        grid_layout="2x2",
        checkpoint_path=checkpoint,
    )

    assert created == ["provider-grid-task-1"]
    assert created_requests[0]["image_urls"] == ["https://reference.example/style.png"]
    assert queried == [{
        "task_id": "provider-grid-task-1",
        "channel_id": "image2",
        "key_index": 1,
        "endpoint": "https://image2.example/v1/",
        "model_id": "gpt-image-2-all",
    }]
    checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert checkpoint_data["batches"]["first_frame_grid_001"]["task_id"] == "provider-grid-task-1"
    assert result["image_task_mode"] == "asynchronous"


def test_async_grid_can_skip_tos_publish_when_requested(tmp_path, monkeypatch):
    """显式关闭发布时只创建、查询、下载和裁切，不触发 TOS 发布。"""
    image = pytest.importorskip("PIL.Image")
    source = tmp_path / "provider-grid.jpg"
    image.new("RGB", (1024, 1024), (20, 30, 40)).save(source, format="JPEG")
    create_calls: list[str] = []

    def fake_create(_request, _targets):
        create_calls.append("create")
        return {
            "task_id": "provider-grid-task-no-publish",
            "state": "submitted",
            "image_urls": [],
            "image_b64": [],
            "channel_id": "image2",
            "endpoint": "https://image2.example/v1/",
            "model_id": "gpt-image-2-all",
            "key_index": 1,
        }

    monkeypatch.setattr(assets, "_image2_create_with_failover", fake_create)
    monkeypatch.setattr(
        assets,
        "_image2_query_with_failover",
        lambda request, _targets: {
            "task_id": request["task_id"],
            "state": "completed",
            "image_urls": ["https://provider.example/grid.jpg"],
            "image_b64": [],
            "channel_id": request.get("channel_id"),
        },
    )
    monkeypatch.setattr(
        assets,
        "_download",
        lambda _url, path, _minimum: path.parent.mkdir(parents=True, exist_ok=True) or path.write_bytes(source.read_bytes()),
    )

    def unexpected_publish(*_args, **_kwargs):
        raise AssertionError("publish=False 不应调用 TOS 图片发布")

    monkeypatch.setattr(assets, "publish_existing_grid_images", unexpected_publish)
    result = assets._generate_images_as_grids_sync(
        {"aspect_ratio": "1:1", "shots": [{"shot_id": "s1", "first_frame_prompt": "画面"}]},
        tmp_path / "auth.md",
        tmp_path / "out",
        [{"channel_id": "image2", "api_keys": ["key"]}],
        grid_layout="2x2",
        publish=False,
    )

    assert create_calls == ["create"]
    assert result["image_urls"] == []
    assert Path(result["image_paths"][0]).is_file()


def test_generate_images_uses_image2_transport_even_without_runtime_env(tmp_path, monkeypatch):
    calls: list[str] = []

    monkeypatch.delenv(assets.IMAGE2_RUNTIME_TARGETS_ENV, raising=False)
    monkeypatch.setattr(assets, "_load_image2_runtime_targets", lambda _path: [{"channel_id": "image2", "api_keys": ["key"]}])
    monkeypatch.setattr(assets, "_generate_images_sync", lambda *args, **kwargs: calls.append("image2") or {})
    monkeypatch.setattr(assets, "_generate_images_legacy", lambda *args, **kwargs: calls.append("legacy") or {})

    assets.generate_images(
        {"shots": [{"shot_id": "s1", "first_frame_prompt": "画面"}]},
        tmp_path / "auth.md",
        tmp_path / "out",
    )

    assert calls == ["image2"]


def test_publish_existing_grid_images_resumes_from_checkpoint(tmp_path, monkeypatch):
    paths = []
    for index in range(3):
        path = tmp_path / f"frame-{index}.jpg"
        path.write_bytes(b"image")
        paths.append(path)
    published: list[str] = []
    monkeypatch.setattr(assets, "_publish_reference", lambda path, _auth: published.append(path.name) or f"https://example.test/{path.name}")
    state_path = tmp_path / "publish_state.json"
    assets._save(
        state_path,
        {"image_paths": [str(path) for path in paths], "image_urls": ["https://example.test/frame-0.jpg", "", ""]},
    )

    urls = assets.publish_existing_grid_images(paths, tmp_path / "auth.md", state_path)

    assert published == ["frame-1.jpg", "frame-2.jpg"]
    assert all(url.startswith("https://example.test/") for url in urls)


def test_sync_grid_b64_result_is_durable_before_checkpoint_resume(tmp_path, monkeypatch):
    image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    image.new("RGB", (200, 200), (20, 30, 40)).save(buffer, format="JPEG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    calls = []

    monkeypatch.setattr(
        assets,
        "_image2_create_with_failover",
        lambda _request, _targets: calls.append("create") or {"image_b64": [encoded], "channel_id": "test"},
    )
    monkeypatch.setattr(
        assets,
        "publish_existing_grid_images",
        lambda paths, *_args, **_kwargs: [f"https://example.test/{Path(path).name}" for path in paths],
    )
    manifest = {"aspect_ratio": "1:1", "shots": [{"shot_id": "s1", "first_frame_prompt": "镜头"}]}
    checkpoint = tmp_path / "first_frame_checkpoint.json"
    first = assets._generate_images_as_grids_sync(
        manifest,
        tmp_path / "auth.md",
        tmp_path / "out",
        [{"channel_id": "test", "api_keys": ["key"]}],
        grid_layout="2x2",
        checkpoint_path=checkpoint,
    )
    assert calls == ["create"]
    assert checkpoint.is_file()
    assert list((tmp_path / "out" / "grids" / ".provider").glob("*.jpg"))

    monkeypatch.setattr(
        assets,
        "_image2_create_with_failover",
        lambda *_args: (_ for _ in ()).throw(AssertionError("恢复同步 b64 结果时不得重新调用 Image2")),
    )
    second = assets._generate_images_as_grids_sync(
        manifest,
        tmp_path / "auth.md",
        tmp_path / "out",
        [{"channel_id": "test", "api_keys": ["key"]}],
        grid_layout="2x2",
        checkpoint_path=checkpoint,
        resume=True,
    )
    assert second["image_paths"] == first["image_paths"]


def test_publish_existing_grid_images_force_refreshes_cached_urls(tmp_path, monkeypatch):
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"image")
    published: list[str] = []
    monkeypatch.setattr(assets, "_publish_reference", lambda source, _auth: published.append(source.name) or "https://example.test/fresh.jpg")
    state_path = tmp_path / "publish_state.json"
    assets._save(state_path, {"image_paths": [str(path)], "image_urls": ["https://example.test/expired.jpg"]})

    urls = assets.publish_existing_grid_images(
        [path], tmp_path / "auth.md", state_path, force_refresh=True,
    )

    assert published == ["frame.jpg"]
    assert urls == ["https://example.test/fresh.jpg"]


def test_publish_reference_preserves_api_management_tos_environment(tmp_path, monkeypatch):
    source = tmp_path / "frame.jpg"
    source.write_bytes(b"image")
    runtime_values = {
        "ARK_TTS_TOS_ACCESS_KEY": "page-ak",
        "ARK_TTS_TOS_SECRET_KEY": "page-sk",
        "ARK_TTS_TOS_BUCKET": "page-bucket",
        "ARK_TTS_TOS_ENDPOINT": "https://page-endpoint.example",
        "ARK_TTS_TOS_REGION": "page-region",
        "ARK_TTS_TOS_BACKUP_ACCESS_KEY": "backup-ak",
        "ARK_TTS_TOS_BACKUP_SECRET_KEY": "backup-sk",
        "ARK_TTS_TOS_BACKUP_BUCKET": "backup-bucket",
        "ARK_TTS_TOS_BACKUP_ENDPOINT": "https://backup-endpoint.example",
        "ARK_TTS_TOS_BACKUP_REGION": "backup-region",
    }
    for name, value in runtime_values.items():
        monkeypatch.setenv(name, value)

    monkeypatch.setattr(
        assets,
        "load_infinite_talk_auth_from_document",
        lambda _path: {
            "tos_access_key": "document-ak",
            "tos_secret_key": "document-sk",
            "tos_bucket": "document-bucket",
            "tos_endpoint": "https://document-endpoint.example",
            "tos_region": "document-region",
        },
    )
    captured: dict[str, object] = {}

    class FakePublisher:
        @classmethod
        def from_env(cls):
            captured.update({name: os.environ.get(name, "") for name in runtime_values})
            return cls()

        def __call__(self, *_args, **_kwargs):
            return {"url": "https://page-endpoint.example/published.jpg"}

    monkeypatch.setattr(assets, "TOSAudioPublisher", FakePublisher)

    assert assets._publish_reference(source, tmp_path / "auth.md") == "https://page-endpoint.example/published.jpg"
    assert captured == runtime_values


def test_generate_videos_preserves_successful_files_when_one_shot_query_fails(tmp_path, monkeypatch):
    events: list[dict] = []
    created_events: list[dict] = []
    camera_flags: list[bool] = []

    class FakeTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        def generate(self, params):
            camera_flags.append(params["camera_fixed"])
            return {"task_id": f"task-{params['prompt']}"}

        def query(self, params):
            if params["task_id"].endswith("bad"):
                return {"success": False, "status": "failed", "message": "provider rejected request"}
            return {"success": True, "public_video_url": "https://example.test/good.mp4"}

    def fake_download(_url: str, path: Path, _minimum: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")

    monkeypatch.setattr(assets, "load_seedance_api_keys_from_auth_document", lambda _path: ["primary", "backup"])
    monkeypatch.setattr(assets, "load_infinite_talk_auth_from_document", lambda _path: {"tos_access_key": "ak", "tos_secret_key": "sk", "tos_bucket": "bucket", "tos_endpoint": "", "tos_region": "cn-beijing"})
    monkeypatch.setattr(assets, "SeedanceHTTPTransport", FakeTransport)
    monkeypatch.setattr(assets, "_download", fake_download)

    result = assets.generate_videos(
        {"shots": [
            {"shot_id": "good", "prompt": "good", "duration_ms": 4000},
            {"shot_id": "bad", "prompt": "bad", "duration_ms": 4000},
        ]},
        {"image_urls": ["https://example.test/one.jpg", "https://example.test/two.jpg"]},
        tmp_path / "auth.md", tmp_path / "out",
        progress_reporter=events.append,
        task_created_reporter=created_events.append,
    )

    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 1
    assert result["shot_results"][0]["status"] == "succeeded"
    assert Path(result["video_paths"][0]).is_file()
    assert result["shot_results"][1]["error"] == "视频查询未成功：provider rejected request"
    assert result["shot_results"][1]["provider_diagnostic"]["query"] == {
        "success": False, "status": "failed", "message": "provider rejected request",
    }
    assert [event["completed"] for event in events] == [1, 2]
    assert {event["shot_id"] for event in created_events} == {"good", "bad"}
    assert {event["task_id"] for event in created_events} == {"task-good", "task-bad"}
    assert {event["status"] for event in created_events} == {"submitted"}
    assert len(camera_flags) == 3  # 失败镜头按既有策略重试一次
    assert all(camera_flags)


def test_generate_videos_resume_queries_persisted_task_without_creating_duplicate(tmp_path, monkeypatch):
    calls = {"generate": 0, "query": 0}

    class FakeTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        def generate(self, _params):
            calls["generate"] += 1
            raise AssertionError("恢复已有 task_id 时不得重新创建视频任务")

        def query(self, params):
            calls["query"] += 1
            assert params["task_id"] == "provider-task-1"
            return {"success": True, "public_video_url": "https://example.test/resumed.mp4"}

    def fake_download(_url: str, path: Path, _minimum: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")

    monkeypatch.setattr(assets, "load_seedance_api_keys_from_auth_document", lambda _path: ["primary"])
    monkeypatch.setattr(assets, "load_infinite_talk_auth_from_document", lambda _path: {"tos_access_key": "ak", "tos_secret_key": "sk", "tos_bucket": "bucket", "tos_endpoint": "", "tos_region": "cn-beijing"})
    monkeypatch.setattr(assets, "SeedanceHTTPTransport", FakeTransport)
    monkeypatch.setattr(assets, "_download", fake_download)

    result = assets.generate_videos(
        {"shots": [{"shot_id": "s1", "prompt": "镜头", "duration_ms": 4000}]},
        {"image_urls": ["https://example.test/frame.jpg"]},
        tmp_path / "auth.md", tmp_path / "out",
        existing_records=[{"shot_id": "s1", "task_id": "provider-task-1", "status": "submitted"}],
        resume=True,
    )

    assert calls == {"generate": 0, "query": 1}
    assert result["shot_results"][0]["status"] == "succeeded"
    assert result["shot_results"][0]["resumed_from_checkpoint"] is True
    assert Path(result["video_paths"][0]).is_file()


def test_generate_videos_resume_reuses_existing_local_video_without_provider_calls(tmp_path, monkeypatch):
    local_video = tmp_path / "existing.mp4"
    local_video.write_bytes(b"existing")
    calls = {"generate": 0, "query": 0}

    class FakeTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        def generate(self, _params):
            calls["generate"] += 1
            raise AssertionError("已有本地视频时不得重新创建")

        def query(self, _params):
            calls["query"] += 1
            raise AssertionError("已有本地视频时不得查询")

    monkeypatch.setattr(assets, "load_seedance_api_keys_from_auth_document", lambda _path: ["primary"])
    monkeypatch.setattr(assets, "load_infinite_talk_auth_from_document", lambda _path: {"tos_access_key": "ak", "tos_secret_key": "sk", "tos_bucket": "bucket", "tos_endpoint": "", "tos_region": "cn-beijing"})
    monkeypatch.setattr(assets, "SeedanceHTTPTransport", FakeTransport)

    result = assets.generate_videos(
        {"shots": [{"shot_id": "s1", "prompt": "镜头", "duration_ms": 4000}]},
        {"image_urls": ["https://example.test/frame.jpg"]},
        tmp_path / "auth.md", tmp_path / "out",
        existing_records=[{"shot_id": "s1", "task_id": "provider-task-1", "status": "succeeded", "video_path": str(local_video)}],
        resume=True,
    )

    assert calls == {"generate": 0, "query": 0}
    assert result["shot_results"][0]["status"] == "succeeded"
    assert Path(result["shot_results"][0]["video_path"]).resolve() == local_video.resolve()


def test_generate_videos_retries_one_unexplained_provider_failure(tmp_path, monkeypatch):
    calls: list[str] = []

    class FakeTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        def generate(self, _params):
            task_id = f"task-{len(calls) + 1}"
            calls.append(task_id)
            return {"task_id": task_id}

        def query(self, params):
            if params["task_id"] == "task-1":
                return {"success": False, "status": "生成失败", "raw_error": ""}
            return {"success": True, "public_video_url": "https://example.test/retried.mp4"}

    def fake_download(_url: str, path: Path, _minimum: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")

    monkeypatch.setattr(assets, "load_seedance_api_keys_from_auth_document", lambda _path: ["primary", "backup"])
    monkeypatch.setattr(assets, "load_infinite_talk_auth_from_document", lambda _path: {"tos_access_key": "ak", "tos_secret_key": "sk", "tos_bucket": "bucket", "tos_endpoint": "", "tos_region": "cn-beijing"})
    monkeypatch.setattr(assets, "SeedanceHTTPTransport", FakeTransport)
    monkeypatch.setattr(assets, "_download", fake_download)
    monkeypatch.setattr(assets.time, "sleep", lambda _seconds: None)

    result = assets.generate_videos(
        {"shots": [{"shot_id": "retry", "prompt": "retry", "duration_ms": 4000}]},
        {"image_urls": ["https://example.test/frame.jpg"]},
        tmp_path / "auth.md", tmp_path / "out",
    )

    record = result["shot_results"][0]
    assert result["succeeded_count"] == 1
    assert record["attempt_count"] == 2
    assert record["attempts"][0]["error"] == "视频查询未成功：生成失败"


def test_generate_videos_uses_three_to_five_second_aigc_duration_window(tmp_path, monkeypatch):
    durations: list[int] = []

    class FakeTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        def generate(self, params):
            durations.append(params["duration"])
            return {"task_id": "task-short"}

        def query(self, _params):
            return {"success": True, "public_video_url": "https://example.test/short.mp4"}

    monkeypatch.setattr(assets, "load_seedance_api_keys_from_auth_document", lambda _path: ["primary", "backup"])
    monkeypatch.setattr(assets, "load_infinite_talk_auth_from_document", lambda _path: {"tos_access_key": "ak", "tos_secret_key": "sk", "tos_bucket": "bucket", "tos_endpoint": "", "tos_region": "cn-beijing"})
    monkeypatch.setattr(assets, "SeedanceHTTPTransport", FakeTransport)
    monkeypatch.setattr(assets, "_download", lambda _url, path, _minimum: (path.parent.mkdir(parents=True, exist_ok=True), path.write_bytes(b"video")))

    assets.generate_videos(
        {"shots": [{"shot_id": "short", "prompt": "short", "duration_ms": 3000}]},
        {"image_urls": ["https://example.test/frame.jpg"]},
        tmp_path / "auth.md", tmp_path / "out",
    )

    assert durations == [4]


def test_generate_videos_uses_provider_safe_duration_for_real_timeline_fraction(tmp_path, monkeypatch):
    durations: list[int] = []

    class FakeTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        def generate(self, params):
            durations.append(params["duration"])
            return {"task_id": "task-3497"}

        def query(self, _params):
            return {"success": True, "public_video_url": "https://example.test/3497.mp4"}

    monkeypatch.setattr(assets, "load_seedance_api_keys_from_auth_document", lambda _path: ["primary"])
    monkeypatch.setattr(assets, "load_infinite_talk_auth_from_document", lambda _path: {"tos_access_key": "ak", "tos_secret_key": "sk", "tos_bucket": "bucket", "tos_endpoint": "", "tos_region": "cn-beijing"})
    monkeypatch.setattr(assets, "SeedanceHTTPTransport", FakeTransport)
    monkeypatch.setattr(assets, "_download", lambda _url, path, _minimum: (path.parent.mkdir(parents=True, exist_ok=True), path.write_bytes(b"video")))

    assets.generate_videos(
        {"shots": [{"shot_id": "g02_s02", "prompt": "retry", "duration_ms": 3497}]},
        {"image_urls": ["https://example.test/frame.jpg"]},
        tmp_path / "auth.md", tmp_path / "out",
    )

    assert durations == [4]


def test_provider_duration_rounds_up_to_supported_integer_seconds():
    assert assets._provider_duration_seconds(3_000) == 4
    assert assets._provider_duration_seconds(3_497) == 4
    assert assets._provider_duration_seconds(4_001) == 5
    assert assets._provider_duration_seconds(5_000) == 5
    assert assets._provider_duration_seconds(5_408) == 6


def test_generate_video_one_uses_provider_safe_duration_for_retry(tmp_path, monkeypatch):
    durations: list[int] = []

    class FakeTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        def generate(self, params):
            durations.append(params["duration"])
            return {"task_id": "retry-task-3497"}

        def query(self, _params):
            return {"success": True, "public_video_url": "https://example.test/retry-3497.mp4"}

    monkeypatch.setattr(assets, "load_seedance_api_keys_from_auth_document", lambda _path: ["primary"])
    monkeypatch.setattr(assets, "load_infinite_talk_auth_from_document", lambda _path: {"tos_access_key": "ak", "tos_secret_key": "sk", "tos_bucket": "bucket", "tos_endpoint": "", "tos_region": "cn-beijing"})
    monkeypatch.setattr(assets, "SeedanceHTTPTransport", FakeTransport)
    monkeypatch.setattr(assets, "_download", lambda _url, path, _minimum: (path.parent.mkdir(parents=True, exist_ok=True), path.write_bytes(b"video")))

    result = assets.generate_video_one(
        {"shots": [{"shot_id": "g02_s02", "prompt": "retry", "duration_ms": 3497}]},
        {"image_urls": ["https://example.test/frame.jpg"]},
        tmp_path / "auth.md",
        tmp_path / "out",
        0,
    )

    assert durations == [4]
    assert result["provider_duration_seconds"] == 4


def test_generate_videos_rejects_subthree_second_shot_before_provider_call(tmp_path, monkeypatch):
    provider_calls: list[dict] = []

    class FakeTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        def generate(self, params):
            provider_calls.append(params)
            return {"task_id": "should-not-run"}

    monkeypatch.setattr(assets, "SeedanceHTTPTransport", FakeTransport)

    with pytest.raises(ValueError, match="必须使用图片"):
        assets.generate_videos(
            {"shots": [{"shot_id": "short", "prompt": "short", "duration_ms": 2_500}]},
            {"image_urls": ["https://example.test/frame.jpg"]},
            tmp_path / "auth.md", tmp_path / "out",
        )

    assert provider_calls == []


def test_generate_videos_allows_short_full_video_first_shot_and_uses_provider_safe_duration(tmp_path, monkeypatch):
    durations: list[int] = []

    class FakeTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        def generate(self, params):
            durations.append(params["duration"])
            return {"task_id": "first-shot-task"}

        def query(self, _params):
            return {"success": True, "public_video_url": "https://example.test/first-shot.mp4"}

    monkeypatch.setattr(assets, "load_seedance_api_keys_from_auth_document", lambda _path: ["primary"])
    monkeypatch.setattr(assets, "load_infinite_talk_auth_from_document", lambda _path: {"tos_access_key": "ak", "tos_secret_key": "sk", "tos_bucket": "bucket", "tos_endpoint": "", "tos_region": "cn-beijing"})
    monkeypatch.setattr(assets, "SeedanceHTTPTransport", FakeTransport)
    monkeypatch.setattr(assets, "_download", lambda _url, path, _minimum: (path.parent.mkdir(parents=True, exist_ok=True), path.write_bytes(b"video")))

    result = assets.generate_videos(
        {"shots": [{"shot_id": "g01_s01", "prompt": "首镜", "duration_ms": 2_500}]},
        {"image_urls": ["https://example.test/frame.jpg"]},
        tmp_path / "auth.md", tmp_path / "out",
    )

    assert durations == [4]
    assert result["shot_results"][0]["requested_duration_ms"] == 2_500
    assert result["shot_results"][0]["provider_duration_seconds"] == 4


def test_generate_videos_switches_to_backup_after_primary_inference_limit(tmp_path, monkeypatch):
    used_keys: list[str] = []

    class FakeTransport:
        def __init__(self, config, **_kwargs):
            self.key = config.primary_api_key

        def generate(self, _params):
            used_keys.append(self.key)
            return {"task_id": f"task-{self.key}"}

        def query(self, _params):
            if self.key == "primary":
                return {"success": False, "status": "生成失败", "raw_error": "SetLimitExceeded: inference limit"}
            return {"success": True, "public_video_url": "https://example.test/backup.mp4"}

    monkeypatch.setattr(assets, "load_seedance_api_keys_from_auth_document", lambda _path: ["primary", "backup"])
    monkeypatch.setattr(assets, "load_infinite_talk_auth_from_document", lambda _path: {"tos_access_key": "ak", "tos_secret_key": "sk", "tos_bucket": "bucket", "tos_endpoint": "", "tos_region": "cn-beijing"})
    monkeypatch.setattr(assets, "SeedanceHTTPTransport", FakeTransport)
    monkeypatch.setattr(assets, "_download", lambda _url, path, _minimum: (path.parent.mkdir(parents=True, exist_ok=True), path.write_bytes(b"video")))
    monkeypatch.setattr(assets.time, "sleep", lambda _seconds: None)

    result = assets.generate_videos(
        {"shots": [{"shot_id": "limit", "prompt": "limit", "duration_ms": 4000}]},
        {"image_urls": ["https://example.test/frame.jpg"]},
        tmp_path / "auth.md", tmp_path / "out",
    )

    assert result["succeeded_count"] == 1
    assert used_keys == ["primary", "backup"]
