import base64
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tools.video_production_console as console
from workflow_1256.style_package_store import StylePackageStore


def _report():
    return {
        "collection_status": "completed",
        "platform": "douyin",
        "creator_id": "sec_uid_for_console_test",
        "creator_url": "https://www.douyin.com/user/sec_uid_for_console_test",
        "creator_name": "控制台测试账号",
        "transcript_ready_count": 1,
        "items": [{
            "video_id": "1",
            "title": "测试作品",
            "source_url": "https://www.douyin.com/video/1",
            "published_at": "2026-08-28T00:00:00+00:00",
            "transcript_status": "local_whisper",
            "transcript": "这是一段足够长的转写内容，用于验证 8768 风格包入口已经调用账号知识库桥接层。" * 4,
        }],
    }


def test_style_entry_requires_vault_when_ui_marks_account_knowledge_required(tmp_path, monkeypatch):
    store = StylePackageStore(tmp_path / "style-packages")
    report = _report()
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    # This unit test models an unconfigured account-knowledge environment.  The
    # production service also restores a Vault from its persisted config file,
    # so isolate that durable file here instead of allowing a developer's local
    # runtime config to make the test order-dependent.
    monkeypatch.setattr(console, "OBSIDIAN_CONFIG_PATH", tmp_path / "obsidian-config.json")
    monkeypatch.setattr(console, "collect_douyin_creator", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        console,
        "distill_style_package",
        lambda **kwargs: ({"profile_name": "控制台测试", "overview": "测试"}, report["items"]),
    )
    monkeypatch.delenv("ACCOUNT_KNOWLEDGE_VAULT_PATH", raising=False)

    status, result = console._create_style_package({
        "creator_home_url": report["creator_url"],
        "sample_target": 30,
        "account_knowledge_mode": "required",
    })

    assert status == 503
    assert result["status"] == "account_knowledge_distillation_failed"
    assert "ACCOUNT_KNOWLEDGE_VAULT_PATH" in result["message"]
    assert store.list_profiles() == []


def test_style_entry_binds_pending_account_context_to_new_profile(tmp_path, monkeypatch):
    store = StylePackageStore(tmp_path / "style-packages")
    vault = tmp_path / "vault"
    report = _report()
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", store)
    monkeypatch.setattr(console, "collect_douyin_creator", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        console,
        "distill_style_package",
        lambda **kwargs: ({"profile_name": "控制台测试", "overview": "测试"}, report["items"]),
    )
    monkeypatch.setenv("ACCOUNT_KNOWLEDGE_VAULT_PATH", str(vault))
    monkeypatch.setattr(
        console.AccountKnowledgeModelProvider,
        "from_runtime_config",
        classmethod(lambda cls, **kwargs: object()),
    )
    bridge_calls = []

    def fake_bridge(report_value, *, repository, provider, requested_account_id="", progress_reporter=None):
        bridge_calls.append((report_value, repository.vault_path, provider))
        repository.initialize_account("creator_test", account_name="控制台测试账号")
        return {"status": "review_required", "account_id": "creator_test", "sample_count": 1}

    monkeypatch.setattr(console, "sync_collection_to_obsidian", fake_bridge)

    status, result = console._create_style_package({
        "creator_home_url": report["creator_url"],
        "sample_target": 30,
        "account_knowledge_mode": "required",
    })

    assert status == 201
    assert len(bridge_calls) == 1
    assert result["report"]["account_knowledge"]["account_id"] == "creator_test"
    assert result["record"]["style_profile"]["account_id"] == "creator_test"
    assert result["record"]["style_profile"]["account_knowledge_context"]["available"] is False


def test_style_entry_writes_obsidian_before_language_style_distillation(tmp_path, monkeypatch):
    """转写先进入作品卡/账号知识，再运行语言风格蒸馏。"""

    report = _report()
    calls = []
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", StylePackageStore(tmp_path / "style-packages"))
    monkeypatch.setattr(console, "collect_douyin_creator", lambda *args, **kwargs: report)

    def fake_sync(*_args, **_kwargs):
        calls.append("obsidian")
        return {"status": "not_used"}, None

    def fake_distill(**_kwargs):
        calls.append("style")
        return {"profile_name": "顺序测试", "overview": "测试"}, report["items"]

    monkeypatch.setattr(console, "_sync_style_collection_account_knowledge", fake_sync)
    monkeypatch.setattr(console, "distill_style_package", fake_distill)

    status, result = console._create_style_package({
        "creator_home_url": report["creator_url"],
        "sample_target": 30,
        "account_knowledge_mode": "required",
    })

    assert status == 201
    assert result["status"] == "pending_user_review"
    assert calls == ["obsidian", "style"]


