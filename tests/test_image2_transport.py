from __future__ import annotations

from collections.abc import Mapping
import base64
import io
import json

import pytest

from workflow_1256.image2_transport import (
    Image2AllHTTPClient,
    Image2RequestUncertainError,
    Image2Response,
    _default_requester,
    extract_image_result,
)


def test_async_create_omits_size_and_get_task_uses_documented_endpoint() -> None:
    calls: list[tuple[str, str, Mapping | None]] = []

    def requester(method, url, headers, payload, timeout):
        calls.append((method, url, payload))
        if method == "POST":
            return Image2Response({"code": 200, "data": {"taskId": "task-1", "state": "queued"}})
        return Image2Response({"data": {"taskId": "task-1", "state": "processing"}})

    client = Image2AllHTTPClient("test-key", requester=requester)
    client.create_image(
        prompt="测试",
        size="",
        aspect_ratio="16:9",
        resolution="1K",
    )
    client.get_task("task-1")

    assert calls[0][0] == "POST"
    assert calls[0][1] == "https://api.apimodels.app/v1/images/generations"
    assert calls[0][2] is not None
    assert "size" not in calls[0][2]
    assert calls[0][2]["aspect_ratio"] == "16:9"
    assert calls[1] == (
        "GET",
        "https://api.apimodels.app/v1/images/generations?task_id=task-1",
        None,
    )


def test_default_image2_requester_uses_requests_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    import workflow_1256.image2_transport as transport

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        headers = {"x-apimodels-task-id": "task-from-requests"}
        content = b'{"data":{"taskId":"task-from-requests","state":"queued"}}'

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse()

    def fail_urlopen(*args, **kwargs):  # pragma: no cover - failure path assertion
        raise AssertionError("urllib transport should not be used by default")

    monkeypatch.setattr(transport.requests, "request", fake_request)
    monkeypatch.setattr(transport, "urlopen", fail_urlopen)

    response = _default_requester(
        "POST",
        "https://api.apimodels.app/v1/images/generations",
        {"Authorization": "Bearer test", "Content-Type": "application/json"},
        {"model": "gpt-image-2-all", "prompt": "x"},
        180,
    )

    assert extract_image_result(response)["task_id"] == "task-from-requests"
    assert calls[0]["method"] == "POST"
    assert calls[0]["stream"] is True
    assert calls[0]["timeout"] == (30, 180.0)


def test_extract_image_result_supports_nested_result_and_header_task_id() -> None:
    response = Image2Response(
        {
            "code": 200,
            "data": {"state": "completed", "result": {"images": [{"url": ["https://img.test/a.jpg"]}]}},
        },
        response_headers={"x-apimodels-task-id": "task-from-header"},
    )

    result = extract_image_result(response)

    assert result["task_id"] == "task-from-header"
    assert result["state"] == "completed"
    assert result["image_urls"] == ["https://img.test/a.jpg"]


def test_extract_image_result_treats_http_200_body_error_as_failure() -> None:
    result = extract_image_result(
        {
            "error": {
                "code": "content_policy_violation",
                "message": "blocked",
                "task_id": "task-2",
            }
        }
    )

    assert result["state"] == "failed"
    assert result["task_id"] == "task-2"
    assert "content_policy_violation" in result["error"]


def test_image2_create_does_not_treat_http_200_body_error_as_submitted_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import run_token_assets_live as assets

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def create_image(self, **kwargs):
            return {
                "error": {
                    "code": "content_policy_violation",
                    "message": "blocked",
                    "task_id": "late-error-task",
                }
            }

    monkeypatch.setattr(assets, "Image2AllHTTPClient", FakeClient)

    with pytest.raises(RuntimeError, match="content_policy_violation"):
        assets._image2_create_with_failover(
            {"prompt": "测试", "image2_mode": "async", "aspect_ratio": "1:1"},
            [{"channel_id": "primary", "endpoint": "https://example.test/v1/", "api_keys": ["a"]}],
        )


def test_image2_http_200_body_error_with_task_id_does_not_fail_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import run_token_assets_live as assets

    calls: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append(str(kwargs.get("model_id") or "constructed"))

        def create_image(self, **kwargs):
            calls.append("create")
            return {
                "error": {
                    "code": "content_policy_violation",
                    "message": "blocked",
                    "task_id": "late-error-task",
                }
            }

    monkeypatch.setattr(assets, "Image2AllHTTPClient", FakeClient)

    with pytest.raises(RuntimeError, match="late-error-task"):
        assets._image2_create_with_failover(
            {"prompt": "测试", "image2_mode": "async", "aspect_ratio": "1:1"},
            [
                {"channel_id": "primary", "endpoint": "https://example.test/v1/", "api_keys": ["a"]},
                {"channel_id": "backup", "endpoint": "https://example.test/v1/", "api_keys": ["b"]},
            ],
        )

    assert calls == ["gpt-image-2-all", "create"]


