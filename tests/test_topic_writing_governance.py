from __future__ import annotations

import json
from pathlib import Path
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen

import pytest

import tools.video_production_console as console
from workflow_1256.topic_writing_governance import (
    TopicWritingGovernanceError,
    TopicWritingGovernanceStore,
)


def test_governance_store_tracks_selection_extraction_retry_and_manual_text(tmp_path):
    store = TopicWritingGovernanceStore(tmp_path / "governance.json")
    selection = store.save_selection(
        {
            "candidate_id": "candidate-1",
            "title": "可追踪新闻选题",
            "source_url": "https://example.com/news/1",
            "source_name": "Example News",
            "tags": ["治理", "新闻"],
        }
    )
    assert selection["state"] == "ACCEPTED"

    ready = store.save_extraction_result(
        selection["selection_id"],
        {
            "status": "ready",
            "source_url": selection["source_url"],
            "final_url": selection["source_url"],
            "title": selection["title"],
            "content": "这是一段足够长的真实新闻正文，用于验证正文提取结果可以进入文案创作和后续人工审核流程。" * 2,
            "content_type": "html_text",
            "message": "已读取公开 HTML 文本",
        },
    )
    assert ready["status"] == "CONTENT_READY"
    assert store.get_selection(selection["selection_id"])["source_content_id"] == ready["source_content_id"]
    assert store.get_source_for_copy(ready["source_content_id"])["transcript_status"] == "collected_html"

    failed = store.save_extraction_result(
        selection["selection_id"],
        {
            "status": "blocked",
            "source_url": selection["source_url"],
            "final_url": selection["source_url"],
            "error_code": "http_blocked",
            "message": "HTTP 403",
        },
    )
    assert failed["status"] == "EXTRACTION_BLOCKED"
    assert store.get_selection(selection["selection_id"])["state"] == "EXTRACTION_BLOCKED"
    history_states = [item["state"] for item in store.get_selection(selection["selection_id"])["state_history"]]
    assert history_states[:3] == ["ACCEPTED", "CONTENT_READY", "EXTRACTION_BLOCKED"]

    manual = store.save_manual_source("纯文案输入也必须经过统一的素材对象和审核状态管理，不能绕过正文长度校验。" * 3)
    assert manual["status"] == "CONTENT_READY"
    assert manual["input_type"] == "manual_text"
    assert manual["source_url"] == ""
    assert store.get_source_for_copy(manual["source_content_id"])["transcript_status"] == "manual_text"
    assert (tmp_path / "governance.json").is_file()

    with pytest.raises(TopicWritingGovernanceError, match="至少需要 80"):
        store.save_manual_source("太短")


def test_archive_source_removes_it_from_active_queue_but_keeps_history(tmp_path):
    store = TopicWritingGovernanceStore(tmp_path / "governance.json")
    selection = store.save_selection(
        {
            "candidate_id": "candidate-archive",
            "title": "可移出队列的选题",
            "source_url": "https://example.com/archive",
        }
    )
    source = store.save_extraction_result(
        selection["selection_id"],
        {
            "status": "ready",
            "source_url": selection["source_url"],
            "final_url": selection["source_url"],
            "title": selection["title"],
            "content": "这是一段足够长的正文，用于验证移出队列只隐藏待处理入口，不删除素材本身和选题历史。" * 2,
        },
    )
    assert len(store.list_sources()) == 1

    archived = store.archive_source(source["source_content_id"])

    assert archived["queue_state"] == "ARCHIVED"
    assert store.list_sources() == []
    assert store.get_source(source["source_content_id"])["status"] == "CONTENT_READY"
    assert store.get_selection(selection["selection_id"])["queue_state"] == "ARCHIVED"