def test_writing_page_surfaces_reusable_knowledge_assets_and_hidden_obsidian_entry():
    # /writing 实际挂载的是带运行时卡片增强和资料入口的生产页面。
    page = console._writing_page_live_with_selection_guard().decode("utf-8")

    assert 'id="accountKnowledgePanel"' not in page
    assert 'id="obsidianEntryBar"' in page
    assert 'href="/obsidian"' in page
    assert 'target="_blank"' in page
    assert 'id="writingAssetPicker"' in page
    assert 'id="writingViewpointCards"' in page
    assert 'id="writingExpressionCards"' in page
    assert 'id="writingCompositionCard"' in page
    assert 'class="writing-asset-card-grid"' in page
    assert "本次组合包" in page
    assert "writingAddMaterialPackage" in page
    assert "/writing#writingKnowledgeIntake" in page
    assert 'writing-asset-picker-actions' in page
    assert 'id="writingViewpointSelect"' in page
    assert 'id="writingExpressionSelect"' in page
    assert 'id="fiveLayerComposer"' not in page
    assert 'id="previewFiveLayerComposition"' not in page
    assert "/api/account-knowledge/summary" in page
    assert page.count("<script") == page.count("</script>")
    assert "8790" not in page


def test_obsidian_page_contains_five_modules_and_input_entries():
    page = console._obsidian_page_live().decode("utf-8")

    assert 'id="obsidianWorkspace"' in page
    for module_id in (
        "obsidianModuleKnowledge",
        "obsidianModuleViewpoint",
        "obsidianModuleExpression",
        "obsidianModuleHot",
        "obsidianModuleGovernance",
    ):
        assert f'id="{module_id}"' in page
    for input_id in (
        "obsidianKnowledgeSource",
        "obsidianKnowledgeFile",
        "obsidianKnowledgeMaterialType",
        "obsidianKnowledgePlatform",
        "obsidianKnowledgeText",
        "obsidianViewpointSource",
        "obsidianExpressionCreator",
        "obsidianHotUrl",
        "obsidianHotText",
        "previewObsidianComposition",
    ):
        assert f'id="{input_id}"' in page
    for platform_label in ("视频平台", "图文平台", "抖音", "哔哩哔哩", "小红书", "微信公众号"):
        assert platform_label in page
    assert "不会调用大模型" in page
    assert page.count("<script") == page.count("</script>")


def test_writing_governance_exposes_viewpoint_and_language_selectors():
    page = console._writing_governance_page().decode("utf-8")

    assert 'id="wgwViewpointPack"' in page
    assert 'id="wgwStyleProfile"' in page
    assert "语言风格包（已审核）" in page
    assert 'href="/obsidian"' in page
    assert 'target="_blank"' in page


def test_professional_material_registration_writes_video_article_and_attachment(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNT_KNOWLEDGE_VAULT_PATH", str(tmp_path / "vault"))

    article_status, article = console._register_account_knowledge_material({
        "material_type": "article",
        "platform": "wechat_article",
        "source_url": "https://mp.weixin.qq.com/s/example",
        "content_text": "宏观经济资料摘要",
        "domain": "宏观经济",
        "tags": ["通胀", "就业"],
    })
    assert article_status == 201
    assert article["status"] == "pending_processing"
    assert Path(article["paths"]["material_path"]).is_file()

    video_status, video = console._register_account_knowledge_material({
        "material_type": "video",
        "platform": "bilibili",
        "source_url": "https://www.bilibili.com/video/BVtest",
        "content_text": "视频字幕摘要",
    })
    assert video_status == 201
    assert video["platform"] == "bilibili"

    document_status, document = console._register_account_knowledge_material({
        "material_type": "document",
        "platform": "other",
        "file": {
            "name": "report.pdf",
            "mime_type": "application/pdf",
            "base64": base64.b64encode(b"%PDF-test").decode("ascii"),
        },
    })
    assert document_status == 201
    assert Path(document["paths"]["attachment_path"]).read_bytes() == b"%PDF-test"


def test_writing_page_sends_selected_account_to_rewrite_request():
    page = console._writing_page_live_with_selection_guard().decode("utf-8")
    assert "body.account_id=window.__selectedViewpointAccountId" in page
    assert '"account_id": str(normalized_payload.get("account_id") or "").strip()' in inspect.getsource(console._create_case_rewrite_task_locked)


def test_production_writing_page_does_not_start_legacy_task_pollers():
    page = console._writing_page_production_impl().decode("utf-8")

    assert "window.__writingProductionPage=true" in page
    # 生产页在最终输出层硬禁用历史轮询，避免它与知识源登记争抢连接。
    assert "if(false)window.setTimeout(reconcileRewriteTask,400);" in page
    assert "if(false)window.setTimeout(start,300);" in page
    assert "const fetchWithTimeout=" in page
    assert "fetchWithTimeout('/api/knowledge-sources'" in page


def test_production_profile_intake_uses_tikhub_without_qr_gate():
    page = console._writing_page_production_impl().decode("utf-8")

    # The QR/Cookie widget remains available for the historical case-video
    # compatibility flow, but a creator homepage registered as a knowledge
    # source must open the 50/100 collection dialog and call the TikHub-backed
    # process endpoint directly.
    assert "await window.__ensureDouyinLogin()" not in page
    assert "正在启动 TikHub 账号资料整理" in page
    assert "请选择采集数量后启动 TikHub 账号资料整理" in page
    assert "后续 Skill 蒸馏" in page
