from __future__ import annotations

import json

import pytest

import tools.video_production_console as console
import workflow_1256.topic_collection as topic_collection
from workflow_1256.topic_writing_governance import TopicWritingGovernanceStore


def test_topic_center_selection_requires_real_primary_and_persists_bundle(tmp_path, monkeypatch):
    store = TopicWritingGovernanceStore(tmp_path / "governance.json")
    monkeypatch.setattr(console, "TOPIC_WRITING_GOVERNANCE_STORE", store)
    result = console._topic_center_selection_create({
        "candidate_id": "trend-1",
        "title": "真实主题",
        "content_items": [
            {
                "content_id": "tikhub-douyin-1",
                "provider": "TikHub",
                "platform": "douyin",
                "content_type": "video",
                "title": "真实视频",
                "url": "https://www.douyin.com/video/1",
                "author": "作者",
                "metrics": {"likes": 12},
            },
            {
                "content_id": "tikhub-zhihu-2",
                "provider": "TikHub",
                "platform": "zhihu",
                "content_type": "article",
                "title": "参考文章",
                "url": "https://zhuanlan.zhihu.com/p/2",
            },
        ],
        "primary_content_id": "tikhub-douyin-1",
        "reference_content_ids": ["tikhub-zhihu-2"],
    })
    selection = result["selection"]
    assert result["status"] == "accepted"
    assert selection["primary_content_id"] == "tikhub-douyin-1"
    assert selection["reference_content_ids"] == ["tikhub-zhihu-2"]
    assert selection["content_items"][0]["metrics"]["likes"] == 12
    saved = json.loads((tmp_path / "governance.json").read_text(encoding="utf-8"))
    assert saved["schema_version"] == "topic-writing-governance-v2"


def test_topic_center_selection_rejects_known_short_videos(tmp_path, monkeypatch):
    store = TopicWritingGovernanceStore(tmp_path / "governance.json")
    monkeypatch.setattr(console, "TOPIC_WRITING_GOVERNANCE_STORE", store)
    with pytest.raises(ValueError, match="不超过1分钟"):
        console._topic_center_selection_create({
            "candidate_id": "trend-short",
            "title": "短视频主题",
            "content_items": [{
                "content_id": "tikhub-douyin-short",
                "provider": "TikHub",
                "platform": "douyin",
                "content_type": "video",
                "duration_seconds": 60,
                "title": "一分钟视频",
                "url": "https://www.douyin.com/video/short",
            }],
            "primary_content_id": "tikhub-douyin-short",
        })


def test_topic_center_enrich_is_explicit_and_returns_fake_service_result(monkeypatch):
    monkeypatch.setattr(console, "_topic_center_find_candidate", lambda _id: {
        "candidate_id": "trend-2", "title": "主题二"
    })
    calls = []

    def enrich(candidate, **kwargs):
        calls.append((candidate, kwargs))
        return {"status": "ready", "contents": [{"content_id": "real-1"}]}

    monkeypatch.setattr(console.TOPIC_INTELLIGENCE, "enrich", enrich)
    result = console._topic_center_enrich("trend-2", {"platforms": ["douyin"], "limit": 3})
    assert result["status"] == "ready"
    assert calls[0][1]["platforms"] == ["douyin"]
    assert calls[0][1]["limit"] == 3


def test_topic_center_preview_truncates_real_source_content_without_persisting(monkeypatch):
    monkeypatch.setattr(console, "_topic_center_find_candidate", lambda _id: {
        "candidate_id": "trend-preview",
        "title": "真实新闻",
        "url": "https://example.test/article/1",
        "platform": "wallstreetcn-hot",
    })
    body = "正文段落。" * 120
    monkeypatch.setattr(console, "collect_topic_source", lambda selection, timeout: {
        "status": "ready",
        "content_type": "html_text",
        "title": selection["title"],
        "content": body,
    })

    result = console._topic_center_preview("trend-preview")

    assert result["status"] == "ready"
    assert result["content_preview_source"] == "source_html_preview"
    assert len(result["content_preview"]) == 360
    assert "content" not in result


def test_topic_center_preview_defers_social_platforms_without_fetching(monkeypatch):
    monkeypatch.setattr(console, "_topic_center_find_candidate", lambda _id: {
        "candidate_id": "trend-social",
        "title": "社交内容",
        "url": "https://www.zhihu.com/question/1",
        "platform": "zhihu",
    })
    called = []
    monkeypatch.setattr(console, "collect_topic_source", lambda *_args, **_kwargs: called.append(True))

    result = console._topic_center_preview("trend-social")

    assert result["status"] == "deferred"
    assert not called


def test_topic_collection_prefers_marked_article_body_over_page_chrome(monkeypatch):
    body = "正文段落一。" * 30
    html = (
        "<html><head><title>页面标题</title></head><body>"
        "<nav>首页 推荐 热门标题</nav>"
        "<article><header>页面标题 作者 时间</header>"
        "<section class='summary'>这是摘要，不是正文。</section>"
        "<section class='_articleBody_v7w04_1 article'><p>"
        + body
        + "</p></section></article></body></html>"
    )
    monkeypatch.setattr(topic_collection, "_fetch_html", lambda _url, timeout: (200, _url, html))

    result = topic_collection.collect_topic_source({
        "selection_id": "preview-source",
        "source_url": "https://example.test/article/1",
        "platform": "wallstreetcn-hot",
        "title": "页面标题",
    })

    assert result["status"] == "ready"
    assert result["content"].startswith("正文段落一")
    assert "首页 推荐" not in result["content"]
    assert "这是摘要" not in result["content"]


