from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from workflow_1256.ffmpeg_bgm_merge_transport import (
    FfmpegBgmMergeTransport,
    FfmpegBgmMergeTransportError,
)


def _request() -> dict:
    return {
        "audio_urls": ["https://example.test/a.mp3", "https://example.test/b.mp3"],
        "timelines": [
            {"start": 0, "end": 10_000_000},
            {"start": 10_000_000, "end": 20_000_000},
        ],
        "transition_schemes": ["cut"],
    }


def _publisher(audio: bytes, filename: str, metadata: dict) -> dict:
    assert audio
    assert filename.endswith(".mp3")
    assert metadata["timeline_end"] == 20_000_000
    return {"url": "https://storage.example.test/" + filename}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_merge_with_synthetic_mp3_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    output = tmp_path / "merged.mp3"
    subprocess = __import__("subprocess")
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=10",
            "-codec:a",
            "libmp3lame",
            str(source),
        ],
        check=True,
    )
    raw = source.read_bytes()
    urls = {"https://example.test/a.mp3": raw, "https://example.test/b.mp3": raw}
    transport = FfmpegBgmMergeTransport(
        downloader=urls.__getitem__,
        publisher=_publisher,
        temp_root=tmp_path,
    )
    result = transport(_request())
    assert result["audio_url_list"] == [result["audio_url"]]
    assert result["duration"] == 20.0


def test_rejects_missing_publisher() -> None:
    with pytest.raises(FfmpegBgmMergeTransportError, match="发布器"):
        FfmpegBgmMergeTransport(ffmpeg_path="ffmpeg")


def test_rejects_invalid_transition_count() -> None:
    transport = FfmpegBgmMergeTransport(
        ffmpeg_path="ffmpeg",
        downloader=lambda _: b"x",
        publisher=_publisher,
    )
    with pytest.raises(FfmpegBgmMergeTransportError):
        transport({**_request(), "transition_schemes": []})
