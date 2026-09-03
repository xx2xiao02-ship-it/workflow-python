from __future__ import annotations

from pathlib import Path

from workflow_1256.topic_intelligence import TopicIntelligenceService


class _Trend:
    def health(self):
        return {"status": "ready", "provider": "TrendRadar"}

    def candidates(self, **kwargs):
        return {"status": "ready", "candidates": [{"candidate_id": "t-1", "title": "AI 提效", "score": 0.9}]}


class _TikHub:
    def health(self):
        return {"status": "ready", "auth_configured": True}

    def search_related(self, keyword, **kwargs):
        return {"status": "ready", "keyword": keyword, "contents": [{"content_id": "c-1", "title": keyword}]}


def test_topics_are_auto_recommended_from_trendradar():
    service = TopicIntelligenceService(_Trend(), _TikHub(), Path("topic-cache-test.json"))
    result = service.topics(intent="AI")
    assert result["auto_recommended"] is True
    assert result["topics"][0]["topic_id"] == "t-1"


def test_enrich_is_cached_after_first_explicit_call(tmp_path):
    service = TopicIntelligenceService(_Trend(), _TikHub(), tmp_path / "cache.json")
    candidate = {"candidate_id": "t-1", "title": "AI 提效"}
    first = service.enrich(candidate, platforms=["douyin"])
    second = service.enrich(candidate, platforms=["douyin"])
    assert first["cached"] is False
    assert second["cached"] is True
    assert service.cached_enrich("t-1", platforms=["douyin"])["contents"]


def test_cached_malformed_id_only_content_is_hidden_without_rewriting_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    service = TopicIntelligenceService(_Trend(), _TikHub(), cache_path)
    candidate = {"candidate_id": "t-1", "title": "AI 提效"}
    service.enrich(candidate, platforms=["douyin"])
    document = cache_path.read_text(encoding="utf-8")
    document = document.replace('"title": "AI 提效"', '"title": "abcdef1234567890abcdef1234567890",\n        "url": ""')
    cache_path.write_text(document, encoding="utf-8")
    result = service.cached_enrich("t-1", platforms=["douyin"])
    assert result["contents"] == []
    assert "缺少可读标题" in result["cache_warning"]
    assert '"title": "abcdef1234567890abcdef1234567890"' in cache_path.read_text(encoding="utf-8")


def test_sanitize_hides_cached_videos_not_over_one_minute_without_deleting_cache():
    service = TopicIntelligenceService.__new__(TopicIntelligenceService)
    result = service._sanitize_result({
        "status": "ready",
        "contents": [
            {
                "content_id": "short",
                "content_type": "video",
                "title": "短视频",
                "url": "https://example.test/video/short",
                "duration_seconds": 60,
            },
            {
                "content_id": "long",
                "content_type": "video",
                "title": "长视频",
                "url": "https://example.test/video/long",
                "duration_seconds": 61,
            },
        ],
    })
    assert [item["content_id"] for item in result["contents"]] == ["long"]
    assert result["filtered_short_videos"] == 1
    assert "不超过1分钟" in result["cache_warning"]
