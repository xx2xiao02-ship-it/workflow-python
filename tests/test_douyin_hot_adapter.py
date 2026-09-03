from __future__ import annotations

import asyncio

from tools import douyin_hot_adapter as adapter


def test_classify_hot_title_is_auditable_keyword_rule() -> None:
    assert adapter.classify_hot_title("人工智能模型发布") == "科技趋势"
    assert adapter.classify_hot_title("某地法院回应") == "社会观察"
    assert adapter.classify_hot_title("没有命中规则的标题") == "综合热点"


def test_fetch_hot_search_normalizes_to_selection_source(monkeypatch) -> None:
    async def fake_fetch(_url, _params, *, timeout=20.0):
        return {
            "data": {
                "word_list": [
                    {"word": "人工智能新进展", "position": 1, "hot_value": 123456},
                    {"word": "第二条", "position": 2, "hot_value": 100},
                ]
            }
        }

    monkeypatch.setattr(adapter, "_fetch_json", fake_fetch)
    result = asyncio.run(adapter.fetch_douyin_board("hot_search", limit=2))

    assert result["status"] == "ready"
    assert result["board"] == "hot_search"
    assert len(result["items"]) == 2
    item = result["items"][0]
    assert item["source_type"] == "douyin_hot"
    assert item["metadata"]["rank"] == 1
    assert item["metadata"]["hot_value"] == 123456
    assert item["metadata"]["collector_route"] == "douyin_hot_search"
    assert item["url"].startswith("https://www.douyin.com/search/")


def test_unsupported_board_is_rejected() -> None:
    try:
        asyncio.run(adapter.fetch_douyin_board("unknown"))
    except adapter.DouyinHotSourceError as exc:
        assert "不支持" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("unsupported board should fail")


def test_fetch_music_accepts_root_level_music_list(monkeypatch) -> None:
    async def fake_fetch(_url, _params, *, timeout=20.0):
        return {"music_list": [{"music_info": {"title": "测试音乐", "author": "作者", "id": 12}, "heat": 456}]}

    monkeypatch.setattr(adapter, "_fetch_json", fake_fetch)
    result = asyncio.run(adapter.fetch_douyin_board("music", limit=1))

    assert result["status"] == "ready"
    assert result["items"][0]["title"] == "测试音乐"
    assert result["items"][0]["metadata"]["hot_value"] == 456
