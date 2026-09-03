from __future__ import annotations

import json

import pytest

from workflow_1256.tikhub_transport import TikHubConfig, TikHubTransport, TikHubTransportError, _normalize_item


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def test_health_does_not_call_external_service_without_token():
    calls = []
    transport = TikHubTransport(TikHubConfig(token=""), opener=lambda *args, **kwargs: calls.append(args))
    health = transport.health()
    assert health["status"] == "blocked"
    assert health["paid_call"] is False
    assert calls == []


def test_search_related_uses_official_paths_and_normalizes_metrics():
    calls = []

    def opener(request, timeout):
        calls.append((request.method, request.full_url, request.data))
        return _Response({
            "data": {
                "items": [{
                    "aweme_id": "123",
                    "desc": "AI 提效真实案例",
                    "share_url": "https://www.douyin.com/video/123",
                    "author": {"nickname": "测试作者"},
                    "statistics": {"digg_count": 9},
                }]
            }
        })

    transport = TikHubTransport(TikHubConfig(token="token", max_retries=0), opener=opener)
    result = transport.search_related("AI 提效", platforms=["douyin"], limit=5)
    assert result["status"] == "ready"
    assert result["contents"][0]["provider"] == "TikHub"
    assert result["contents"][0]["url"] == "https://www.douyin.com/video/123"
    assert result["contents"][0]["title"] == "AI 提效真实案例"
    assert result["contents"][0]["metrics"]["likes"] == 9
    assert calls[0][0] == "POST"
    assert "/api/v1/douyin/search/fetch_video_search_v2" in calls[0][1]
    request_body = json.loads(calls[0][2].decode("utf-8"))
    assert request_body["cursor"] == 0
    assert request_body["sort_type"] == "0"
    assert request_body["publish_time"] == "0"
    assert request_body["filter_duration"] == "0"
    assert request_body["content_type"] == "0"
    assert result["contents"][0]["duration_seconds"] is None


def test_search_related_filters_videos_not_over_one_minute_using_real_duration():
    def opener(request, timeout):
        return _Response({
            "data": {
                "items": [
                    {
                        "aweme_id": "short-45",
                        "desc": "短视频",
                        "duration": 45000,
                        "share_url": "https://www.douyin.com/video/short-45",
                    },
                    {
                        "aweme_id": "exact-60",
                        "desc": "临界视频",
                        "duration": 60000,
                        "share_url": "https://www.douyin.com/video/exact-60",
                    },
                    {
                        "aweme_id": "long-90",
                        "desc": "长视频",
                        "duration": 90000,
                        "share_url": "https://www.douyin.com/video/long-90",
                    },
                ]
            }
        })

    transport = TikHubTransport(TikHubConfig(token="token", max_retries=0), opener=opener)
    result = transport.search_related("视频", platforms=["douyin"], limit=10)

    assert result["status"] == "ready"
    assert [item["title"] for item in result["contents"]] == ["长视频"]
    assert result["contents"][0]["duration_seconds"] == 90.0
    assert result["filtered_short_videos"] == 2
    assert result["queries"][0]["received_count"] == 3
    assert result["queries"][0]["count"] == 1
    assert result["queries"][0]["filtered_short_videos"] == 2
    assert "不超过1分钟" in result["message"]


def test_duration_clock_text_is_normalized_to_seconds():
    item = _normalize_item(
        {
            "type": "video",
            "id": "clock-1",
            "title": "时长文本视频",
            "url": "https://example.test/video/clock-1",
            "duration": "01:05",
        },
        platform="douyin",
        endpoint="/test",
        keyword="视频",
    )
    assert item is not None
    assert item["duration_seconds"] == 65.0
    assert item["duration_source"] == "duration"

    seconds_item = _normalize_item(
        {
            "id": "seconds-1",
            "title": "无类型但有时长字段的视频",
            "url": "https://example.test/video/seconds-1",
            "duration_seconds": 45,
        },
        platform="douyin",
        endpoint="/test",
        keyword="视频",
    )
    assert seconds_item is not None
    assert seconds_item["content_type"] == "video"
    assert seconds_item["duration_seconds"] == 45.0


