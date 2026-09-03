from __future__ import annotations

import base64
import json
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tools.video_production_console as console
from workflow_1256.knowledge_source_registry import KnowledgeSourceRegistry
from workflow_1256.style_package_store import StylePackageStore


def _request(server: ThreadingHTTPServer, path: str, *, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{server.server_port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urlopen(request) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


@pytest.fixture()
def route_server(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", StylePackageStore(tmp_path / "styles"))
    monkeypatch.setattr(console, "OBSIDIAN_CONFIG_PATH", tmp_path / "obsidian-config.json")
    monkeypatch.setenv("ACCOUNT_KNOWLEDGE_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("KNOWLEDGE_SOURCE_REGISTRY_PATH", str(tmp_path / "sources.json"))
    # Force the lazy registry accessor to use the fixture path immediately.
    monkeypatch.setattr(console, "KNOWLEDGE_SOURCE_REGISTRY", KnowledgeSourceRegistry(tmp_path / "sources.json"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_detection_routes_are_conservative(route_server):
    status, payload = _request(route_server, "/api/knowledge-sources/detect?value=https%3A%2F%2Fwww.douyin.com%2Fuser%2Fabc")
    assert status == 200
    assert payload["detection"]["platform"] == "douyin"
    assert payload["detection"]["material_type"] == "profile"

    status, payload = _request(route_server, "/api/knowledge-sources/detect?value=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Fexample")
    assert status == 200
    assert payload["detection"]["material_type"] == "article"

    status, payload = _request(route_server, "/api/knowledge-sources/detect?value=https%3A%2F%2Fexample.com%2Funknown")
    assert status == 200
    assert payload["detection"]["detection_status"] == "pending_confirmation"
    assert payload["detection"]["platform"] == "unknown"


def test_douyin_profile_share_text_is_not_routed_to_video(route_server):
    share_text = "长按复制此条消息，打开抖音搜索，查看 TA 的更多作品。 https://v.douyin.com/profile-share/"
    status, payload = _request(
        route_server,
        "/api/knowledge-sources/detect?value=" + quote(share_text),
    )
    assert status == 200
    assert payload["detection"]["platform"] == "douyin"
    assert payload["detection"]["material_type"] == "profile"
    assert payload["detection"]["detection_status"] == "detected"


def test_douyin_short_link_without_target_hint_stays_pending(route_server):
    status, payload = _request(
        route_server,
        "/api/knowledge-sources/detect?value=https%3A%2F%2Fv.douyin.com%2Fambiguous%2F",
    )
    assert status == 200
    assert payload["detection"]["platform"] == "douyin"
    assert payload["detection"]["material_type"] == "unknown"
    assert payload["detection"]["detection_status"] == "pending_confirmation"


def test_writing_page_exposes_real_source_registration_progress(route_server):
    with urlopen(f"http://127.0.0.1:{route_server.server_port}/writing") as response:
        page = response.read().decode("utf-8")

    # 抖音等平台的分享入口经常是“说明文字 + 短链”，不能使用浏览器
    # ``type=url`` 原生校验，否则登记按钮会被错误阻断。
    assert 'id="writingSourceUrl" type="text" inputmode="url"' in page
    assert "链接或完整分享文案" in page
    assert 'id="writingSourceRegisterProgress"' in page
    assert 'aria-label="知识源登记进度，仅代表第 1 步"' in page
    assert "正在自动识别资料并登记来源…" in page
    assert "已收到登记结果，正在核验 Obsidian 落盘…" in page
    assert "登记完成：已写入 Obsidian 待处理区。" in page
    assert "登记失败：" in page


def test_register_preserves_profile_semantics_from_share_text(route_server):
    share_text = "打开抖音搜索，查看TA的更多作品。 https://v.douyin.com/profile-share/"
    status, payload = _request(
        route_server,
        "/api/knowledge-sources",
        method="POST",
        payload={"raw_url": share_text},
    )
    assert status == 201
    source = payload["source"]
    assert source["raw_url"] == "https://v.douyin.com/profile-share/"
    assert source["original_input"] == share_text
    assert source["material_type"] == "profile"
    assert source["status"] == "PENDING_PROCESSING"
    assert source["usage_scope"] == "distillation"
    assert source["usage_scope_label"] == "纳入知识资产中枢"


def test_reregister_profile_share_upgrades_legacy_short_link_record(route_server):
    short_link = "https://v.douyin.com/profile-share-reregister/"
    status, payload = _request(
        route_server,
        "/api/knowledge-sources",
        method="POST",
        payload={"raw_url": short_link, "usage_scope": "distillation"},
    )
    assert status == 201
    source_id = payload["source"]["source_id"]

    # 模拟主线服务升级前已把短链保存成“单条视频且处理失败”的历史记录。
    registry = console._knowledge_source_registry()
    document = json.loads(registry.path.read_text(encoding="utf-8"))
    legacy = document["sources"][source_id]
    legacy.update(
        {
            "source_kind": "video",
            "material_type": "video",
            "status": "FAILED",
            "status_label": "失败",
            "error_code": "legacy_video_route",
            "error_message": "旧版本错误进入单条视频处理链",
        }
    )
    registry.path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    share_text = "长按复制此条消息，打开抖音搜索，查看TA的更多作品。 " + short_link
    status, repaired = _request(
        route_server,
        "/api/knowledge-sources",
        method="POST",
        payload={"raw_url": share_text},
    )
    assert status == 201
    source = repaired["source"]
    assert source["source_id"] == source_id
    assert source["original_input"] == share_text
    assert source["material_type"] == "profile"
    assert source["usage_scope"] == "distillation"
    assert source["status"] == "PENDING_PROCESSING"
    assert source["error_code"] == ""
    assert source["error_message"] == ""


def test_legacy_case_rewrite_explains_profile_input(route_server):
    status, result = console._create_case_rewrite(
        {
            "case_video_url": "打开抖音搜索，查看TA的更多作品。 https://v.douyin.com/profile-share/",
            "style_profile_id": "style-demo",
        }
    )
    assert status == 422
    assert result["status"] == "profile_source_requires_registration"
    assert "知识源（自动识别）" in result["message"]


def test_processing_reclassifies_legacy_profile_share_record(route_server, monkeypatch):
    share_text = "打开抖音搜索，查看TA的更多作品。 https://v.douyin.com/profile-share/"
    status, payload = _request(
        route_server,
        "/api/knowledge-sources",
        method="POST",
        payload={"raw_url": share_text, "usage_scope": "distillation"},
    )
    assert status == 201
    source_id = payload["source"]["source_id"]

    # 模拟旧版本已经把该主页分享记录保存成 video；处理时应自动纠正，
    # 而不是再次进入单条视频采集器。
    registry = console._knowledge_source_registry()
    document = json.loads(registry.path.read_text(encoding="utf-8"))
    document["sources"][source_id]["source_kind"] = "video"
    document["sources"][source_id]["material_type"] = "video"
    document["sources"][source_id]["status"] = "PENDING_PROCESSING"
    document["sources"][source_id]["status_label"] = "待处理"
    registry.path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(console, "_create_style_task", lambda _payload: {"task_id": "style-reclassified"})

    process_status, processed = _request(
        route_server,
        f"/api/knowledge-sources/{source_id}/process",
        method="POST",
        payload={},
    )
    assert process_status == 202
    assert processed["task_id"] == "style-reclassified"
    assert processed["source"]["material_type"] == "profile"


def test_local_source_is_written_to_vault_and_visible_in_queue(route_server, tmp_path):
    encoded = base64.b64encode("这是本地资料正文，用于路由级知识源回归验证。".encode("utf-8")).decode("ascii")
    status, payload = _request(
        route_server,
        "/api/knowledge-sources",
        method="POST",
        payload={
            "usage_scope": "knowledge_base",
            "title": "本地测试资料",
            "file": {"name": "notes.txt", "mime_type": "text/plain", "base64": encoded},
        },
    )
    assert status == 201
    source = payload["source"]
    assert source["source_id"].startswith("source-")
    assert source["status"] == "PENDING_PROCESSING"
    assert Path(source["obsidian_path"]).is_file()
    assert str(tmp_path / "vault") in source["obsidian_path"]

    queue_status, queue = _request(route_server, "/api/obsidian/queue")
    assert queue_status == 200
    row = next(item for item in queue["sources"] if item["source_id"] == source["source_id"])
    assert row["source_id"] == source["source_id"]
    assert row["obsidian_path"] == source["obsidian_path"]

    detail_status, detail = _request(route_server, f"/api/knowledge-sources/{source['source_id']}")
    assert detail_status == 200
    assert detail["source"]["source_id"] == source["source_id"]
    assert "content" not in detail["source"]


def test_unknown_source_can_be_confirmed_without_guessing(route_server):
    status, payload = _request(
        route_server,
        "/api/knowledge-sources",
        method="POST",
        payload={"raw_url": "https://example.com/material", "usage_scope": "distillation"},
    )
    assert status == 201
    source_id = payload["source"]["source_id"]
    assert payload["source"]["status"] == "PENDING_CONFIRMATION"

    confirm_status, confirmed = _request(
        route_server,
        f"/api/knowledge-sources/{source_id}/confirm",
        method="POST",
        payload={"platform": "other", "material_type": "document", "author": "人工确认"},
    )
    assert confirm_status == 200
    assert confirmed["source"]["status"] == "PENDING_PROCESSING"
    assert confirmed["source"]["detection_status"] == "confirmed"


def test_process_route_extracts_local_text_and_updates_vault_record(route_server, tmp_path):
    encoded = base64.b64encode(
        ("这是足够长的本地资料正文，用于验证显式处理接口会把内容写回同一份 Obsidian 笔记。" * 4).encode("utf-8")
    ).decode("ascii")
    status, payload = _request(
        route_server,
        "/api/knowledge-sources",
        method="POST",
        payload={
            "usage_scope": "knowledge_base",
            "title": "待处理本地资料",
            "file": {"name": "process.txt", "mime_type": "text/plain", "base64": encoded},
        },
    )
    assert status == 201
    source = payload["source"]
    assert source["status"] == "PENDING_PROCESSING"
    source_id = source["source_id"]

    process_status, processed = _request(
        route_server,
        f"/api/knowledge-sources/{source_id}/process",
        method="POST",
        payload={},
    )
    assert process_status == 200
    assert processed["status"] == "ready"
    assert processed["source"]["status"] == "PENDING_REVIEW"
    assert processed["source"]["content_available"] is True
    note = Path(processed["source"]["obsidian_path"])
    assert note.is_file()
    assert "显式处理接口" in note.read_text(encoding="utf-8")


def test_process_route_records_explicit_failure_for_unsupported_local_file(route_server):
    encoded = base64.b64encode(b"binary placeholder").decode("ascii")
    status, payload = _request(
        route_server,
        "/api/knowledge-sources",
        method="POST",
        payload={
            "usage_scope": "knowledge_base",
            "title": "不支持的本地资料",
            "file": {"name": "source.bin", "mime_type": "application/octet-stream", "base64": encoded},
        },
    )
    assert status == 201
    source_id = payload["source"]["source_id"]

    with pytest.raises(HTTPError) as error:
        _request(
            route_server,
            f"/api/knowledge-sources/{source_id}/process",
            method="POST",
            payload={},
        )
    assert error.value.code == 422
    failed = json.loads(error.value.read().decode("utf-8"))
    assert failed["status"] == "processing_failed"
    assert failed["source"]["status"] == "FAILED"
    assert failed["source"]["error_code"] == "content_extraction_failed"


def test_style_task_input_preserves_knowledge_source_association():
    task_input = console._style_task_input(
        {
            "creator_home_url": "https://www.douyin.com/user/example",
            "sample_target": 30,
            "knowledge_source_id": "source-profile-1",
        }
    )
    assert task_input["knowledge_source_ids"] == ["source-profile-1"]
    assert task_input["sample_target"] == 30
    assert task_input["max_video_duration_seconds"] == 600


def test_profile_processing_persists_collection_policy(route_server, monkeypatch):
    """主页登记后的采集数量和五分钟阈值必须进入同一任务快照。"""

    status, payload = _request(
        route_server,
        "/api/knowledge-sources",
        method="POST",
        payload={
            "raw_url": "https://www.douyin.com/user/example-profile-policy",
            "usage_scope": "distillation",
        },
    )
    assert status == 201
    source_id = payload["source"]["source_id"]
    captured: dict[str, object] = {}

    def fake_create_style_task(value):
        captured.update(value)
        return {"task_id": "style-policy-task"}

    monkeypatch.setattr(console, "_create_style_task", fake_create_style_task)
    process_status, result = _request(
        route_server,
        f"/api/knowledge-sources/{source_id}/process",
        method="POST",
        payload={"sample_target": 100, "max_video_duration_seconds": 300},
    )

    assert process_status == 202
    assert result["task_id"] == "style-policy-task"
    assert captured["sample_target"] == 100
    assert captured["max_video_duration_seconds"] == 300
    policy = result["source"]["processing_result"]["collection_policy"]
    assert policy == {"sample_target": 100, "max_video_duration_seconds": 300}


def test_registered_source_is_bound_to_created_writing_task(route_server, tmp_path, monkeypatch):
    """知识源与异步文案任务必须共享同一个 source_id。"""

    encoded = base64.b64encode(
        ("这是足够长的本地资料正文，用于验证知识源登记后可以被文案任务引用。" * 4).encode("utf-8")
    ).decode("ascii")
    status, payload = _request(
        route_server,
        "/api/knowledge-sources",
        method="POST",
        payload={
            "usage_scope": "knowledge_base",
            "title": "任务绑定资料",
            "file": {"name": "task-binding.txt", "mime_type": "text/plain", "base64": encoded},
        },
    )
    assert status == 201
    source_id = payload["source"]["source_id"]

    # 这里只验证创建任务的真实持久化/绑定契约，不触发模型调用。
    monkeypatch.setattr(console, "_run_case_rewrite_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(console, "STYLE_TASK_DIR", tmp_path / "tasks")
    monkeypatch.setattr(console, "STYLE_TASKS", {})
    task = console._create_case_rewrite_task_locked(
        {
            "writing_mode": "legacy",
            "knowledge_source_ids": [source_id],
            "topic": "验证知识源任务关联",
            "target_platform": "小红书图文",
            "audience": "测试用户",
            "style_profile_id": "",
        }
    )
    task_id = str(task["task_id"])
    assert task["input"]["knowledge_source_ids"] == [source_id]

    source_status, source_payload = _request(
        route_server, f"/api/knowledge-sources/{source_id}"
    )
    assert source_status == 200
    assert task_id in source_payload["source"]["bound_task_ids"]


def test_production_pages_expose_real_workbench_and_console(route_server):
    writing = urlopen(f"http://127.0.0.1:{route_server.server_port}/writing").read().decode("utf-8")
    assert 'id="writingKnowledgeWorkspace"' in writing
    assert 'id="writingProductionWorkspace"' in writing
    assert 'id="writingKnowledgeIntake"' in writing
    assert writing.index('id="writingKnowledgeIntake"') < writing.index('id="writingProductionWorkspace"')
    assert '本次文案需求' in writing
    assert 'id="writingChooseLocalFile"' in writing
    assert '选择本地文件' in writing
    assert 'id="writingSourceFile" type="file"' in writing
    assert 'id="writingAssetPipelineProgress"' in writing
    assert '账号资产整理（第 2—3 步）' in writing
    assert 'data-asset-pipeline-step="thinking"' in writing
    assert 'data-asset-pipeline-step="style"' in writing
    assert 'data-asset-pipeline-step="review"' not in writing
    assert writing.count('data-asset-pipeline-step=') == 3
    assert 'writingResumeAssetPipeline' in writing
    assert '使用已有样本复用冲突作品' in writing
    assert 'restoreRecoverableAssetPipeline' in writing
    assert "view=recoverable&task_type=style_collection" in writing
    assert '/resume' in writing
    assert 'aria-label="知识源登记进度，仅代表第 1 步"' in writing
    assert '/api/style-tasks/' in writing
    assert 'setAssetPipelineProgress' in writing
    assert 'status_read_retry' in writing
    assert 'status_read_unavailable' in writing
    assert '这不代表账号资产任务失败' in writing
    assert 'id="writingSourceUsageButtons"' not in writing
    assert 'data-source-usage="current_copy"' not in writing
    assert 'id="writingSourceUsage"' not in writing
    assert "usageScope='distillation'" in writing
    assert 'data-source-usage="knowledge_base"' not in writing
    assert 'id="writingKnowledgeSourceSession"' in writing
    assert 'id="writingProfileCollectionDialog"' in writing
    assert 'data-profile-sample-target="50"' in writing
    assert 'data-profile-sample-target="100"' in writing
    assert '只保留 5 分钟以内的视频' in writing
    assert '设置采集条件' in writing
    assert 'max_video_duration_seconds:300' in writing
    assert 'id="writingKnowledgeSourceList"' not in writing
    assert '纳入知识资产中枢' in writing
    assert '确认并开始整理' in writing
    assert '去 Obsidian 蒸馏' not in writing
    assert '<select id="writingSourceUsage"' not in writing
    assert "writing_mode:'production'" in writing
    assert "production_eligible===true" in writing
    assert writing.count("<script") == writing.count("</script>")

    obsidian = urlopen(f"http://127.0.0.1:{route_server.server_port}/obsidian").read().decode("utf-8")
    assert "Obsidian 控制台" in obsidian
    assert "Obsidian 知识资产控制台" not in obsidian
    assert 'class="global-tools"' in obsidian
    assert 'global-link' in obsidian and 'href="/obsidian"' in obsidian
    assert 'global-link' in obsidian and 'href="/knowledge-review"' in obsidian
    assert 'class="nav" href="/obsidian"' not in obsidian
    assert 'class="nav" href="/knowledge-review"' not in obsidian
    assert 'id="obsidianQueueBody"' in obsidian
    assert "/api/obsidian/queue" in obsidian
    assert "/api/obsidian/assets" in obsidian
    assert obsidian.count("<script") == obsidian.count("</script>")

    review = urlopen(f"http://127.0.0.1:{route_server.server_port}/knowledge-review").read().decode("utf-8")
    assert "知识资产审核" in review
    assert "obsidian-review-page" in review
    assert 'id="obsidianAssetsCard"' in review
    assert "#obsidianDistillationCard" in review
    assert review.count("<script") == review.count("</script>")


def test_readonly_dev_allows_detection_but_blocks_writes(route_server, monkeypatch):
    """只读冒烟实例可验证自动识别，但不能伪造登记成功。"""

    monkeypatch.setattr(console, "DEV_READ_ONLY", True)
    status, payload = _request(
        route_server,
        "/api/knowledge-sources/detect",
        method="POST",
        payload={"file": {"name": "smoke.txt", "mime_type": "text/plain"}},
    )
    assert status == 200
    assert payload["detection"]["platform"] == "local"
    assert payload["detection"]["material_type"] == "document"

    with pytest.raises(HTTPError) as error:
        _request(
            route_server,
            "/api/knowledge-sources",
            method="POST",
            payload={"raw_url": "https://www.douyin.com/video/smoke"},
        )
    assert error.value.code == 409
    assert json.loads(error.value.read().decode("utf-8"))["status"] == "dev_read_only"


def test_persisted_obsidian_config_is_used_after_environment_reset(route_server, tmp_path, monkeypatch):
    """页面保存的 Vault 配置在服务重启/清空环境变量后仍是有效事实源。"""

    vault = tmp_path / "persisted-vault"
    config_status, config = _request(
        route_server,
        "/api/obsidian/config",
        method="POST",
        payload={"vault_path": str(vault)},
    )
    assert config_status == 200
    assert config["vault_path"] == str(vault.resolve())

    # 模拟服务重启后没有继承旧环境变量；所有生产读取/写入路径都应从
    # obsidian_config.json 恢复，而不是把账号知识库误报成未配置。
    monkeypatch.delenv("ACCOUNT_KNOWLEDGE_VAULT_PATH", raising=False)
    health_status, health = _request(route_server, "/api/executor/health")
    assert health_status == 200
    assert health["account_knowledge_bridge"]["configured"] is True
    assert health["account_knowledge_bridge"]["vault_path"] == str(vault.resolve())

    assets_status, assets = _request(route_server, "/api/obsidian/assets")
    assert assets_status == 200
    assert assets["knowledge"]["status"] == "ready"
    assert assets["knowledge"]["configured"] is True

    material_status, material = _request(
        route_server,
        "/api/account-knowledge/materials",
        method="POST",
        payload={
            "material_type": "document",
            "platform": "other",
            "content_text": "配置文件恢复后的专业资料正文。",
            "domain": "restart-test",
        },
    )
    assert material_status == 201
    assert material["status"] == "pending_processing"


def test_legacy_approved_profile_is_not_production_eligible(tmp_path, monkeypatch):
    store = StylePackageStore(tmp_path / "styles")
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    legacy = store.save_profile(
        profile={"profile_name": "历史包"},
        creator_url="https://www.douyin.com/user/legacy",
        collection_id="collection-legacy",
        sample_count=16,
        creator_name="历史包",
    )
    store.approve_profile(legacy["style_profile_id"])

    summary = console._profile_summary(store.get_profile(legacy["style_profile_id"]))
    assert summary["production_eligible"] is False
    assert summary["writing_dna_gate"]["status"] == "EXPERIMENTAL"
    assert summary["writing_dna_gate"]["formal"] is False

    status, result = console._create_case_rewrite(
        {
            "writing_mode": "production",
            "style_profile_id": legacy["style_profile_id"],
            "case_video_url": "https://v.douyin.com/legacy",
        }
    )
    assert status == 409
    assert result["status"] == "style_profile_not_production_eligible"
