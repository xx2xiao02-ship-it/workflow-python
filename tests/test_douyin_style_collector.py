from types import SimpleNamespace

import pytest

import workflow_1256.douyin_style_collector as douyin_style_collector
from workflow_1256.douyin_style_collector import (
    MONITOR_ROOT,
    DouyinCollectorError,
    _transcribe_videos,
    _load_spider_runtime,
    _run_fetch,
    collect_creator,
    extract_douyin_share_url,
    fetch_creator_name,
    parse_creator_url,
    parse_video_url,
    resolve_case_video_url,
    resolve_creator_url,
)


def test_parse_douyin_urls():
    assert parse_creator_url("https://www.douyin.com/user/MS4wLjABAAAA") == "MS4wLjABAAAA"


def test_load_spider_runtime_uses_monitor_venv_dependencies():
    site_packages = MONITOR_ROOT / ".venv" / "Lib" / "site-packages"
    if not site_packages.is_dir():
        pytest.skip("项目虚拟环境不存在")

    spider_class = _load_spider_runtime()

    assert spider_class.__name__ == "DouyinSpider"
    assert str(site_packages) in __import__("sys").path


def test_run_fetch_has_a_hard_deadline():
    class StalledSpider:
        async def fetch(self, _sec_uid):
            import asyncio
            await asyncio.sleep(1)

    with pytest.raises(DouyinCollectorError, match="超时"):
        _run_fetch(StalledSpider(), "MS4wLjABAAAA", timeout_seconds=0)
    assert parse_video_url("https://www.douyin.com/video/123456789") == "123456789"


def test_run_fetch_keeps_videos_collected_before_timeout():
    partial_video = SimpleNamespace(video_id="987654321")

    class PartialStalledSpider:
        videos = [partial_video]
        profile = SimpleNamespace(nickname="部分结果博主")
        _error = None
        _scroll_count = 3

        async def fetch(self, _sec_uid):
            import asyncio
            await asyncio.sleep(2)
            return self.videos

    diagnostics = {}
    videos = _run_fetch(
        PartialStalledSpider(), "MS4wLjABAAAA", timeout_seconds=1, diagnostics=diagnostics,
    )

    assert videos == [partial_video]
    assert diagnostics["fetch_status"] == "partial_timeout"
    assert diagnostics["partial_video_count"] == 1


def test_collect_creator_returns_partial_result_after_timeout():
    videos = [
        SimpleNamespace(
            video_id="987654321", title="已获取作品", create_time=1, duration_ms=4_000,
            video_url="https://cdn.example.com/a.mp4",
        )
    ]

    class PartialStalledSpider:
        def __init__(self):
            self.videos = videos
            self.profile = SimpleNamespace(nickname="部分结果博主")
            self._error = None
            self._scroll_count = 2

        async def fetch(self, _sec_uid):
            import asyncio
            await asyncio.sleep(2)
            return self.videos

    result = collect_creator(
        "https://www.douyin.com/user/MS4wLjABAAAA",
        limit=1,
        target_samples=1,
        crawl_limit=1,
        fetch_timeout_seconds=1,
        spider_factory=PartialStalledSpider,
        transcript_provider=lambda _received: "这是已经获取的有效转写样本。" * 10,
    )

    assert result["collection_status"] == "partial"
    assert len(result["items"]) == 1
    assert result["transcript_ready_count"] == 1
    assert result["diagnostics"]["partial_result"] is True


def test_extract_and_resolve_creator_share_text():
    share_text = "5- 长按复制此条消息，打开抖音搜索，查看TA的更多作品。 https://v.douyin.com/LXcmxpG28jQ/ 4@9.com :2pm"

    class FakeResponse:
        def geturl(self):
            return "https://www.douyin.com/user/MS4wLjABAAAA?from=share"

        def close(self):
            pass

    assert extract_douyin_share_url(share_text) == "https://v.douyin.com/LXcmxpG28jQ/"
    assert resolve_creator_url(share_text, opener=lambda _request: FakeResponse()).startswith("https://www.douyin.com/user/MS4wLjABAAAA")
    assert resolve_creator_url("https://www.douyin.com/user/MS4wLjABAAAA") == "https://www.douyin.com/user/MS4wLjABAAAA"