def test_topic_center_refresh_schedule_saves_interval_and_starts_immediate_refresh(tmp_path, monkeypatch):
    schedule_path = tmp_path / "trendradar_refresh_schedule.json"
    monkeypatch.setattr(console, "TOPIC_CENTER_REFRESH_SCHEDULE_PATH", schedule_path)
    started = []
    monkeypatch.setattr(console, "_start_trendradar_scheduler", lambda: started.append("scheduler"))
    monkeypatch.setattr(console, "_trigger_trendradar_refresh", lambda: True)

    result = console._topic_center_refresh_schedule({"interval_hours": 6})

    assert result["status"] == "ready"
    assert result["interval_hours"] == 6
    assert result["refresh_started"] is True
    assert started == ["scheduler"]
    saved = json.loads(schedule_path.read_text(encoding="utf-8"))
    assert saved["interval_hours"] == 6
    assert saved["last_run_status"] == "queued"


def test_topic_center_refresh_schedule_rejects_unsupported_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "TOPIC_CENTER_REFRESH_SCHEDULE_PATH", tmp_path / "schedule.json")
    try:
        console._topic_center_refresh_schedule({"interval_hours": 30})
    except ValueError as exc:
        assert "每小时" in str(exc)
    else:
        raise AssertionError("unsupported interval should be rejected")


def test_production_page_uses_only_topic_center_api_and_has_no_example_payload():
    page = console._topic_center_production_page().decode("utf-8")
    assert "/api/topic-center/topics" in page
    assert "?intent=" in page
    assert "loadedQuery" in page
    assert "loadCachedContents" in page
    assert "fallback_to_latest" in page
    assert "ticCrawlInterval" in page
    assert "每小时" in page
    assert "每6小时" in page
    assert "每12小时" in page
    assert "setInterval" in page
    assert "latest_crawl" in page
    assert "/api/topic-center/refresh-schedule" in page
    assert "ticRefreshInterval" not in page
    assert "页面自动检测" not in page
    assert "/api/topic-center/topics/" in page
    assert "/api/topic-center/selections" in page
    assert "source_content_id=" in page
    assert "CONTENT_READY" in page
    assert "采集完成后进入文案" in page
    assert "adoptedCandidateIds" in page
    assert "已从自动推荐列表移出" in page
    assert "已采用主题不会再次出现在自动推荐列表" in page
    assert "tic-topic-query" in page
    assert "data-enrich-topic" in page
    assert "查询相关内容" in page
    assert "loadCache:false" in page
    assert "tic-topic-heat" in page
    assert "tic-content-preview" in page
    assert "图文预览（接口返回）" in page
    assert "/preview" in page
    assert "tic-topic-preview" in page
    assert "预览正文" in page
    assert "duration_seconds" in page
    assert "不超过1分钟" in page
    assert "时长未返回" in page
    assert "tic-query-toolbar" in page
    assert "<strong>热点侦测</strong>" in page
    assert "<small>TrendRadar → TikHub</small>" in page
    assert "<b>02</b><div><strong>主素材确认</strong>" in page
    assert "<b>03</b><div><strong>进入创作</strong>" in page
    assert "<strong>自动发现</strong>" not in page
    assert "<strong>相关内容</strong>" not in page
    assert "example.com" not in page
    assert "模拟" not in page


def test_writing_page_handoff_requires_content_ready_source_and_prefills_from_query():
    page = console._writing_governance_page().decode("utf-8")
    assert "new URLSearchParams(location.search).get('source_content_id')" in page
    assert "已从选题中心自动带入" in page
    assert "尚未达到 CONTENT_READY，已阻断生成" in page
    assert "payload.source_content_id=currentSource.source_content_id" in page


def test_topic_center_account_watchlist_is_local_and_deduplicated(tmp_path):
    from workflow_1256.topic_intelligence import TopicIntelligenceService
    service = TopicIntelligenceService.__new__(TopicIntelligenceService)
    result = service.save_watchlist(tmp_path / "accounts.json", [
        {"platform": "douyin", "account_id": "u1", "name": "账号一"},
        {"platform": "douyin", "account_id": "u1", "name": "重复"},
        {"platform": "unknown", "account_id": "bad"},
    ])
    assert result["saved"] == 1
    assert json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))["accounts"][0]["account_id"] == "u1"


def test_topic_collection_owns_platform_detection_helpers():
    from workflow_1256.topic_collection import detect_source_platform, utc_now
    assert detect_source_platform("https://www.douyin.com/video/1") == "douyin"
    assert detect_source_platform("https://zhuanlan.zhihu.com/p/1") == "zhihu"
    assert "+00:00" in utc_now()
