"""video_query 脱敏原始插件与新实现的离线运行适配器。"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workflow_1256.video_query import run_video_query


SOURCE_PATH = Path(__file__).parent / "source" / "video_query_attachment_sanitized.py"


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
    def __init__(self, events: list[Any]) -> None:
        self.events = list(events)
        self.get_calls: list[dict[str, Any]] = []

    def _next(self) -> Any:
        if not self.events:
            raise RuntimeError("synthetic requests event exhausted")
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    def get(self, url: str, **kwargs: Any) -> Any:
        self.get_calls.append({"url": url, **kwargs})
        return self._next()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _load_original_namespace() -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": "coze_video_query_original"}
    source = SOURCE_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(SOURCE_PATH), "exec"), namespace)
    return namespace


def run_original(
    params: Any,
    events: list[Any] | None = None,
) -> tuple[dict[str, Any], FakeRequests, FakeClock]:
    namespace = _load_original_namespace()
    client = FakeRequests(events or [])
    clock = FakeClock()
    original_time = namespace["time"]
    namespace["requests"] = client
    namespace["time"] = SimpleNamespace(sleep=clock.sleep, time=clock.time)
    try:
        result = namespace["handler"](SimpleNamespace(input=copy.deepcopy(params)))
    finally:
        namespace["time"] = original_time
    return result, client, clock


def run_migrated(
    params: Any,
    events: list[Any] | None = None,
) -> tuple[dict[str, Any], FakeRequests, FakeClock]:
    client = FakeRequests(events or [])
    clock = FakeClock()
    result = run_video_query(
        copy.deepcopy(params),
        requests_client=client,
        clock=clock,
        sleep=clock.sleep,
    )
    return result, client, clock


def run_both(
    params: Any,
    events: list[Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], FakeRequests, FakeRequests, FakeClock, FakeClock]:
    original, original_client, original_clock = run_original(params, events)
    migrated, migrated_client, migrated_clock = run_migrated(params, events)
    return original, migrated, original_client, migrated_client, original_clock, migrated_clock
