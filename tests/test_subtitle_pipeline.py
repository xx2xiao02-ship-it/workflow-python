from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow_1256.capcut_stt_transport import (
    CapCutSTTConfig,
    CapCutSTTError,
    CapCutSTTTransport,
)
from workflow_1256.capcut_stt_subtitle_pipeline import run_capcut_stt_subtitle_pipeline
from workflow_1256.subtitle_correction import (
    SubtitleCorrectionError,
    SubtitleCorrectionTransport,
    load_subtitle_correction_channels,
)


class _Upload:
    vid = "vid-1"
    md5 = "md5-1"
    duration_ms = 3_000


class _Utterance:
    def __init__(self, text: str, start_time: int, end_time: int):
        self.text = text
        self.start_time = start_time
        self.end_time = end_time
        self.words = []


class _Subtitle:
    utterances = [_Utterance("带动村子支付", 280, 1_600), _Utterance("褚橙", 1_600, 2_800)]


class _FakeCapCutClient:
    def upload_audio(self, _path: Path):
        return _Upload()

    def create_stt_task(self, **_kwargs):
        return {"data": {"tasks": [{"id": "task-1", "token": "token-1"}]}}

    def query_stt_task(self, _task_id: str, _token: str):
        return {"data": {"tasks": [{"id": "task-1", "status": "succeed", "payload": "{}"}]}}

    def extract_subtitles(self, _response):
        return _Subtitle()


def test_capcut_stt_accepts_succeed_and_converts_ms_to_us(tmp_path: Path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"mp3")
    transport = CapCutSTTTransport(
        client=_FakeCapCutClient(),
        config=CapCutSTTConfig(timeout_seconds=1, poll_interval_seconds=0.01),
        sleep=lambda _seconds: None,
    )
    captions, report = transport.transcribe_groups([
        {
            "group_id": "g01",
            "audio_path": str(audio),
            "reference_text": "带动村子致富，褚橙。",
        }
    ])
    assert report["status"] == "succeeded"
    assert [item["caption_id"] for item in captions] == ["g01.stt.001", "g01.stt.002"]
    assert captions[0]["start_us"] == 280_000
    assert captions[0]["end_us"] == 1_600_000
    assert captions[1]["start_us"] == 1_600_000
    assert captions[1]["end_us"] == 2_800_000
    assert report["group_timelines"] == [{"start": 0, "end": 3_000_000}]
    assert report["total_timeline"] == {"start": 0, "end": 3_000_000}
    assert report["timeline_source"] == "capcut_stt_upload_duration_and_utterances"


def test_capcut_stt_subtitle_pipeline_uses_tts_audio_and_keeps_stt_timing(tmp_path: Path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"mp3")

    class FakeSTT:
        def transcribe_groups(self, groups):
            assert groups[0]["audio_path"] == str(audio.resolve())
            assert "start_us" not in groups[0]
            assert "end_us" not in groups[0]
            return ([{
                "caption_id": "g01.stt.001",
                "group_id": "g01",
                "text": "带动村子支付",
                "start_us": 280_000,
                "end_us": 1_600_000,
                "reference_text": "带动村子致富",
            }], {
                "status": "succeeded",
                "provider": "capcut_stt",
                "tasks": [],
                "group_timelines": [{"start": 0, "end": 3_000_000}],
                "total_timeline": {"start": 0, "end": 3_000_000},
            })

    class FakeCorrection:
        def correct(self, captions):
            assert captions[0]["start_us"] == 280_000
            corrected = [dict(captions[0], text="带动村子致富")]
            return corrected, {"status": "succeeded", "provider": "mini", "caption_count": 1}

    result = run_capcut_stt_subtitle_pipeline(
        segment_text=["带动村子致富"],
        audio_sources=[str(audio)],
        stt_transport=FakeSTT(),
        correction_transport=FakeCorrection(),
    )
    assert result["new_segments"] == ["带动村子致富"]
    assert result["new_timelines"] == [{"start": 280_000, "end": 1_600_000}]
    assert result["subtitle_source"] == "capcut_stt_doubao_mini"
    assert result["pipeline"]["caption_count"] == 1
    assert result["group_timelines"] == [{"start": 0, "end": 3_000_000}]
    assert result["total_timeline"] == {"start": 0, "end": 3_000_000}
    assert result["pipeline"]["timeline_source"] == "capcut_stt_upload_duration_and_utterances"
    performance = result["pipeline"]["performance"]
    assert performance["audio_materialize_elapsed_ms"] >= 0
    assert performance["stt_elapsed_ms"] >= 0
    assert performance["correction_elapsed_ms"] >= 0
    assert performance["total_elapsed_ms"] >= performance["stt_elapsed_ms"]