def test_archive_source_blocks_material_already_in_copy_review_chain(tmp_path):
    store = TopicWritingGovernanceStore(tmp_path / "governance.json")
    selection = store.save_selection(
        {
            "candidate_id": "candidate-protected",
            "title": "已进入审核的选题",
            "source_url": "https://example.com/protected",
        }
    )
    source = store.save_extraction_result(
        selection["selection_id"],
        {
            "status": "ready",
            "source_url": selection["source_url"],
            "final_url": selection["source_url"],
            "title": selection["title"],
            "content": "这是一段足够长的正文，用于验证已经进入文案审核链路的素材不能被移出队列。" * 2,
        },
    )
    store.link_copy(
        "rewrite-protected",
        source_content_id=source["source_content_id"],
        state="REVIEW_PENDING",
    )

    with pytest.raises(TopicWritingGovernanceError, match="不能移出队列"):
        store.archive_source(source["source_content_id"])


def test_governance_accept_and_extract_use_new_store_without_opening_archive_queue(monkeypatch, tmp_path):
    store = TopicWritingGovernanceStore(tmp_path / "governance.json")
    monkeypatch.setattr(console, "TOPIC_WRITING_GOVERNANCE_STORE", store)
    monkeypatch.setattr(
        console,
        "_save_topic_center_decision",
        lambda payload: {
            "selection_id": "topic-candidate-2",
            "candidate_id": payload["candidate_id"],
            "decision": "accepted",
            "collection_status": "archived",
        },
    )
    accepted = console._topic_writing_governance_accept(
        {
            "candidate_id": "candidate-2",
            "title": "接口可验收的新闻",
            "source_url": "https://example.com/news/2",
        }
    )
    assert accepted["selection"]["state"] == "ACCEPTED"

    monkeypatch.setattr(
        console,
        "collect_topic_source",
        lambda selection, timeout=20: {
            "status": "ready",
            "selection_id": selection["selection_id"],
            "source_url": selection["source_url"],
            "final_url": selection["source_url"],
            "title": selection["title"],
            "content": "真实正文提取结果用于验证重试和读取接口，达到八十个字符后才允许进入统一的文案创作链路。" * 2,
            "content_type": "html_text",
            "message": "测试采集完成",
        },
    )
    extracted = console._topic_writing_governance_extract(accepted["selection"]["selection_id"])
    assert extracted["status"] == "CONTENT_READY"
    assert extracted["source"]["content"]
    assert extracted["selection"]["source_content_id"] == extracted["source"]["source_content_id"]
    assert console.TOPIC_CENTER_QUEUE_ENABLED is False


def test_trendradar_accept_does_not_requery_horizon(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "TOPIC_CENTER_ARCHIVE_PATH", tmp_path / "archive.json")
    monkeypatch.setattr(console, "TOPIC_CENTER_DECISIONS_PATH", tmp_path / "decisions.json")
    monkeypatch.setattr(
        console,
        "_topic_center_source_snapshot",
        lambda _candidate_id: (_ for _ in ()).throw(AssertionError("不应回查 Horizon")),
    )
    result = console._save_topic_center_decision(
        {
            "candidate_id": "trendradar-candidate",
            "status": "accepted",
            "title": "TrendRadar 真实热点",
            "source_url": "https://example.com/trendradar",
            "source_name": "财联社热门",
            "platform": "cls-hot",
            "source_metadata": {"provider": "TrendRadar", "news_item_id": 12},
        }
    )
    assert result["decision"] == "accepted"


def test_joint_accept_persists_trendradar_platform_and_provider(monkeypatch, tmp_path):
    store = TopicWritingGovernanceStore(tmp_path / "governance.json")
    monkeypatch.setattr(console, "TOPIC_WRITING_GOVERNANCE_STORE", store)
    monkeypatch.setattr(console, "TOPIC_CENTER_ARCHIVE_PATH", tmp_path / "archive.json")
    monkeypatch.setattr(console, "TOPIC_CENTER_DECISIONS_PATH", tmp_path / "decisions.json")
    monkeypatch.setattr(
        console,
        "_topic_center_source_snapshot",
        lambda _candidate_id: (_ for _ in ()).throw(AssertionError("不应回查 Horizon")),
    )
    result = console._topic_writing_governance_accept(
        {
            "candidate_id": "trendradar-candidate-2",
            "title": "TrendRadar 联合治理候选",
            "source_url": "https://example.com/trendradar-2",
            "source_name": "知乎",
            "platform": "zhihu",
            "source_metadata": {"provider": "TrendRadar", "news_item_id": 22},
        }
    )
    assert result["selection"]["state"] == "ACCEPTED"
    assert result["selection"]["platform"] == "zhihu"
    assert result["selection"]["source_metadata"]["provider"] == "TrendRadar"


