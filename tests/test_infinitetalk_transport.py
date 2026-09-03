from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from workflow_1256.infinitetalk_transport import (
    InfiniteTalkConfig,
    InfiniteTalkValidationError,
    RunningHubInfiniteTalkTransport,
    _find_task_id,
    build_payload,
    load_infinite_talk_auth_from_document,
    normalize_api_key,
)


KEY = "a" * 32


def test_load_infinite_talk_auth_from_user_document_shape(tmp_path: Path) -> None:
    document = tmp_path / "auth.md"
    document.write_text(
        "## RUNNINGHUB\n" + KEY + "\n\n"
        "## TOS\n{\n"
        '  "ak": "access",\n  "sk": "secret",\n'
        '  "bucket": "bucket-a",\n  "endpoint": "https://tos.example",\n  "region": "cn-test"\n}\n',
        encoding="utf-8",
    )
    values = load_infinite_talk_auth_from_document(document)
    assert values == {
        "api_key": KEY,
        "tos_access_key": "access",
        "tos_secret_key": "secret",
        "tos_bucket": "bucket-a",
        "tos_endpoint": "https://tos.example",
        "tos_region": "cn-test",
    }


@dataclass
class Response:
    status_code: int
    body: dict
    content: bytes = b""
    headers: dict[str, str] | None = None

    def json(self):
        return self.body


class FakeHTTP:
    def __init__(self, query_bodies=None):
        self.posts = []
        self.query_bodies = list(query_bodies or [])

    def get(self, url, **kwargs):
        if url.endswith("image.jpg"):
            from io import BytesIO
            from PIL import Image
            output = BytesIO()
            Image.new("RGB", (2, 2), (255, 0, 0)).save(output, format="JPEG")
            return Response(200, {}, output.getvalue(), {"Content-Type": "image/jpeg"})
        return Response(200, {}, b"ID3fake-audio", {"Content-Type": "audio/mpeg"})

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/media/upload/binary"):
            filename = kwargs["files"]["file"][0]
            return Response(200, {"code": 0, "data": {"fileName": f"uploaded/{filename}"}})
        if "/run/ai-app/" in url:
            return Response(200, {"code": 0, "taskId": "task-1"})
        body = self.query_bodies.pop(0) if self.query_bodies else {"status": "RUNNING"}
        return Response(200, body)


def test_build_payload_preserves_supplied_source_node_ids() -> None:
    payload = build_payload(image_file="img", audio_file="audio", prompt="说话")
    assert [item["nodeId"] for item in payload["nodeInfoList"]] == ["284", "312", "125", "314", "369", "370"]
    assert payload["nodeInfoList"][3]["fieldValue"] == "说话"


def test_create_uploads_media_once_and_returns_task_without_exposing_key() -> None:
    http = FakeHTTP()
    transport = RunningHubInfiniteTalkTransport(
        InfiniteTalkConfig(api_key=KEY, health_check_seconds=0),
        requests_module=http,
        sleep=lambda _: None,
    )
    result = transport.create({"image": "https://example.invalid/image.jpg", "audio": "https://example.invalid/audio.mp3", "api_key": KEY})
    assert result == {"task_ID": "task-1", "status": "QUEUED", "msg": "任务已创建，交由查询节点继续处理", "output_url": "", "cost_coins": "0", "cost_time": "0"}
    assert len(http.posts) == 3
    assert KEY not in repr(result)


def test_query_success_extracts_video_and_usage() -> None:
    http = FakeHTTP([{"status": "SUCCESS", "results": [{"outputType": "mp4", "url": "https://example.invalid/result.mp4"}], "usage": {"consumeCoins": 3, "taskCostTime": 12}}])
    transport = RunningHubInfiniteTalkTransport(InfiniteTalkConfig(api_key=KEY, max_wait_seconds=1), requests_module=http, sleep=lambda _: None, clock=lambda: 0)
    result = transport.query({"taskId": "task-1", "api_key": KEY})
    assert result["status"] == "SUCCESS"
    assert result["output_url"].endswith("result.mp4")
    assert result["cost_coins"] == "3"