def test_resolve_iesdouyin_creator_share_url():
    class FakeResponse:
        def geturl(self):
            return "https://www.iesdouyin.com/share/user/MS4wLjABAAAA8CXv?sec_uid=MS4wLjABAAAA8CXv"

        def close(self):
            pass

    assert resolve_creator_url("https://v.douyin.com/LXcmxpG28jQ/", opener=lambda _request: FakeResponse()) == "https://www.douyin.com/user/MS4wLjABAAAA8CXv"


def test_rejects_short_link_that_resolves_to_a_video():
    class FakeResponse:
        def geturl(self):
            return "https://www.douyin.com/video/123456789"

        def close(self):
            pass

    with pytest.raises(DouyinCollectorError, match="单条视频"):
        resolve_creator_url("https://v.douyin.com/abc/", opener=lambda _request: FakeResponse())


def test_resolves_case_video_share_text_to_canonical_url():
    share_text = "8.46 复制打开抖音，看看【鹤老师的作品】一招解决实体流量 https://v.douyin.com/5uROHPfwTmQ/ L@J.vf"

    class FakeResponse:
        def geturl(self):
            return "https://www.douyin.com/video/7123456789012345678?from=share"

        def close(self):
            pass

    assert resolve_case_video_url(share_text, opener=lambda _request: FakeResponse()) == "https://www.douyin.com/video/7123456789012345678"


def test_resolves_tikhub_iesdouyin_video_share_url_to_canonical_url():
    share_url = "https://www.iesdouyin.com/share/video/7678258031926283535/?region=US"

    assert extract_douyin_share_url(share_url) == share_url
    assert resolve_case_video_url(share_url) == "https://www.douyin.com/video/7678258031926283535"


def test_collect_creator_uses_login_spider_and_transcript_provider():
    video = SimpleNamespace(
        video_id="123456789", title="测试标题", create_time=1, duration_ms=12_000, video_url="https://cdn.example.com/a.mp4"
    )

    class FakeSpider:
        _error = None
        profile = SimpleNamespace(nickname="测试博主")

        async def fetch(self, sec_uid):
            assert sec_uid == "MS4wLjABAAAA"
            return [video]

    result = collect_creator(
        "https://www.douyin.com/user/MS4wLjABAAAA",
        limit=30,
        spider_factory=FakeSpider,
        transcript_provider=lambda received: "这是用于风格蒸馏的足量抖音转写文本。" * 5,
    )
    assert result["collection_status"] == "completed"
    assert result["creator_name"] == "测试博主"
    assert result["items"][0]["transcript_status"] == "local_whisper"
    assert result["transcript_ready_count"] == 1


def test_fetch_creator_name_uses_collector_profile():
    class FakeSpider:
        _error = None
        profile = SimpleNamespace(nickname="真实博主")

        async def fetch(self, _sec_uid):
            return [SimpleNamespace(video_id="1")]

    assert fetch_creator_name("https://www.douyin.com/user/MS4wLjABAAAA", spider_factory=FakeSpider) == "真实博主"


def test_collect_creator_uses_deeper_crawl_limit_for_a_valid_sample_target():
    videos = [
        SimpleNamespace(
            video_id=f"{index:09d}", title="测试标题", create_time=1, duration_ms=12_000, video_url="https://cdn.example.com/a.mp4"
        )
        for index in range(70)
    ]

    class FakeSpider:
        _error = None

        async def fetch(self, _sec_uid):
            return videos

    result = collect_creator(
        "https://www.douyin.com/user/MS4wLjABAAAA",
        limit=30,
        target_samples=30,
        spider_factory=FakeSpider,
        transcript_provider=lambda _received: "用于风格蒸馏的有效文本。" * 20,
    )

    assert result["target_samples"] == 30
    assert result["crawl_limit"] == 60
    assert result["listed_count"] == 60
    assert result["transcript_ready_count"] == 30
    assert result["diagnostics"]["transcription"] == {
        "candidate_total": 60,
        "candidate_scanned_count": 30,
        "target_samples": 30,
        "valid_sample_count": 30,
        "stopped_at_target": True,
    }


