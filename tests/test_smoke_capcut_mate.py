from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "skills" / "daily-update-te" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import smoke_capcut_mate  # noqa: E402


def test_single_video_url_builds_one_second_infos() -> None:
    infos = json.loads(
        smoke_capcut_mate._single_media_infos(
            "http://host.docker.internal:30123/sample.mp4",
            kind="video",
        )
    )

    assert infos == [{
        "video_url": "http://host.docker.internal:30123/sample.mp4",
        "start": 0,
        "end": 1_000_000,
        "duration": 1_000_000,
        "volume": 0,
    }]


def test_single_audio_url_builds_one_second_infos() -> None:
    infos = json.loads(
        smoke_capcut_mate._single_media_infos(
            "https://example.invalid/sample.mp3",
            kind="audio",
        )
    )

    assert infos[0]["audio_url"].endswith("sample.mp3")
    assert infos[0]["volume"] == 0.8


def test_single_media_url_rejects_local_path() -> None:
    with pytest.raises(ValueError, match="http/https"):
        smoke_capcut_mate._single_media_infos("C:\\media\\sample.mp4", kind="video")


def test_field_summary_counts_json_items_without_exposing_values() -> None:
    result = smoke_capcut_mate._field_summary(
        "effect_infos:暗角",
        {"infos": '[{"effect_type": "暗角"}]'},
        "infos",
    )

    assert result == {
        "step": "effect_infos:暗角",
        "ok": True,
        "keys": ["infos"],
        "field": "infos",
        "item_count": 1,
    }


def test_array_summary_requires_every_requested_array() -> None:
    result = smoke_capcut_mate._array_summary(
        "audio_timelines",
        {
            "timelines": [{"start": 0, "end": 1_000_000}],
            "all_timelines": [{"start": 0, "end": 1_000_000}],
        },
        "timelines",
        "all_timelines",
    )

    assert result["ok"] is True
    assert result["array_counts"] == {"timelines": 1, "all_timelines": 1}
