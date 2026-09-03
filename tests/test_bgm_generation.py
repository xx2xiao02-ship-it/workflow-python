from __future__ import annotations

import pytest

from workflow_1256.bgm_generation import (
    BgmGenerationTransportRequired,
    BgmGenerationValidationError,
    run_bgm_generation,
)
from workflow_1256.ark_audio_transport import ArkAudioConfig, ArkAudioHTTPTransport


def _transport(*, code: int = 0):
    calls = []

    def requester(_url, _headers, payload, _timeout):
        calls.append(payload)
        return {
            "body": {
                "code": code,
                "duration": float(payload["text_prompt"].count("秒")),
                "url": f"https://example.invalid/bgm-{len(calls)}.mp3",
            },
            "headers": {"X-Tt-Logid": f"log-{len(calls)}"},
        }

    return ArkAudioHTTPTransport(
        ArkAudioConfig(api_key="key-not-real"),
        requester=requester,
    ), calls


def test_bgm_generation_preserves_task_and_url_order() -> None:
    transport, calls = _transport()
    result = run_bgm_generation(
        {
            "timelines": [
                {"start": 0, "end": 40_000_000},
                {"start": 40_000_000, "end": 80_000_000},
            ],
            "music_cues": [
                {"music_role": "推进", "emotion": "稳定", "energy": 3},
                {"music_role": "收束", "emotion": "平静", "energy": 2},
            ],
        },
        transport=transport.generate_bgm,
    )

    assert len(result["bgm_tasks"]) == len(result["AudioUrl_list"])
    assert result["AudioUrl_list"] == [
        f"https://example.invalid/bgm-{index}.mp3"
        for index in range(1, len(result["bgm_tasks"]) + 1)
    ]
    assert calls[0]["model"] == "seed-audio-1.0-multilingual"
    assert "不要人声" in calls[0]["text_prompt"]


def test_bgm_generation_requires_transport() -> None:
    with pytest.raises(BgmGenerationTransportRequired):
        run_bgm_generation({"timelines": [], "music_cues": []})


def test_bgm_generation_resumes_existing_task_ids_without_create():
    created_calls = []
    queried_calls = []

    def create(_task):
        created_calls.append(True)
        raise AssertionError("resume must not create a new BGM task")

    def query(task, task_id):
        queried_calls.append((task, task_id))
        return {"data": {"SongDetail": {"AudioUrl": f"https://example.invalid/{task_id}.mp3"}}}

    result = run_bgm_generation(
        {
            "timelines": [{"start": 0, "end": 40_000_000}],
            "music_cues": [{"music_role": "推进", "emotion": "稳定", "energy": 3}],
        },
        transport=create,
        query_transport=query,
        existing_task_ids=["persisted-bgm-task"],
    )

    assert not created_calls
    assert [item[1] for item in queried_calls] == ["persisted-bgm-task"]
    assert result["AudioUrl_list"] == ["https://example.invalid/persisted-bgm-task.mp3"]


def test_bgm_generation_accepts_documented_success_code_20000000() -> None:
    transport, _calls = _transport(code=20_000_000)
    result = run_bgm_generation(
        {
            "timelines": [{"start": 0, "end": 40_000_000}],
            "music_cues": [{"music_role": "鎺ㄨ繘", "emotion": "绋冲畾", "energy": 3}],
        },
        transport=transport.generate_bgm,
    )
    assert result["AudioUrl_list"] == ["https://example.invalid/bgm-1.mp3"]


def test_bgm_generation_rejects_missing_song_url() -> None:
    with pytest.raises(BgmGenerationValidationError, match="AudioUrl"):
        run_bgm_generation(
            {
                "timelines": [{"start": 0, "end": 40_000_000}],
                "music_cues": [{"music_role": "推进", "energy": 3}],
            },
            transport=lambda _task: {"code": 0, "data": {"SongDetail": {}}},
        )
