from __future__ import annotations

from workflow_1256.infinite_talk_media import InfiniteTalkMediaTransport


def upload(content, filename, mime):
    return {"success": True, "url": f"https://media.invalid/{filename}", "bucket": "test"}


def splitter(url, start, end, kind):
    return f"{kind}:{url}:{start}:{end}".encode()


def merger(urls):
    return ("|".join(urls).encode(), 2.5)


def test_host_audio_split_preserves_one_to_one_order_and_microseconds() -> None:
    transport = InfiniteTalkMediaTransport(splitter=splitter, uploader=upload)
    result = transport.host_audio_split({"links": ["a", "b"], "host_audio_timeline": [{"start": 0, "end": 1_000_000}, {"start": 2_000_000, "end": 3_500_000}]})
    assert result["status"] == "success"
    assert [item["index"] for item in result["segments"]] == [0, 1]
    assert result["segments"][1]["duration_sec"] == 1.5


def test_video_split_preserves_source_timeline() -> None:
    transport = InfiniteTalkMediaTransport(splitter=splitter, uploader=upload)
    result = transport.video_split({"video_url": "https://video.invalid/a.mp4", "timeline": [{"start": 0, "end": 2_000_000}]})
    assert result["url_list"][0].startswith("https://media.invalid/video/seg_0_")
    assert result["segments"][0]["start"] == 0


def test_merge_audio_urls_returns_capcut_plugin_shape() -> None:
    transport = InfiniteTalkMediaTransport(merger=merger, uploader=upload)
    result = transport.merge_audio_urls({"audio_urls": ["a", "b"]})
    assert result["status"] == "success"
    assert result["duration"] == 2.5
    assert result["timeline"] == [{"start": 0, "end": 2_500_000}]
