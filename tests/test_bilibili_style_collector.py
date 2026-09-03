from urllib.parse import parse_qs, urlparse

from workflow_1256.bilibili_style_collector import collect_creator, parse_creator_url, parse_video_url


def _requester(url, _headers, _timeout):
    parsed = urlparse(url)
    if parsed.path.endswith("/x/space/arc/search"):
        return {"code": 0, "data": {"list": {"vlist": [{"bvid": "BV1abc", "title": "样本视频", "created": 1}]}}}
    if parsed.path.endswith("/x/web-interface/view"):
        return {"code": 0, "data": {"cid": 99, "title": "样本视频", "pubdate": 1, "duration": 66}}
    if parsed.path.endswith("/x/player/v2"):
        assert parse_qs(parsed.query)["bvid"] == ["BV1abc"]
        return {"code": 0, "data": {"subtitle": {"subtitles": [{"subtitle_url": "https://subtitle.example.com/a.json"}]}}}
    if parsed.netloc == "subtitle.example.com":
        return {"body": [{"content": "这是第一句字幕。"}, {"content": "这是第二句字幕。"}]}
    raise AssertionError(url)


def test_parse_bilibili_urls():
    assert parse_creator_url("https://space.bilibili.com/12345") == "12345"
    assert parse_video_url("https://www.bilibili.com/video/BV1abc") == "BV1abc"


def test_collect_creator_builds_traceable_subtitle_manifest():
    result = collect_creator("https://space.bilibili.com/12345", limit=30, requester=_requester)
    assert result["collection_status"] == "completed"
    assert result["items"][0]["source_url"].endswith("BV1abc")
    assert result["items"][0]["transcript_status"] == "official_subtitle"
    assert "第一句" in result["items"][0]["transcript"]


def test_collect_creator_skips_long_listing_before_subtitle_request():
    calls = []

    def requester(url, _headers, _timeout):
        calls.append(url)
        parsed = urlparse(url)
        if parsed.path.endswith("/x/space/arc/search"):
            return {
                "code": 0,
                "data": {"list": {"vlist": [
                    {"bvid": "BVshort", "title": "短视频", "created": 1, "length": "05:00"},
                    {"bvid": "BVlong", "title": "长视频", "created": 1, "length": "05:01"},
                ]}},
            }
        if parsed.path.endswith("/x/web-interface/view"):
            assert parse_qs(parsed.query)["bvid"] == ["BVshort"]
            return {"code": 0, "data": {"cid": 99, "title": "短视频", "pubdate": 1, "duration": 300}}
        if parsed.path.endswith("/x/player/v2"):
            return {"code": 0, "data": {"subtitle": {"subtitles": []}}}
        raise AssertionError(url)

    result = collect_creator(
        "https://space.bilibili.com/12345",
        limit=2,
        requester=requester,
        max_video_duration_seconds=300,
    )

    assert [item["bvid"] for item in result["items"]] == ["BVshort"]
    assert result["max_video_duration_seconds"] == 300
    assert result["skipped_long_videos"][0]["bvid"] == "BVlong"
    assert not any("BVlong" in url for url in calls if "/x/web-interface/view" in url)
