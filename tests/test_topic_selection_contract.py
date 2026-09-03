from __future__ import annotations

import pytest

from workflow_1256 import topic_collection
from workflow_1256.topic_selection_contract import build_topic_selection_package, load_queue, upsert_queue_item


def test_build_topic_selection_package_routes_zhihu_to_collection() -> None:
    item = build_topic_selection_package(
        candidate_id="abc123",
        decision="accepted",
        source_url="https://www.zhihu.com/question/123",
        title="知乎问题",
    )
    assert item["platform"] == "zhihu"
    assert item["collection"]["route"] == "zhihu_question"
    assert item["collection"]["state"] == "queued"
    assert item["writing"]["state"] == "blocked_collection_required"


def test_build_topic_selection_package_preserves_douyin_hot_route_and_metadata() -> None:
    item = build_topic_selection_package(
        candidate_id="douyin-hot-1",
        decision="accepted",
        source_url="https://www.douyin.com/search/人工智能",
        title="人工智能热点",
        source_metadata={
            "board_type": "hot_search",
            "collector_route": "douyin_hot_search",
            "hot_value": 123,
        },
    )

    assert item["platform"] == "douyin"
    assert item["collection"]["route"] == "douyin_hot_search"
    assert item["source_metadata"]["hot_value"] == 123


def test_build_topic_selection_package_rejects_non_http_source_url() -> None:
    with pytest.raises(ValueError, match="HTTP\(S\)"):
        build_topic_selection_package(
            candidate_id="unsafe",
            decision="accepted",
            source_url="javascript:alert(1)",
            title="不应写入",
        )


def test_build_topic_selection_package_rejects_malformed_rank() -> None:
    with pytest.raises(ValueError, match="rank"):
        build_topic_selection_package(
            candidate_id="bad-rank",
            decision="later",
            source_url="https://example.com/topic",
            title="稍后处理",
            rank="not-a-number",
        )


def test_upsert_queue_item_replaces_same_selection(tmp_path) -> None:
    path = tmp_path / "queue.json"
    first = build_topic_selection_package(
        candidate_id="abc123", decision="accepted", source_url="https://example.com/1", title="一"
    )
    second = {**first, "title": "二"}
    upsert_queue_item(path, first)
    upsert_queue_item(path, second)
    queue = load_queue(path)
    assert len(queue["items"]) == 1
    assert queue["items"][0]["title"] == "二"


def test_collect_topic_source_extracts_visible_html(monkeypatch) -> None:
    fixture = "<html><head><title>测试标题</title></head><body><script>隐藏</script>正文内容。" + "公开文本。" * 30 + "</body></html>"
    monkeypatch.setattr(topic_collection, "_fetch_html", lambda _url, timeout=20: (200, _url, fixture))
    result = topic_collection.collect_topic_source(
        {"selection_id": "topic-1", "source_url": "https://example.com/article", "title": "候选"}
    )
    assert result["status"] == "ready"
    assert result["content_type"] == "html_text"
    assert result["collected_at"]
    assert "隐藏" not in result["content"]
    assert len(result["content"]) >= 80


def test_detects_tikhub_iesdouyin_share_as_douyin() -> None:
    share_url = "https://www.iesdouyin.com/share/video/7678258031926283535/"

    assert topic_collection.detect_source_platform(share_url) == "douyin"


def test_collect_topic_source_marks_zhihu_http_403_blocked(monkeypatch) -> None:
    def blocked(_url, timeout=20):
        raise topic_collection.TopicCollectionError("HTTP 403")

    monkeypatch.setattr(topic_collection, "_fetch_html", blocked)
    result = topic_collection.collect_topic_source(
        {"selection_id": "topic-zhihu", "source_url": "https://www.zhihu.com/question/123", "title": "问题"}
    )
    assert result["platform"] == "zhihu"
    assert result["status"] == "blocked"
    assert result["error_code"] == "http_blocked"
    assert result["collected_at"]


def test_collect_topic_source_rejects_non_http_source_url() -> None:
    result = topic_collection.collect_topic_source(
        {
            "selection_id": "unsafe",
            "source_url": "file:///C:/secret.txt",
            "title": "不应读取本地文件",
        }
    )
    assert result["status"] == "invalid_input"
    assert result["error_code"] == "invalid_source_url"


def test_collect_topic_source_does_not_fake_douyin_hot_search_content() -> None:
    result = topic_collection.collect_topic_source(
        {
            "selection_id": "topic-douyin-hot",
            "source_url": "https://www.douyin.com/search/人工智能",
            "title": "人工智能热点",
            "source_metadata": {"collector_route": "douyin_hot_search"},
            "collection": {"route": "douyin_hot_search"},
        }
    )

    assert result["status"] == "delegated"
    assert result["error_code"] == "douyin_hot_search_collector_required"


def test_candidates_merges_current_bing_query_with_exact_douyin_intent(monkeypatch) -> None:
    """A current Bing query is merged without reusing the previous query run."""

    topic_bridge = pytest.importorskip("tools.horizon_topic_bridge")

    monkeypatch.setattr(
        topic_bridge,
        "_latest_stage",
        lambda: (
            "old-bing-run",
            "raw",
            [
                {
                    "source_type": "search",
                    "title": "许家印 判了",
                    "url": "https://example.com/old",
                    "content": "2026 年旧查询结果",
                    "metadata": {"feed_name": "Bing 新闻检索"},
                }
            ],
        ),
    )
    monkeypatch.setattr(
        topic_bridge,
        "_douyin_hot_candidates",
        lambda intent, tags, selected_sources: (
            [
                {
                    "candidate_id": "tyl-hot",
                    "title": "TYL获2026年度总冠军",
                    "source_type": "douyin_hot",
                    "source": "抖音热榜",
                    "source_name": "抖音热榜",
                    "url": "https://www.douyin.com/search/TYL",
                    "score": 100,
                    "rank": 1,
                    "content_preview": "抖音热榜",
                    "source_metadata": {"category": "综合热点"},
                }
            ],
            {"status": "ready", "captured_at": "now"},
        ),
    )
    monkeypatch.setattr(
        topic_bridge,
        "_query_fetch_run",
        lambda _query: (
            "current-bing-run",
            "raw",
            [
                {
                    "source_type": "search",
                    "title": "TYL 夺得 2026 年度总冠军：赛后报道",
                    "url": "https://example.com/current",
                    "content": "TYL 获得 2026 年度总冠军。",
                    "metadata": {"feed_name": "Bing 新闻检索"},
                }
            ],
        ),
    )

    result = topic_bridge.candidates(
        "TYL获2026年度总冠军",
        ["综合热点"],
        ["douyin_hot", "rss", "rss_36kr", "google_news", "rss_zhihu", "web_search"],
    )

    assert result["query_fallback"] is True
    assert [item["title"] for item in result["candidates"]] == [
        "TYL获2026年度总冠军",
        "TYL 夺得 2026 年度总冠军：赛后报道",
    ]
    assert not any("许家印" in item["title"] for item in result["candidates"])