def test_image2_create_escalates_low_to_auto_then_high_on_explicit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import run_token_assets_live as assets

    qualities: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def create_image(self, **kwargs):
            quality = str(kwargs["quality"])
            qualities.append(quality)
            if quality != "high":
                return {"data": {"state": "failed", "error": {"message": "render failed"}}}
            return {"data": {"state": "queued", "taskId": "task-high"}}

    monkeypatch.setattr(assets, "Image2AllHTTPClient", FakeClient)

    result = assets._image2_create_with_failover(
        {"prompt": "测试", "image2_mode": "async", "aspect_ratio": "16:9", "quality": "low"},
        [{"channel_id": "primary", "endpoint": "https://example.test/v1/", "api_keys": ["a"]}],
    )

    assert qualities == ["low", "auto", "high"]
    assert result["task_id"] == "task-high"
    assert result["quality"] == "high"
    assert result["quality_attempts"] == ["low", "auto", "high"]


def test_image2_create_does_not_escalate_uncertain_post(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import run_token_assets_live as assets

    qualities: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def create_image(self, **kwargs):
            qualities.append(str(kwargs["quality"]))
            raise Image2RequestUncertainError("连接中断", task_id="task-uncertain")

    monkeypatch.setattr(assets, "Image2AllHTTPClient", FakeClient)

    result = assets._image2_create_with_failover(
        {"prompt": "测试", "image2_mode": "async", "aspect_ratio": "16:9", "quality": "low"},
        [{"channel_id": "primary", "endpoint": "https://example.test/v1/", "api_keys": ["a"]}],
    )

    assert qualities == ["low"]
    assert result["task_id"] == "task-uncertain"
    assert result["quality_attempts"] == ["low"]


def test_image2_create_does_not_escalate_terminal_task_id_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import run_token_assets_live as assets

    qualities: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def create_image(self, **kwargs):
            qualities.append(str(kwargs["quality"]))
            return {"data": {"taskId": "task-failed", "state": "failed"}}

    monkeypatch.setattr(assets, "Image2AllHTTPClient", FakeClient)

    with pytest.raises(assets.Image2ResponseError, match="task-failed"):
        assets._image2_create_with_failover(
            {"prompt": "测试", "image2_mode": "async", "aspect_ratio": "16:9", "quality": "low"},
            [{"channel_id": "primary", "endpoint": "https://example.test/v1/", "api_keys": ["a"]}],
        )

    assert qualities == ["low"]


def test_image2_quality_escalation_preserves_auto_high_and_medium_modes() -> None:
    from tools import run_token_assets_live as assets

    assert assets._image2_quality_escalation("low") == ("low", "auto", "high")
    assert assets._image2_quality_escalation("auto") == ("auto", "high")
    assert assets._image2_quality_escalation("high") == ("high",)
    assert assets._image2_quality_escalation("medium") == ("medium",)


def test_image2_uncertain_error_is_not_silently_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import run_token_assets_live as assets

    calls: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append("construct")

        def create_image(self, **kwargs):
            calls.append("create")
            raise Image2RequestUncertainError("断开", task_id="")

    monkeypatch.setattr(assets, "Image2AllHTTPClient", FakeClient)

    with pytest.raises(RuntimeError, match="状态不确定"):
        assets._image2_create_with_failover(
            {"prompt": "测试", "image2_mode": "async", "aspect_ratio": "1:1"},
            [
                {"channel_id": "primary", "endpoint": "https://example.test/v1/", "api_keys": ["a"]},
                {"channel_id": "backup", "endpoint": "https://example.test/v1/", "api_keys": ["b"]},
            ],
        )

    assert calls == ["construct", "create"]


def test_image2_async_failover_returns_task_and_query_never_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import run_token_assets_live as assets

    calls: list[tuple[str, object]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append(("construct", kwargs))

        def create_image(self, **kwargs):
            calls.append(("create", kwargs))
            return {"code": 200, "data": {"taskId": "task-async", "state": "queued"}}

        def get_task(self, task_id):
            calls.append(("get", task_id))
            return {"data": {"taskId": task_id, "state": "completed", "resultUrls": ["https://img.test/grid.jpg"]}}

    monkeypatch.setattr(assets, "Image2AllHTTPClient", FakeClient)

    created = assets._image2_create_with_failover(
        {
            "prompt": "测试",
            "image2_mode": "async",
            "aspect_ratio": "16:9",
            "resolution": "1K",
        },
        [{"channel_id": "primary", "endpoint": "https://example.test/v1/", "api_keys": ["a"]}],
    )
    assert created["task_id"] == "task-async"
    assert created["image_task_mode"] == "asynchronous"
    create_payload = next(value for name, value in calls if name == "create")
    assert create_payload["size"] == ""

    queried = assets._image2_query_with_failover(
        {"task_id": "task-async", "channel_id": "primary"},
        [{"channel_id": "primary", "endpoint": "https://example.test/v1/", "api_keys": ["a"]}],
        deadline_seconds=1,
    )
    assert queried["image_urls"] == ["https://img.test/grid.jpg"]
    assert [name for name, _ in calls].count("create") == 1
    assert [name for name, _ in calls].count("get") == 1


def test_image2_async_query_polls_same_task_every_ten_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import run_token_assets_live as assets

    task_ids: list[str] = []
    sleeps: list[float] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_task(self, task_id):
            task_ids.append(task_id)
            if len(task_ids) == 1:
                return {"data": {"taskId": task_id, "state": "processing"}}
            return {
                "data": {
                    "taskId": task_id,
                    "state": "completed",
                    "resultUrls": ["https://img.test/final-grid.jpg"],
                }
            }

    monkeypatch.setattr(assets, "Image2AllHTTPClient", FakeClient)
    monkeypatch.setattr(assets.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = assets._image2_query_with_failover(
        {"task_id": "provider-grid-1"},
        [{"channel_id": "primary", "endpoint": "https://example.test/v1/", "api_keys": ["a"]}],
        deadline_seconds=60,
    )

    assert task_ids == ["provider-grid-1", "provider-grid-1"]
    assert sleeps == [10.0]
    assert result["image_urls"] == ["https://img.test/final-grid.jpg"]


def test_grid_async_checkpoint_is_reused_without_paid_create(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from tools import run_token_assets_live as assets

    image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    image.new("RGB", (200, 200), (30, 40, 50)).save(buffer, format="JPEG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    create_calls: list[object] = []
    query_calls: list[object] = []

    monkeypatch.setattr(
        assets,
        "_image2_create_with_failover",
        lambda request, _targets: create_calls.append(dict(request)) or {
            "task_id": "provider-grid-1",
            "state": "queued",
            "channel_id": "primary",
            "key_index": 1,
            "endpoint": "https://example.test/v1/",
        },
    )
    monkeypatch.setattr(
        assets,
        "_image2_query_with_failover",
        lambda request, _targets: query_calls.append(dict(request)) or {
            "task_id": "provider-grid-1",
            "state": "completed",
            "image_b64": [encoded],
            "image_urls": [],
            "channel_id": "primary",
        },
    )
    monkeypatch.setattr(
        assets,
        "publish_existing_grid_images",
        lambda paths, *_args, **_kwargs: [f"https://published.test/{i}" for i, _ in enumerate(paths)],
    )
    manifest = {"aspect_ratio": "1:1", "shots": [{"shot_id": "s1", "first_frame_prompt": "画面"}]}
    checkpoint = tmp_path / "first_frame_checkpoint.json"
    targets = [{"channel_id": "primary", "api_keys": ["key"]}]

    first = assets._generate_images_as_grids_sync(
        manifest, tmp_path / "auth.md", tmp_path / "out", targets,
        grid_layout="2x2", checkpoint_path=checkpoint,
    )
    assert first["image_task_mode"] == "asynchronous"
    assert create_calls and create_calls[0]["image2_mode"] == "async"
    assert query_calls and query_calls[0]["task_id"] == "provider-grid-1"
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["batches"]["first_frame_grid_001"]["status"] == "completed"

    monkeypatch.setattr(
        assets,
        "_image2_create_with_failover",
        lambda *_args: (_ for _ in ()).throw(AssertionError("恢复时不应重新创建付费任务")),
    )
    second = assets._generate_images_as_grids_sync(
        manifest, tmp_path / "auth.md", tmp_path / "out", targets,
        grid_layout="2x2", checkpoint_path=checkpoint, resume=True,
    )
    assert second["image_paths"] == first["image_paths"]


def test_grid_resume_does_not_create_when_checkpoint_lacks_handle(tmp_path) -> None:
    from workflow_1256.first_frame_grid import FirstFrameGridBatchError, run_first_frame_grid_batch

    image = pytest.importorskip("PIL.Image")
    grid_path = tmp_path / "grid-1.png"
    image.new("RGB", (720, 1280), (60, 70, 80)).save(grid_path)
    create_calls: list[dict[str, object]] = []
    query_calls: list[dict[str, object]] = []

    def create_runner(request):
        create_calls.append(dict(request))
        raise AssertionError("恢复模式不应为缺少句柄的宫格创建新任务")

    def query_runner(request):
        query_calls.append(dict(request))
        return {"status": "completed", "image_url": "https://img.test/grid-1.png"}

    def materialize_runner(image_url, _batch, _output_dir):
        assert image_url == "https://img.test/grid-1.png"
        return grid_path

    checkpoint = tmp_path / "first_frame_checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-08-29T00:00:00Z",
                "batches": {
                    "first_frame_grid_001": {
                        "grid_id": "first_frame_grid_001",
                        "status": "submitted",
                        "task_id": "provider-grid-1",
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with pytest.raises(FirstFrameGridBatchError, match="恢复断点缺少 task_id/image_url/local_grid_path"):
        run_first_frame_grid_batch(
            [{"shot_id": "s1", "prompt": "一"}, {"shot_id": "s2", "prompt": "二"}],
            create_runner=create_runner,
            query_runner=query_runner,
            materialize_runner=materialize_runner,
            output_dir=tmp_path / "out",
            cells_per_grid=1,
            grid_layout=None,
            checkpoint_path=checkpoint,
            resume=True,
        )

    assert len(query_calls) == 1
    assert not create_calls