def test_governance_extract_retry_replaces_failed_latest_source(monkeypatch, tmp_path):
    store = TopicWritingGovernanceStore(tmp_path / "governance.json")
    monkeypatch.setattr(console, "TOPIC_WRITING_GOVERNANCE_STORE", store)
    monkeypatch.setattr(
        console,
        "_save_topic_center_decision",
        lambda payload: {"selection_id": "topic-retry", "candidate_id": payload["candidate_id"]},
    )
    accepted = console._topic_writing_governance_accept(
        {"candidate_id": "retry", "title": "可重试正文", "source_url": "https://example.com/retry"}
    )
    responses = [
        {"status": "blocked", "source_url": "https://example.com/retry", "final_url": "https://example.com/retry", "message": "HTTP 403", "error_code": "http_blocked"},
        {"status": "ready", "source_url": "https://example.com/retry", "final_url": "https://example.com/retry", "content": "重试后读取到的正文达到八十个字符，允许进入文案创作并保留每一次提取结果的可追踪状态。" * 2, "title": "可重试正文", "message": "重试成功"},
    ]
    monkeypatch.setattr(console, "collect_topic_source", lambda *_args, **_kwargs: responses.pop(0))
    first = console._topic_writing_governance_extract(accepted["selection"]["selection_id"])
    second = console._topic_writing_governance_extract(accepted["selection"]["selection_id"])
    assert first["status"] == "EXTRACTION_BLOCKED"
    assert first["retryable"] is True
    assert second["status"] == "CONTENT_READY"
    assert second["source"]["content"]


def test_governance_store_saves_real_video_transcript_as_content_ready(tmp_path):
    store = TopicWritingGovernanceStore(tmp_path / "governance.json")
    source = store.save_video_source(
        {
            "platform": "douyin",
            "source_url": "https://www.douyin.com/video/123456",
            "video_id": "123456",
            "title": "真实视频案例",
            "duration_seconds": 92,
            "transcript_status": "local_whisper",
            "transcript": "这是一段来自真实视频的字幕转写，必须保存为正文并参与后续文案治理，不能用标题或搜索摘要代替。" * 2,
        }
    )
    assert source["input_type"] == "video_transcript"
    assert source["status"] == "CONTENT_READY"
    assert source["transcript_status"] == "local_whisper"
    assert source["content_hash"]
    assert source["source_metadata"]["video_id"] == "123456"
    assert store.get_source_for_copy(source["source_content_id"])["content"] == source["content"]


def test_governed_video_link_rewrite_creates_content_ready_source_and_waits_review(monkeypatch, tmp_path):
    governance = TopicWritingGovernanceStore(tmp_path / "governance.json")
    style_store = console.StylePackageStore(tmp_path / "styles")
    monkeypatch.setattr(console, "TOPIC_WRITING_GOVERNANCE_STORE", governance)
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", style_store)
    profile = style_store.save_profile(
        profile={"profile_name": "视频案例风格", "overview": "测试规则"},
        creator_url="https://example.com/creator",
        collection_id="collection-video",
        sample_count=1,
        creator_name="视频案例风格",
    )
    style_store.approve_profile(profile["style_profile_id"])
    monkeypatch.setattr(console, "collect_douyin_case_video", lambda _url: {
        "platform": "douyin",
        "source_url": "https://www.douyin.com/video/123456",
        "video_id": "123456",
        "title": "真实视频案例",
        "duration_seconds": 92,
        "transcript_status": "local_whisper",
        "transcript": "这是一段真实视频字幕转写，用于验证视频链接接入联合治理后必须先达到 CONTENT_READY，再等待人工审核。" * 2,
    })

    class DummyTransport:
        pass

    monkeypatch.setattr(console.ArkStoryFailoverHTTPTransport, "from_content_runtime_config", lambda: DummyTransport())
    monkeypatch.setattr(console.ArkStoryFailoverHTTPTransport, "from_style_runtime_config", lambda: DummyTransport())
    monkeypatch.setattr(console.ArkStoryFailoverHTTPTransport, "from_review_runtime_config", lambda: DummyTransport())
    monkeypatch.setattr(console, "rewrite_case_copy", lambda **_kwargs: {
        "rewritten_copy": "视频链接生成的待审核文案。",
        "pipeline": {"gate": "PASS", "review": {"status": "PASS"}},
    })

    status, result = console._create_case_rewrite({
        "style_profile_id": profile["style_profile_id"],
        "case_video_url": "https://v.douyin.com/example",
        "governance_mode": "joint",
    })
    assert status == 201
    assert result["status"] == "review_pending"
    source_id = result["source_content_id"]
    source = governance.get_source(source_id)
    assert source["status"] == "CONTENT_READY"
    assert source["input_type"] == "video_transcript"
    assert source["copy_state"] == "REVIEW_PENDING"
    assert result["record"]["case_source"]["source_content_id"] == source_id
    assert "approved_copy" not in result["record"]


