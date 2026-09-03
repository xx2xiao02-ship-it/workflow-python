from workflow_1256.governance.invalidation import (
    build_tts_fingerprint,
    transactional_tts_rebuild,
)


def test_tts_fingerprint_changes_when_voice_or_audio_changes():
    base = dict(
        text="一段旁白",
        voice_key="qingcang_2",
        speaker_id="speaker-a",
        resource_id="seed-tts-2.0",
        model="seed-tts-2.0-standard",
        speed_ratio=1.1,
        performance={"pitch": 0},
        audio_sha256=["a"],
        actual_durations=[1.2],
    )
    first = build_tts_fingerprint(**base)
    assert first != build_tts_fingerprint(**{**base, "voice_key": "custom_voice"})
    assert first != build_tts_fingerprint(**{**base, "audio_sha256": ["b"]})


def test_failed_rebuild_preserves_active_state_and_records_scope():
    events = []
    current = {"active_version": "old", "tts_fingerprint": "old", "timeline": [{"start": 0, "end": 1}]}

    def rebuild(_state, _scope):
        raise RuntimeError("TTS 失败")

    result = transactional_tts_rebuild(
        current,
        new_fingerprint="new",
        rebuild=rebuild,
        event_sink=events.append,
        group_id="g01",
    )
    assert result["status"] == "failed_preserved"
    assert result["state"]["active_version"] == "old"
    assert result["state"]["tts_fingerprint"] == "old"
    assert result["state"]["invalidation"]["status"] == "failed_preserved"
    assert result["event"]["affected"] == [
        "tts", "stt", "timeline", "captions", "shot_timeline",
        "assets", "digital_human", "edit",
    ]
    assert events[0]["status"] == "failed_preserved"


def test_successful_rebuild_switches_active_version_atomically():
    result = transactional_tts_rebuild(
        {"active_version": "old", "tts_fingerprint": "old"},
        new_fingerprint="new",
        rebuild=lambda state, _scope: {**state, "candidate": True, "active_version": "new"},
    )
    assert result["status"] == "committed"
    assert result["state"]["active_version"] == "new"
    assert result["state"]["tts_fingerprint"] == "new"


def test_successful_rebuild_cannot_retain_old_active_version_from_candidate():
    result = transactional_tts_rebuild(
        {"active_version": "old", "tts_fingerprint": "old"},
        new_fingerprint="new",
        rebuild=lambda state, _scope: {**state, "active_version": "old", "timeline": "rebuilt"},
    )
    assert result["status"] == "committed"
    assert result["state"]["active_version"] == "new"
    assert result["state"]["timeline"] == "rebuilt"


def test_deferred_rebuild_keeps_old_active_until_downstream_commit():
    events = []
    result = transactional_tts_rebuild(
        {"active_version": "old", "tts_fingerprint": "old", "timeline": "old-timeline"},
        new_fingerprint="new",
        pending_version="candidate-1",
        defer_commit=True,
        event_sink=events.append,
        rebuild=lambda state, _scope: {
            **state,
            "active_version": "new",
            "tts_fingerprint": "new",
            "timeline": "candidate-timeline",
        },
    )

    assert result["status"] == "replacement_pending"
    assert result["state"]["active_version"] == "old"
    assert result["state"]["tts_fingerprint"] == "old"
    assert result["state"]["pending_version"] == "candidate-1"
    assert result["state"]["timeline"] == "candidate-timeline"
    assert result["event"]["status"] == "replacement_pending"
    assert events == [result["event"]]


def test_deferred_rebuild_failure_preserves_old_active_version():
    result = transactional_tts_rebuild(
        {"active_version": "old", "tts_fingerprint": "old"},
        new_fingerprint="new",
        pending_version="candidate-2",
        defer_commit=True,
        rebuild=lambda _state, _scope: (_ for _ in ()).throw(RuntimeError("下游失败")),
    )

    assert result["status"] == "failed_preserved"
    assert result["state"]["active_version"] == "old"
    assert result["state"]["tts_fingerprint"] == "old"
    assert result["state"]["invalidation"]["status"] == "failed_preserved"
