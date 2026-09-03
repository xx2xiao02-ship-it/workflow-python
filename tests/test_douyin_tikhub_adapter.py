from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_1256.douyin_style_collector import collect_creator_tikhub
from workflow_1256.douyin_tikhub_adapter import (
    DOUYIN_HIGH_QUALITY_PLAY_URL_PATH,
    DOUYIN_USER_POST_VIDEOS_PATH,
    TikHubDouyinAdapter,
    TikHubUserVideosPage,
    TikHubVideo,
    normalize_tikhub_video,
)


class _RedirectResponse:
    def __init__(self, url: str):
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url


def test_short_profile_link_is_expanded_before_tikhub_resolution():
    calls = []

    def requester(method, endpoint, *, params):
        calls.append((method, endpoint, params))
        return {"data": {"sec_user_id": "sec-from-tikhub"}}

    def share_opener(request, timeout):
        assert request.full_url == "https://v.douyin.com/short/"
        assert timeout == 15
        return _RedirectResponse("https://www.douyin.com/user/sec-from-redirect")

    adapter = TikHubDouyinAdapter(requester=requester, share_url_opener=share_opener)
    assert adapter.resolve_sec_user_id("https://v.douyin.com/short/") == "sec-from-redirect"
    assert calls == []


def test_short_profile_link_failure_is_explicit_and_does_not_call_tikhub():
    calls = []

    def requester(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    def share_opener(_request, timeout):
        raise OSError("TLS failure")

    adapter = TikHubDouyinAdapter(requester=requester, share_url_opener=share_opener)
    try:
        adapter.resolve_sec_user_id("https://v.douyin.com/short/")
    except Exception as exc:
        assert getattr(exc, "code", "") == "short_link_unresolved"
    else:
        raise AssertionError("expected short_link_unresolved")
    assert calls == []


def test_normalize_video_never_uses_music_play_url():
    normalized = normalize_tikhub_video(
        {
            "aweme_id": "aweme-1",
            "desc": "测试作品",
            "music": {"play_url": {"url_list": ["https://cdn.example/music.mp3"]}},
            "video": {"play_addr": {"url_list": ["https://cdn.example/video-1.mp4"]}},
        }
    )
    assert normalized is not None
    assert normalized.video_url == "https://cdn.example/video-1.mp4"
    assert "music" not in normalized.media_source


def test_adapter_paginates_and_deduplicates_aweme_ids():
    calls: list[tuple[str, str, dict]] = []

    def requester(method, endpoint, *, params):
        calls.append((method, endpoint, params))
        cursor = str(params.get("max_cursor"))
        if cursor == "0":
            return {"data": {"aweme_list": [
                {"aweme_id": "1", "desc": "一", "video": {"play_addr": {"url_list": ["https://cdn/1.mp4"]}}},
                {"aweme_id": "2", "desc": "二", "video": {"play_addr": {"url_list": ["https://cdn/2.mp4"]}}},
            ], "max_cursor": 2, "has_more": True}}
        return {"data": {"aweme_list": [
            {"aweme_id": "2", "desc": "二", "video": {"play_addr": {"url_list": ["https://cdn/2.mp4"]}}},
            {"aweme_id": "3", "desc": "三", "video": {"play_addr": {"url_list": ["https://cdn/3.mp4"]}}},
        ], "max_cursor": 3, "has_more": False}}

    adapter = TikHubDouyinAdapter(requester=requester)
    first = adapter.fetch_user_post_videos("sec-demo", max_cursor=0, count=50)
    second = adapter.fetch_user_post_videos("sec-demo", max_cursor=first.max_cursor, count=50)
    assert [item.aweme_id for item in first.items] == ["1", "2"]
    assert [item.aweme_id for item in second.items] == ["2", "3"]
    assert calls[0][1] == DOUYIN_USER_POST_VIDEOS_PATH
    assert "cookie" not in calls[0][2]


class _MediaResponse:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.headers = {"Content-Type": "video/mp4"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        data, self.payload = self.payload, b""
        return data


def test_download_refreshes_expired_url_by_aweme_id(tmp_path: Path):
    requested: list[tuple[str, dict]] = []

    def requester(method, endpoint, *, params):
        requested.append((endpoint, params))
        assert endpoint == DOUYIN_HIGH_QUALITY_PLAY_URL_PATH
        return {"data": {"play_url": "https://cdn.example/fresh.mp4"}}

    def opener(request, timeout):
        if "stale" in request.full_url:
            raise OSError("expired")
        return _MediaResponse(b"\x00\x00\x00\x18ftypisom" + b"x" * 2048)

    adapter = TikHubDouyinAdapter(requester=requester, media_opener=opener)
    result = adapter.download_media("aweme-1", tmp_path / "video.mp4", media_url="https://cdn.example/stale.mp4")
    assert result["media_url_refreshed"] is True
    assert result["media_url"] == "https://cdn.example/fresh.mp4"
    assert requested == [(DOUYIN_HIGH_QUALITY_PLAY_URL_PATH, {"aweme_id": "aweme-1"})]


def test_tikhub_collection_persists_stable_checkpoint_and_reuses_completed_items():
    class Adapter:
        def __init__(self):
            self.page_calls = []
            self.downloaded = []

        def diagnostics(self):
            return {"call_count": len(self.page_calls)}

        def resolve_sec_user_id(self, _url):
            return "sec-demo"

        def fetch_user_post_videos(self, sec_user_id, *, max_cursor=0, count=50):
            self.page_calls.append(str(max_cursor))
            rows = [
                TikHubVideo("1", title="一", video_url="https://cdn/1.mp4", author_name="测试账号"),
                TikHubVideo("2", title="二", video_url="https://cdn/2.mp4", author_name="测试账号"),
            ]
            return TikHubUserVideosPage(sec_user_id, rows, max_cursor="2", has_more=False)

        def fetch_video_media_url(self, aweme_id, **_kwargs):
            return f"https://cdn/{aweme_id}.mp4"

        def download_media(self, aweme_id, output_path, **_kwargs):
            self.downloaded.append(aweme_id)
            Path(output_path).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 2048)
            return {"size_bytes": 2060, "sha256": "test"}

    def extract_audio(_video, audio):
        Path(audio).write_bytes(b"wav")

    def transcribe(_audio):
        return [{"text": "这是足够长的本地转写样本。" * 12}]

    adapter = Adapter()
    checkpoints = []
    reports = []
    result = collect_creator_tikhub(
        "https://www.douyin.com/user/sec-demo",
        limit=2,
        target_samples=2,
        adapter=adapter,
        transcriber_runtime=(extract_audio, transcribe),
        checkpoint_reporter=checkpoints.append,
        item_reporter=reports.append,
        account_id="account-demo",
        source_id="source-demo",
    )
    assert result["collection_status"] == "completed"
    assert result["transcript_ready_count"] == 2
    assert adapter.downloaded == ["1", "2"]
    assert {item["aweme_id"] for item in reports} == {"1", "2"}
    assert set(checkpoints[-1]) == {
        "account_id", "sec_user_id", "max_cursor", "aweme_id",
        "completed_work_ids", "transcript_status", "source_id", "evidence_set_id",
    }
    assert "video_url" not in json.dumps(checkpoints, ensure_ascii=False)

    resumed = collect_creator_tikhub(
        "https://www.douyin.com/user/sec-demo",
        limit=2,
        target_samples=2,
        adapter=adapter,
        transcriber_runtime=(extract_audio, transcribe),
        checkpoint=checkpoints[-1],
        account_id="account-demo",
    )
    assert resumed["transcript_ready_count"] == 2
    assert adapter.downloaded == ["1", "2"]