def test_capcut_stt_rejects_overlapping_utterances(tmp_path: Path) -> None:
    class OverlappingSubtitle:
        utterances = [_Utterance("第一句", 0, 1_000), _Utterance("第二句", 900, 1_500)]

    class Client(_FakeCapCutClient):
        def extract_subtitles(self, _response):
            return OverlappingSubtitle()

    audio = tmp_path / "stt_overlap_fixture.mp3"
    audio.write_bytes(b"mp3")
    transport = CapCutSTTTransport(
        client=Client(),
        config=CapCutSTTConfig(timeout_seconds=1, poll_interval_seconds=0.01),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(CapCutSTTError, match="时间顺序错误或窗口重叠"):
        transport.transcribe_groups([{"group_id": "g01", "audio_path": str(audio)}])


def test_capcut_stt_pipeline_rejects_correction_timeline_mutation(tmp_path: Path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"mp3")

    class FakeSTT:
        def transcribe_groups(self, _groups):
            return ([{
                "caption_id": "g01.stt.001",
                "group_id": "g01",
                "text": "原文",
                "start_us": 100_000,
                "end_us": 900_000,
                "reference_text": "原文",
            }], {
                "status": "succeeded",
                "group_timelines": [{"start": 0, "end": 1_000_000}],
                "total_timeline": {"start": 0, "end": 1_000_000},
            })

    class FakeCorrection:
        def correct(self, captions):
            return [dict(captions[0], text="校正文", end_us=950_000)], {"status": "succeeded"}

    with pytest.raises(CapCutSTTError, match="不得修改剪映 STT 时间线"):
        run_capcut_stt_subtitle_pipeline(
            segment_text=["原文"],
            audio_sources=[str(audio)],
            stt_transport=FakeSTT(),
            correction_transport=FakeCorrection(),
        )


def test_capcut_stt_pipeline_rejects_candidate_order_and_overlap(tmp_path: Path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"mp3")

    class FakeSTT:
        def transcribe_groups(self, _groups):
            return ([
                {"caption_id": "g01.stt.001", "group_id": "g01", "text": "一", "start_us": 100_000, "end_us": 700_000},
                {"caption_id": "g01.stt.002", "group_id": "g01", "text": "二", "start_us": 600_000, "end_us": 900_000},
            ], {
                "status": "succeeded",
                "group_timelines": [{"start": 0, "end": 1_000_000}],
                "total_timeline": {"start": 0, "end": 1_000_000},
            })

    with pytest.raises(CapCutSTTError, match="重叠或时间顺序错误"):
        run_capcut_stt_subtitle_pipeline(
            segment_text=["原文"],
            audio_sources=[str(audio)],
            stt_transport=FakeSTT(),
            correction_transport=lambda captions: captions,
        )


def _write_api_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "groups": {
                    "story-writing": {
                        "endpoint": "https://story.test/responses",
                        "timeout_seconds": 3,
                        "model_slots": {"custom_model_primary": "ep-mini-primary"},
                        "model_names": {"custom_model_primary": "Doubao-Seed-2.0-mini"},
                        "model_order": ["custom_model_primary"],
                    },
                    "director-seed21": {
                        "endpoint": "https://director.test/chat/completions",
                        "timeout_seconds": 3,
                        "model_slots": {"custom_model_backup": "ep-mini-backup"},
                        "model_names": {"custom_model_backup": "Doubao-Seed-2.0-mini"},
                        "model_order": ["custom_model_backup"],
                    },
                },
                "api_groups": {"language-model": {"order": ["story-writing", "director-seed21"]}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_subtitle_correction_uses_mini_order_and_fails_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "api_management.json"
    _write_api_config(config_path)
    monkeypatch.setenv("STORY_WRITER_ARK_API_KEY", "primary-key")
    monkeypatch.setenv("VOICE_DIRECTOR_PRIMARY_API_KEY", "backup-key")
    channels = load_subtitle_correction_channels(config_path=config_path)
    assert [item.channel_id for item in channels] == ["story-writing", "director-seed21"]
    assert [item.model_id for item in channels] == ["ep-mini-primary", "ep-mini-backup"]

    calls: list[tuple[str, str, dict]] = []

    def requester(url, headers, payload, timeout):
        calls.append((url, headers["Authorization"], dict(payload)))
        if len(calls) == 1:
            raise SubtitleCorrectionError("HTTP 429")
        return {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "items": [{"caption_id": "g01.stt.001", "text": "带动村子致富"}]
                    }, ensure_ascii=False)
                }
            }]
        }

    transport = SubtitleCorrectionTransport.from_api_management(
        config_path=config_path,
        requester=requester,
    )
    corrected, report = transport.correct([
        {
            "caption_id": "g01.stt.001",
            "group_id": "g01",
            "text": "带动村子支付",
            "reference_text": "带动村子致富",
            "start_us": 280_000,
            "end_us": 1_600_000,
        }
    ])
    assert corrected[0]["text"] == "带动村子致富"
    assert corrected[0]["start_us"] == 280_000
    assert report["provider"] == "director-seed21"
    assert report["failover"] is True
    assert len(calls) == 2


def test_subtitle_correction_rejects_changed_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "api_management.json"
    _write_api_config(config_path)
    monkeypatch.setenv("STORY_WRITER_ARK_API_KEY", "primary-key")
    monkeypatch.setenv("VOICE_DIRECTOR_PRIMARY_API_KEY", "backup-key")

    def requester(_url, _headers, _payload, _timeout):
        return {
            "output_text": json.dumps({
                "items": [
                    {"caption_id": "g01.stt.002", "text": "二"},
                    {"caption_id": "g01.stt.001", "text": "一"},
                ]
            }, ensure_ascii=False)
        }

    transport = SubtitleCorrectionTransport.from_api_management(
        config_path=config_path,
        requester=requester,
    )
    with pytest.raises(SubtitleCorrectionError, match="主/备用"):
        transport.correct([
            {"caption_id": "g01.stt.001", "text": "一", "start_us": 0, "end_us": 1},
            {"caption_id": "g01.stt.002", "text": "二", "start_us": 1, "end_us": 2},
        ])