def test_collect_creator_skips_videos_over_ten_minutes_before_transcription():
    videos = [
        SimpleNamespace(video_id="short", title="短视频", create_time=1, duration_ms=600_000, video_url="short.mp4"),
        SimpleNamespace(video_id="long", title="超长视频", create_time=1, duration_ms=600_001, video_url="long.mp4"),
    ]
    transcribed = []

    class FakeSpider:
        _error = None
        profile = SimpleNamespace(nickname="测试博主")

        async def fetch(self, _sec_uid):
            return videos

    def transcript_provider(video):
        transcribed.append(video.video_id)
        return "这是足够长的有效风格样本。" * 10

    result = collect_creator(
        "https://www.douyin.com/user/MS4wLjABAAAA",
        limit=1,
        target_samples=1,
        crawl_limit=2,
        spider_factory=FakeSpider,
        transcript_provider=transcript_provider,
    )

    assert transcribed == ["short"]
    assert result["raw_listed_count"] == 2
    assert result["listed_count"] == 1
    assert result["skipped_long_video_count"] == 1
    assert result["skipped_long_videos"][0]["video_id"] == "long"
    assert result["items"][0]["video_id"] == "short"
    assert any("超过 10 分钟" in warning for warning in result["warnings"])


def test_collect_creator_applies_five_minute_policy_before_transcription():
    videos = [
        SimpleNamespace(video_id="five-minutes", title="五分钟边界", create_time=1, duration_ms=300_000, video_url="five.mp4"),
        SimpleNamespace(video_id="over-five-minutes", title="超过五分钟", create_time=1, duration_ms=300_001, video_url="over.mp4"),
    ]
    transcribed = []

    class FakeSpider:
        _error = None
        profile = SimpleNamespace(nickname="测试博主")

        async def fetch(self, _sec_uid):
            return videos

    def transcript_provider(video):
        transcribed.append(video.video_id)
        return "这是足够长的有效风格样本。" * 10

    result = collect_creator(
        "https://www.douyin.com/user/MS4wLjABAAAA",
        limit=1,
        target_samples=1,
        crawl_limit=2,
        max_video_duration_seconds=300,
        spider_factory=FakeSpider,
        transcript_provider=transcript_provider,
    )

    assert transcribed == ["five-minutes"]
    assert result["max_video_duration_seconds"] == 300
    assert result["listed_count"] == 1
    assert result["skipped_long_videos"][0]["video_id"] == "over-five-minutes"
    assert any("超过 5 分钟" in warning for warning in result["warnings"])


def test_resume_recounts_processed_position_after_skipping_long_video():
    videos = [
        SimpleNamespace(video_id="short-1", title="短视频1", create_time=1, duration_ms=1000, video_url="1.mp4"),
        SimpleNamespace(video_id="short-2", title="短视频2", create_time=1, duration_ms=1000, video_url="2.mp4"),
        SimpleNamespace(video_id="long", title="超长视频", create_time=1, duration_ms=600001, video_url="long.mp4"),
        SimpleNamespace(video_id="short-3", title="短视频3", create_time=1, duration_ms=1000, video_url="3.mp4"),
    ]
    transcribed = []

    class FakeSpider:
        _error = None
        profile = SimpleNamespace(nickname="测试博主")

        async def fetch(self, _sec_uid):
            return videos

    def transcript_provider(video):
        transcribed.append(video.video_id)
        return "这是已恢复后的有效样本。" * 10

    result = collect_creator(
        "https://www.douyin.com/user/MS4wLjABAAAA",
        limit=1,
        target_samples=3,
        crawl_limit=4,
        spider_factory=FakeSpider,
        checkpoint={
            "creator_name": "测试博主",
            "candidates": [
                {"video_id": video.video_id, "title": video.title, "duration_ms": video.duration_ms, "video_url": video.video_url}
                for video in videos
            ],
            "processed_count": 3,
            "transcripts": {
                "short-1": "这是已经完成的有效样本。" * 10,
                "short-2": "这是已经完成的有效样本。" * 10,
            },
        },
        transcript_provider=transcript_provider,
    )

    assert transcribed == ["short-3"]
    assert result["transcript_ready_count"] == 3


