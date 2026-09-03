"""两个 Image-2 插件的离线旧代码运行适配器。

适配器只把用户源码的 urllib/time 外部依赖替换为内存脚本，保留源码本身的
分支、重试、轮询和异常行为。脚本不读取或保存真实鉴权值。
"""

from __future__ import annotations

import copy
import io
import json
import sys
import time
import types
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from workflow_1256.create_image_task import (
    CreateImageTaskRequest,
    run_create_image_task,
)
from workflow_1256.get_task_result import (
    GetTaskResultRequest,
    run_get_task_result,
)


ROOT = Path(__file__).parent / "source"


@dataclass
class ExecutionOutcome:
    value: Any = None
    exception_type: str | None = None
    exception_message: str | None = None

    @property
    def raised(self) -> bool:
        return self.exception_type is not None


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedHTTPResponse:
    def __init__(self, body: bytes | str):
        self.body = body.encode("utf-8") if isinstance(body, str) else body

    def __enter__(self) -> "ScriptedHTTPResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class ScriptedUrlOpen:
    def __init__(self, events: list[Any]):
        self.events = list(events)
        self.calls: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float = 0) -> ScriptedHTTPResponse:
        self.calls.append(request)
        if not self.events:
            raise RuntimeError("scripted urlopen 没有更多事件")
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        if isinstance(event, bytes):
            return ScriptedHTTPResponse(event)
        if isinstance(event, str):
            return ScriptedHTTPResponse(event)
        return ScriptedHTTPResponse(json.dumps(event, ensure_ascii=False))


class ScriptedTransport:
    def __init__(self, events: list[Any]):
        self.events = list(events)
        self.calls: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        if not self.events:
            raise RuntimeError("scripted transport 没有更多事件")
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event


def make_http_error(code: int, body: str = "synthetic error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://synthetic.invalid/task",
        code=code,
        msg=f"synthetic HTTP {code}",
        hdrs=None,
        fp=io.BytesIO(body.encode("utf-8")),
    )


def _clone_event(event: Any) -> Any:
    """复制脚本事件，但为 HTTPError 重新创建可读取的响应体。"""

    if isinstance(event, urllib.error.HTTPError):
        body = b""
        if event.fp is not None and hasattr(event.fp, "getvalue"):
            body = event.fp.getvalue()
        return urllib.error.HTTPError(
            url=event.url,
            code=event.code,
            msg=event.msg,
            hdrs=event.hdrs,
            fp=io.BytesIO(body),
        )
    if isinstance(event, json.JSONDecodeError):
        return json.JSONDecodeError(event.msg, event.doc, event.pos)
    if isinstance(event, BaseException):
        return type(event)(str(event))
    return copy.deepcopy(event)


def _clone_events(events: list[Any]) -> list[Any]:
    return [_clone_event(event) for event in events]


@contextmanager
def _patched_runtime(
    *,
    urlopen: Callable[..., Any],
    clock: FakeClock | None = None,
):
    old_urlopen = urllib.request.urlopen
    old_sleep = time.sleep
    old_time = time.time
    old_runtime = sys.modules.get("runtime")
    runtime_module = types.ModuleType("runtime")
    runtime_module.Args = Any
    sys.modules["runtime"] = runtime_module
    urllib.request.urlopen = urlopen  # type: ignore[assignment]
    if clock is not None:
        time.time = clock.time  # type: ignore[assignment]
        time.sleep = clock.sleep  # type: ignore[assignment]
    try:
        yield
    finally:
        urllib.request.urlopen = old_urlopen  # type: ignore[assignment]
        time.sleep = old_sleep  # type: ignore[assignment]
        time.time = old_time  # type: ignore[assignment]
        if old_runtime is None:
            sys.modules.pop("runtime", None)
        else:
            sys.modules["runtime"] = old_runtime


def _capture(call: Callable[[], Any]) -> ExecutionOutcome:
    try:
        return ExecutionOutcome(value=call())
    except Exception as error:
        return ExecutionOutcome(
            exception_type=type(error).__name__,
            exception_message=str(error),
        )


def _load_handler(path: Path) -> Callable[[Any], dict[str, Any]]:
    namespace: dict[str, Any] = {"__name__": f"coze_original_{path.stem}"}
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), namespace)
    return namespace["handler"]


def run_original_create(
    params: Any,
    events: list[Any],
    *,
    clock: FakeClock | None = None,
) -> tuple[ExecutionOutcome, ScriptedUrlOpen]:
    opener = ScriptedUrlOpen(_clone_events(events))
    with _patched_runtime(urlopen=opener, clock=clock):
        handler = _load_handler(ROOT / "142186_coze_original_sanitized.py")
        outcome = _capture(lambda: handler(SimpleNamespace(input=copy.deepcopy(params))))
    return outcome, opener


def run_migrated_create(
    params: Any,
    events: list[Any],
    *,
    sleeps: list[float] | None = None,
) -> tuple[ExecutionOutcome, ScriptedTransport]:
    transport = ScriptedTransport(_clone_events(events))
    sleep_log = sleeps if sleeps is not None else []

    def sleep(seconds: float) -> None:
        sleep_log.append(seconds)

    outcome = _capture(
        lambda: run_create_image_task(
            copy.deepcopy(params), transport=transport, sleep=sleep
        )
    )
    return outcome, transport


def run_original_poll(
    params: Any,
    events: list[Any],
    *,
    clock: FakeClock | None = None,
) -> tuple[ExecutionOutcome, ScriptedUrlOpen, FakeClock]:
    poll_clock = clock or FakeClock()
    opener = ScriptedUrlOpen(_clone_events(events))
    with _patched_runtime(urlopen=opener, clock=poll_clock):
        handler = _load_handler(ROOT / "106538_coze_original_sanitized.py")
        outcome = _capture(lambda: handler(SimpleNamespace(input=copy.deepcopy(params))))
    return outcome, opener, poll_clock


def run_migrated_poll(
    params: Any,
    events: list[Any],
    *,
    clock: FakeClock | None = None,
) -> tuple[ExecutionOutcome, ScriptedTransport, FakeClock]:
    poll_clock = clock or FakeClock()
    transport = ScriptedTransport(_clone_events(events))
    outcome = _capture(
        lambda: run_get_task_result(
            copy.deepcopy(params),
            transport=transport,
            clock=poll_clock,
            sleep=poll_clock.sleep,
        )
    )
    return outcome, transport, poll_clock