def test_governed_video_link_approval_updates_source_and_saves_director_snapshot(monkeypatch, tmp_path):
    governance = TopicWritingGovernanceStore(tmp_path / "governance.json")
    style_store = console.StylePackageStore(tmp_path / "styles")
    monkeypatch.setattr(console, "TOPIC_WRITING_GOVERNANCE_STORE", governance)
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", style_store)
    source = governance.save_video_source(
        {
            "platform": "douyin",
            "source_url": "https://www.douyin.com/video/654321",
            "title": "待审核视频",
            "transcript_status": "local_whisper",
            "transcript": "这是一段真实视频转写正文，用于验证人工确认后治理素材会进入 DIRECTOR_READY，并且 approved_copy 会保存来源编号。" * 2,
        }
    )
    record = style_store.save_rewrite(
        style_profile_id="style-video",
        case_item={
            "source_content_id": source["source_content_id"],
            "source_url": source["source_url"],
            "title": source["title"],
            "transcript": source["content"],
        },
        result={"rewritten_copy": "视频链接生成的待审核文案。", "pipeline": {"gate": "PASS"}},
    )
    governance.link_copy(
        record["rewrite_id"],
        source_content_id=source["source_content_id"],
        state="REVIEW_PENDING",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/case-rewrites/{record['rewrite_id']}/approve",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        approved = json.loads(urlopen(request).read().decode("utf-8"))
        assert approved["review_status"] == "APPROVED"
        snapshot = governance.get_source(source["source_content_id"])
        assert snapshot["copy_state"] == "DIRECTOR_READY"
        assert governance._read()["approved_copies"][record["rewrite_id"]]["source_content_id"] == source["source_content_id"]
    finally:
        server.shutdown()
        server.server_close()


def test_case_rewrite_strict_three_way_input_contract():
    with pytest.raises(ValueError, match="严格三选一"):
        console._create_case_rewrite_task_locked(
            {
                "style_profile_id": "style-test",
                "case_video_url": "https://example.com/video",
                "source_content_id": "source-test",
            }
        )
    with pytest.raises(ValueError, match="必须提供"):
        console._create_case_rewrite_task_locked({"style_profile_id": "style-test"})


def test_joint_manual_rewrite_stays_review_pending_until_explicit_approval(monkeypatch, tmp_path):
    governance = TopicWritingGovernanceStore(tmp_path / "governance.json")
    style_store = console.StylePackageStore(tmp_path / "styles")
    monkeypatch.setattr(console, "TOPIC_WRITING_GOVERNANCE_STORE", governance)
    monkeypatch.setattr(console, "STYLE_PACKAGE_STORE", style_store)
    profile = style_store.save_profile(
        profile={"profile_name": "测试风格", "overview": "测试规则"},
        creator_url="https://example.com/creator",
        collection_id="collection-test",
        sample_count=1,
        creator_name="测试风格",
    )
    style_store.approve_profile(profile["style_profile_id"])

    class DummyTransport:
        pass

    monkeypatch.setattr(console.ArkStoryFailoverHTTPTransport, "from_content_runtime_config", lambda: DummyTransport())
    monkeypatch.setattr(console.ArkStoryFailoverHTTPTransport, "from_style_runtime_config", lambda: DummyTransport())
    monkeypatch.setattr(console.ArkStoryFailoverHTTPTransport, "from_review_runtime_config", lambda: DummyTransport())
    monkeypatch.setattr(
        console,
        "rewrite_case_copy",
        lambda **kwargs: {
            "rewritten_copy": "这是经过模型生成但尚未人工确认的联合治理文案。",
            "pipeline": {"gate": "PASS", "review": {"status": "PASS"}},
        },
    )

    status, result = console._create_case_rewrite(
        {
            "style_profile_id": profile["style_profile_id"],
            "source_text": "这是一段足够长的纯文案事实素材，用于验证不填写视频链接也能创建任务，并且生成结果必须先等待人工审核。" * 2,
            "governance_mode": "joint",
        }
    )
    assert status == 201
    assert result["status"] == "review_pending"
    rewrite = result["record"]
    assert rewrite["review_status"] == "PENDING_USER_REVIEW"
    assert "approved_copy" not in rewrite
    source_id = result["source_content_id"]
    assert governance.get_source(source_id)["copy_state"] == "REVIEW_PENDING"

    approved = style_store.approve_rewrite(rewrite["rewrite_id"])
    assert approved["approved_copy"]
    governance.link_copy(
        rewrite["rewrite_id"],
        source_content_id=source_id,
        state="APPROVED",
        message="人工审核通过",
    )
    governance.link_copy(
        rewrite["rewrite_id"],
        source_content_id=source_id,
        state="DIRECTOR_READY",
        message="已交付编导",
    )
    governance.save_approved_copy(
        rewrite["rewrite_id"],
        approved["approved_copy"],
        source_content_id=source_id,
        style_profile_id=profile["style_profile_id"],
    )
    source_snapshot = governance.get_source(source_id)
    assert source_snapshot["copy_state"] == "DIRECTOR_READY"
    assert governance._read()["approved_copies"][rewrite["rewrite_id"]]["approved_copy"] == approved["approved_copy"]


def test_production_topic_page_uses_real_api_and_keeps_writing_contract():
    new_page = console.PAGES["/topic-center"]().decode("utf-8")
    old_page = console.PAGES["/topic-writing-governance"]().decode("utf-8")
    writing_page = console.PAGES["/writing"]().decode("utf-8")
    writing_governance_page = console.PAGES["/writing-governance"]().decode("utf-8")
    assert "选题中心" in new_page
    assert "TOPIC INTELLIGENCE CENTER" in new_page
    assert "/api/topic-center/topics" in new_page
    assert "/api/topic-center/selections" in new_page
    assert "TrendRadar" in new_page
    assert "/writing" in new_page
    assert "/writing-governance" not in new_page
    assert "twgGenerateCopy" not in new_page
    assert "twgApprove" not in new_page
    assert "模拟" not in new_page
    assert "HORIZON · TOPIC RADAR" not in old_page
    assert 'id="caseVideoUrl"' in writing_page
    assert "文案创作与内容审查" in writing_governance_page
    assert "CONTENT_READY" in writing_governance_page
    assert "/api/case-rewrites" in writing_governance_page
    assert "source_content_id" in writing_governance_page
    assert "source_text" in writing_governance_page
    assert 'value="video"' in writing_governance_page
    assert 'id="wgwVideoUrl"' in writing_governance_page
    assert 'class="wgw-video-entry"' in writing_governance_page
    assert "直接提取视频字幕/转写" in writing_governance_page
    assert "payload.case_video_url" in writing_governance_page
    assert "governance_mode:'joint'" in writing_governance_page
    assert "REVIEW_PENDING" in writing_governance_page
    assert "/api/topic-writing-governance/candidates" not in writing_governance_page
    assert "data-archive-source" in writing_governance_page
    assert "/api/topic-center/sources/" in writing_governance_page
    assert "已移出队列" in writing_governance_page
