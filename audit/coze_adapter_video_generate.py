"""video_generate 脱敏原始插件与新实现的离线运行适配器。"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workflow_1256.video_generate import run_video_generate


SOURCE_PATH = Path(__file__).parent / "source" / "video_generate_attachment_sanitized.py"


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200, content: bytes = b"") -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = content
        self.text = str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeRequests:
    class exceptions:
        class Timeout(Exception):
            pass

        class ConnectionError(Exception):
            pass

    def __init__(self, events: list[Any]) -> None:
        self.events = list(events)
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def _next(self) -> Any:
        if not self.events:
            raise RuntimeError("synthetic requests event exhausted")
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    def post(self, url: str, **kwargs: Any) -> Any:
        self.post_calls.append({"url": url, **kwargs})
        return self._next()

    def get(self, url: str, **kwargs: Any) -> Any:
        self.get_calls.append({"url": url, **kwargs})
        return self._next()


def _load_original_namespace() -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": "coze_video_generate_original"}
    source = SOURCE_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(SOURCE_PATH), "exec"), namespace)
    return namespace


def run_original(
    params: Any,
    events: list[Any] | None = None,
    sleeps: list[float] | None = None,
) -> tuple[dict[str, Any], FakeRequests]:
    namespace = _load_original_namespace()
    client = FakeRequests(events or [])
    sleep_log = sleeps if sleeps is not None else []
    original_time = namespace["time"]
    namespace["requests"] = client
    namespace["time"] = SimpleNamespace(sleep=sleep_log.append, time=original_time.time)
    try:
        result = namespace["handler"](SimpleNamespace(input=copy.deepcopy(params)))
    finally:
        namespace["time"] = original_time
    return result, client


def run_migrated(
    params: Any,
    events: list[Any] | None = None,
    sleeps: list[float] | None = None,
) -> tuple[dict[str, Any], FakeRequests]:
    client = FakeRequests(events or [])
    sleep_log = sleeps if sleeps is not None else []
    result = run_video_generate(
        copy.deepcopy(params),
        requests_client=client,
        sleep=sleep_log.append,
    )
    return result, client


def run_both(
    params: Any,
    events: list[Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], FakeRequests, FakeRequests]:
    original, original_client = run_original(params, events)
    migrated, migrated_client = run_migrated(params, events)
    return original, migrated, original_client, migrated_client