def test_douyin_official_business_data_aweme_info_shape_is_normalized():
    def opener(request, timeout):
        return _Response({
            "code": 200,
            "message_zh": "请求成功，本次请求将被计费。",
            "business_data": [{
                "data_id": "0",
                "type": 1,
                "data": {
                    "type": 1,
                    "aweme_info": {
                        "aweme_id": "987654321",
                        "desc": "恒大债权包相关视频",
                        "create_time": 1760000000,
                        "author": {"nickname": "真实作者"},
                        "statistics": {
                            "digg_count": 123,
                            "comment_count": 8,
                            "share_count": 4,
                        },
                        "share_url": "https://www.douyin.com/video/987654321",
                    },
                },
            }],
        })

    transport = TikHubTransport(TikHubConfig(token="token", max_retries=0), opener=opener)
    result = transport.search_related("恒大债权包", platforms=["douyin"], limit=5)
    assert result["status"] == "ready"
    content = result["contents"][0]
    assert content["content_id"] == "tikhub-douyin-987654321"
    assert content["title"] == "恒大债权包相关视频"
    assert content["url"] == "https://www.douyin.com/video/987654321"
    assert content["author"] == "真实作者"
    assert content["metrics"] == {"likes": 123, "comments": 8, "shares": 4}


def test_zhihu_search_wrapper_normalizes_nested_object_fields():
    def opener(request, timeout):
        return _Response({
            "data": [{
                "type": "search_result",
                "object": {
                    "type": "article",
                    "id": "1794abcdef1234567890",
                    "title": "知乎文章标题",
                    "url": "https://zhuanlan.zhihu.com/p/1794abcdef1234567890",
                    "author": {"name": "知乎作者"},
                    "created_time": "2026-08-27T10:00:00+08:00",
                    "voteup_count": 12,
                    "comment_count": 3,
                },
            }]
        })

    transport = TikHubTransport(TikHubConfig(token="token", max_retries=0), opener=opener)
    result = transport.search_related("知乎文章标题", platforms=["zhihu"], limit=5)
    content = result["contents"][0]
    assert result["status"] == "ready"
    assert content["title"] == "知乎文章标题"
    assert content["url"] == "https://zhuanlan.zhihu.com/p/1794abcdef1234567890"
    assert content["author"] == "知乎作者"
    assert content["published_at"] == "2026-08-27T10:00:00+08:00"
    assert content["metrics"]["likes"] == 12
    assert content["metrics"]["comments"] == 3
    assert content["content_type"] == "article"
    assert content["content_id"].endswith("1794abcdef1234567890")


def test_article_search_keeps_only_explicit_body_as_preview():
    def opener(request, timeout):
        return _Response({
            "data": [{
                "type": "search_result",
                "object": {
                    "type": "article",
                    "id": "1794bodypreview1234567890",
                    "title": "带正文的知乎文章",
                    "url": "https://zhuanlan.zhihu.com/p/1794bodypreview1234567890",
                    "content": "这是接口明确返回的文章正文。" * 30,
                },
            }]
        })

    transport = TikHubTransport(TikHubConfig(token="token", max_retries=0), opener=opener)
    content = transport.search_related("带正文的知乎文章", platforms=["zhihu"], limit=5)["contents"][0]
    assert content["content_type"] == "article"
    assert content["content_preview"].startswith("这是接口明确返回的文章正文")
    assert len(content["content_preview"]) <= 360
    assert content["content_preview_source"] == "search_response"


def test_id_only_result_is_not_exposed_as_title():
    def opener(request, timeout):
        return _Response({
            "data": [{"type": "search_result", "object": {"type": "article", "id": "abcdef1234567890"}}]
        })

    transport = TikHubTransport(TikHubConfig(token="token", max_retries=0), opener=opener)
    result = transport.search_related("知乎文章", platforms=["zhihu"], limit=5)
    assert result["contents"] == []
    assert result["status"] == "partial"


def test_wechat_requires_ghid_without_calling_external_service():
    calls = []
    transport = TikHubTransport(TikHubConfig(token="token"), opener=lambda *args, **kwargs: calls.append(args))
    result = transport.search_related("AI", platforms=["wechat"])
    assert result["status"] == "blocked"
    assert result["queries"][0]["status"] == "needs_account"
    assert calls == []


def test_missing_auth_is_explicit_error():
    transport = TikHubTransport(TikHubConfig(token=""))
    with pytest.raises(TikHubTransportError) as exc_info:
        transport._request_json("GET", "/api/v1/demo/demo/cache_status")
    assert exc_info.value.code == "auth_missing"
    assert "TIKHUB_API_TOKEN" in str(exc_info.value)