def test_query_uses_staged_polling_until_terminal_success() -> None:
    http = FakeHTTP([
        {"status": "RUNNING"},
        {"status": "RUNNING"},
        {"status": "RUNNING"},
        {"status": "RUNNING"},
        {"status": "SUCCESS", "results": [{"outputType": "mp4", "url": "https://example.invalid/result.mp4"}]},
    ])
    now = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    transport = RunningHubInfiniteTalkTransport(
        InfiniteTalkConfig(api_key=KEY),
        requests_module=http,
        sleep=sleep,
        clock=clock,
    )
    result = transport.query({"taskId": "task-1"})

    assert result["status"] == "SUCCESS"
    assert sleeps == [60.0, 60.0, 60.0, 30.0]


def test_query_failed_and_missing_task_are_explicit() -> None:
    http = FakeHTTP([{"status": "FAILED", "failedReason": {"message": "bad input"}}])
    transport = RunningHubInfiniteTalkTransport(InfiniteTalkConfig(api_key=KEY), requests_module=http, sleep=lambda _: None, clock=lambda: 0)
    assert transport.query({"task_ID": "task-1"})["status"] == "FAILED"
    with pytest.raises(InfiniteTalkValidationError):
        transport.query({})


def test_query_statusless_runninghub_error_is_terminal_failure() -> None:
    http = FakeHTTP([{"errorCode": "1004", "errorMessage": "Task not found"}])
    transport = RunningHubInfiniteTalkTransport(
        InfiniteTalkConfig(api_key=KEY, max_wait_seconds=1),
        requests_module=http,
        sleep=lambda _: None,
        clock=lambda: 0,
    )
    result = transport.query({"task_ID": "expired-task"})
    assert result["status"] == "FAILED"
    assert result["cost_coins"] == "0"


def test_api_key_is_not_silently_accepted_in_wrong_format() -> None:
    with pytest.raises(InfiniteTalkValidationError):
        normalize_api_key("short")


def test_create_matches_original_parameter_normalization_and_download_headers() -> None:
    http = FakeHTTP()
    transport = RunningHubInfiniteTalkTransport(
        InfiniteTalkConfig(api_key=KEY, health_check_seconds=0),
        requests_module=http,
        sleep=lambda _: None,
    )
    transport.create({
        "image": "https://p26-bot-workflow-sign.byteimg.com/image.jpg",
        "audio": "https://example.invalid/audio.mp3",
        "Max_side_length": "-12",
        "intensity": "not-a-number",
        "Scale": "oops",
        "instanceType": "unsupported",
    })
    run_payload = http.posts[-1][1]["json"]
    assert run_payload["instanceType"] == "plus"
    assert run_payload["nodeInfoList"][1]["fieldValue"] == "1080"
    assert run_payload["nodeInfoList"][3]["fieldValue"] == "人物自然说话，身体保持稳定，配合轻微自然手势"
    assert run_payload["nodeInfoList"][4]["fieldValue"] == "0"
    assert run_payload["nodeInfoList"][5]["fieldValue"] == "1.05"
    assert http.posts[0][0].endswith("/media/upload/binary")
    # The first download is the Coze/ByteDance image URL and must carry the same Referer as the source plugin.
    # FakeHTTP does not retain GET arguments, so a dedicated probe verifies it below.


def test_download_byteimg_uses_coze_referer() -> None:
    class ProbeHTTP(FakeHTTP):
        def __init__(self):
            super().__init__()
            self.gets = []

        def get(self, url, **kwargs):
            self.gets.append((url, kwargs))
            return super().get(url, **kwargs)

    http = ProbeHTTP()
    transport = RunningHubInfiniteTalkTransport(
        InfiniteTalkConfig(api_key=KEY, health_check_seconds=0),
        requests_module=http,
        sleep=lambda _: None,
    )
    transport.create({
        "image": "https://p26-bot-workflow-sign.byteimg.com/image.jpg",
        "audio": "https://example.invalid/audio.mp3",
    })
    assert http.gets[0][1]["headers"]["Referer"] == "https://www.coze.cn/"


def test_find_task_id_supports_nested_current_api_envelope() -> None:
    assert _find_task_id({"code": 0, "data": {"task": {"task_id": "task-nested"}}}) == "task-nested"
    assert _find_task_id({"data": [{"taskID": "task-list"}]}) == "task-list"
