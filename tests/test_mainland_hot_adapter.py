from __future__ import annotations

import pytest

from tools import mainland_hot_adapter as adapter


def test_normalize_weibo_payload_preserves_rank_heat_and_source_contract() -> None:
    result = adapter.normalize_weibo_payload(
        {"data": {"realtime": [{"word": "人工智能新进展", "realpos": 1, "num": 123456}]}}
    )

    assert len(result) == 1
    item = result[0]
    assert item["source_type"] == "weibo_hot"
    assert item["title"] == "人工智能新进展"
    assert item["metadata"]["rank"] == 1
    assert item["metadata"]["hot_value"] == 123456
    assert item["metadata"]["collector_route"] == "weibo_hot"


def test_normalize_toutiao_payload_reads_public_hot_board_shape() -> None:
    result = adapter.normalize_toutiao_payload(
        {"data": [{"ClusterId": 123, "Title": "科技趋势", "Url": "https://www.toutiao.com/trending/123", "HotValue": "456"}]}
    )

    assert result[0]["source_type"] == "toutiao_hot"
    assert result[0]["url"].endswith("/123")
    assert result[0]["metadata"]["hot_value"] == 456


def test_normalize_bilibili_payload_reads_popular_api_shape() -> None:
    result = adapter.normalize_bilibili_payload(
        {
            "code": 0,
            "data": {
                "list": [
                    {
                        "bvid": "BV1test",
                        "title": "AI 工具分享",
                        "desc": "简介",
                        "tname": "科技",
                        "owner": {"name": "UP 主"},
                        "stat": {"view": 789},
                    }
                ]
            },
        }
    )

    assert result[0]["source_type"] == "bilibili_hot"
    assert result[0]["url"].endswith("/BV1test")
    assert result[0]["metadata"]["hot_value"] == 789
    assert result[0]["metadata"]["author"] == "UP 主"


def test_normalize_baidu_html_reads_current_card_classes() -> None:
    pytest.importorskip("bs4")
    markup = """
    <div class="category-wrap_iQLoo">
      <a class="title_dIF3B" href="https://www.baidu.com/s?wd=测试"><div class="c-single-text-ellipsis">测试热点</div></a>
      <div class="hot-index_1Bl1a">9876</div>
      <div class="hot-desc_1m_jR">热点摘要</div>
    </div>
    """
    result = adapter.normalize_baidu_html(markup)

    assert result[0]["source_type"] == "baidu_hot"
    assert result[0]["title"] == "测试热点"
    assert result[0]["metadata"]["hot_value"] == 9876


def test_bridge_fetches_only_selected_mainland_sources(monkeypatch) -> None:
    bridge = pytest.importorskip("tools.horizon_topic_bridge")
    calls: list[list[str]] = []

    async def fake_fetch(sources=None, *, limit=50, timeout=20.0):
        calls.append(list(sources or []))
        return {"status": "ready", "items": [], "sources": []}

    monkeypatch.setattr(bridge, "fetch_mainland_hot_sources", fake_fetch)
    monkeypatch.setattr(bridge, "MAINLAND_HOT_CACHE", None)

    result = bridge._mainland_hot_snapshot(["weibo_hot"])

    assert result["status"] == "ready"
    assert calls == [["weibo_hot"]]