def test_transcription_progress_distinguishes_target_samples_from_candidate_scan(monkeypatch):
    videos = [
        SimpleNamespace(video_id=str(index), video_url="https://cdn.example.com/a.mp4")
        for index in range(1, 91)
    ]
    transcribed_ids = []
    progress = []

    class FakeFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_transcribe(audio_path):
        transcribed_ids.append(audio_path)
        return [{"text": "这是可用于风格蒸馏的有效转写。" * 8}]

    monkeypatch.setattr(
        douyin_style_collector,
        "_load_transcriber_runtime",
        lambda: (FakeFetcher, lambda *_args: None, lambda *_args: None, fake_transcribe),
    )

    diagnostics = {}
    transcripts, warnings = _transcribe_videos(
        videos,
        target_samples=50,
        progress_reporter=lambda stage, percent, message: progress.append((stage, percent, message)),
        transcription_diagnostics=diagnostics,
    )

    assert not warnings
    assert len(transcripts) == 50
    assert len(transcribed_ids) == 50
    assert diagnostics == {
        "candidate_total": 90,
        "candidate_scanned_count": 50,
        "target_samples": 50,
        "valid_sample_count": 50,
        "stopped_at_target": True,
    }
    assert any("有效样本 0/50；候选扫描 1/90" in message for _stage, _percent, message in progress)
    assert progress[-1][2] == "有效样本已达到 50/50；已扫描候选 50/90，停止继续转写"


def test_transcription_resumes_after_a_persisted_checkpoint_without_repeating_completed_candidates(monkeypatch):
    videos = [
        SimpleNamespace(video_id=str(index), video_url="https://cdn.example.com/a.mp4")
        for index in range(1, 7)
    ]
    transcribed_ids = []
    checkpoints = []

    class FakeFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_transcribe(audio_path):
        transcribed_ids.append(audio_path)
        return [{"text": "这是可用于风格蒸馏的有效转写。" * 8}]

    monkeypatch.setattr(
        douyin_style_collector,
        "_load_transcriber_runtime",
        lambda: (FakeFetcher, lambda *_args: None, lambda *_args: None, fake_transcribe),
    )

    transcripts, warnings = _transcribe_videos(
        videos,
        target_samples=4,
        resume_transcripts={
            "1": "这是已落盘的有效转写样本。" * 8,
            "2": "这是已落盘的有效转写样本。" * 8,
        },
        resume_processed_count=2,
        checkpoint_reporter=lambda processed, saved, saved_warnings: checkpoints.append((processed, saved, saved_warnings)),
    )

    assert not warnings
    assert len(transcribed_ids) == 2
    assert len(transcripts) == 4
    assert checkpoints[-1][0] == 4
    assert set(checkpoints[-1][1]) == {"1", "2", "3", "4"}


def test_transcription_item_reporter_receives_explicit_creator_id(monkeypatch):
    video = SimpleNamespace(video_id="1", title="测试作品", video_url="https://cdn.example.com/a.mp4")

    class FakeFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        douyin_style_collector,
        "_load_transcriber_runtime",
        lambda: (FakeFetcher, lambda *_args: None, lambda *_args: None,
                 lambda _audio_path: [{"text": "这是用于验证逐篇落盘的有效转写文本。" * 8}]),
    )
    reported = []

    transcripts, warnings = _transcribe_videos(
        [video],
        target_samples=1,
        creator_id="MS4wLjABAAAA",
        item_reporter=reported.append,
    )

    assert not warnings
    assert transcripts["1"]
    assert reported[0]["creator_id"] == "MS4wLjABAAAA"
