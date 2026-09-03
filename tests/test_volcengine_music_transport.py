from __future__ import annotations

import json

import pytest

from workflow_1256.volcengine_music_transport import (
    VolcengineMusicConfig,
    VolcengineMusicConfigError,
    VolcengineMusicHTTPTransport,
)


def _request_payload() -> dict[str, object]:
    return {"Duration": 30, "Text": "克制的现代纪录片背景音乐，不要人声。"}


def test_bgm_uses_hmac_ak_sk_and_polls_without_changing_legacy_output():
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
    now = [0.0]
    responses = [
        {"status_code": 200, "body": {"Result": {"TaskID": "task-1"}}},
        {"status_code": 200, "body": {"Result": {"Status": 1}}},
        {
            "status_code": 200,
            "body": {
                "Result": {
                    "Status": 2,
                    "SongDetail": {"AudioUrl": "https://cdn.example/bgm.mp3", "Duration": 30},
                }
            },
        },
    ]

    def requester(url, method, headers, body, timeout):
        calls.append((url, method, dict(headers), body))
        return responses.pop(0)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    transport = VolcengineMusicHTTPTransport(
        VolcengineMusicConfig(
            access_key="ak-test",
            secret_key="sk-test",
            poll_interval=0.1,
            max_wait=2,
        ),
        requester=requester,
        clock=lambda: now[0],
        sleep=sleep,
    )
    result = transport.generate_bgm(_request_payload())

    assert result["data"]["SongDetail"]["AudioUrl"] == "https://cdn.example/bgm.mp3"
    assert result["data"]["SongDetail"]["Duration"] == 30.0
    assert result["data"]["TaskID"] == "task-1"
    assert calls[0][0].split("?", 1)[1].startswith("Action=GenBGMForTime")
    assert "Action=QuerySong" in calls[1][0]
    assert calls[0][1] == "POST"
    assert calls[1][1] == "GET"
    assert calls[0][2]["Authorization"].startswith("HMAC-SHA256 Credential=ak-test/")
    assert "sk-test" not in json.dumps(calls[0][2])
    assert json.loads(calls[0][3].decode("utf-8"))["Version"] == "v5.0"


def test_bgm_query_bgm_resumes_existing_task_without_create_call():
    calls: list[str] = []

    def requester(url, method, headers, body, timeout):
        calls.append(url)
        assert method == "GET"
        return {
            "status_code": 200,
            "body": {
                "Result": {
                    "Status": 2,
                    "SongDetail": {"AudioUrl": "https://cdn.example/resumed.mp3", "Duration": 30},
                }
            },
        }

    transport = VolcengineMusicHTTPTransport(
        VolcengineMusicConfig(access_key="ak-test", secret_key="sk-test", max_wait=2),
        requester=requester,
    )
    result = transport.query_bgm(_request_payload(), "already-created")

    assert result["data"]["TaskID"] == "already-created"
    assert result["data"]["SongDetail"]["AudioUrl"].endswith("resumed.mp3")
    assert len(calls) == 1 and "Action=QuerySong" in calls[0]


def test_bgm_does_not_reuse_tts_or_tos_credentials(monkeypatch):
    monkeypatch.delenv("ARK_BGM_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ARK_BGM_SECRET_KEY", raising=False)
    monkeypatch.setenv("ARK_TTS_API_KEY", "tts-only-test")
    monkeypatch.setenv("ARK_TTS_TOS_ACCESS_KEY", "tos-only-test")
    with pytest.raises(VolcengineMusicConfigError):
        VolcengineMusicHTTPTransport.from_env()


def test_incomplete_primary_pair_can_fail_over_to_complete_backup():
    calls: list[str] = []

    def requester(url, method, headers, body, timeout):
        calls.append(headers["Authorization"])
        if headers["Authorization"].startswith("HMAC-SHA256 Credential=ak-primary/"):
            return {"status_code": 403, "body": {"ResponseMetadata": {"Error": {"Code": "Denied", "Message": "denied"}}}}
        return {
            "status_code": 200,
            "body": {
                "Result": {
                    "Status": 2,
                    "SongDetail": {"AudioUrl": "https://cdn.example/fallback.mp3", "Duration": 30},
                }
            },
        }

    transport = VolcengineMusicHTTPTransport(
        VolcengineMusicConfig(
            access_key="ak-primary",
            secret_key="sk-primary",
            backup_access_key="ak-backup",
            backup_secret_key="sk-backup",
        ),
        requester=requester,
    )
    result = transport.generate_bgm(_request_payload())
    assert result["data"]["SongDetail"]["AudioUrl"].endswith("fallback.mp3")
    assert calls[0].startswith("HMAC-SHA256 Credential=ak-primary/")
    assert calls[1].startswith("HMAC-SHA256 Credential=ak-backup/")
