"""8364 节点 118959 ``time_sleep`` 的本地等价实现。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


class TimeSleepValidationError(ValueError):
    """休眠秒数不符合 YAML 整数契约。"""


@dataclass(frozen=True)
class TimeSleepRequest:
    seconds: int


def _resolve(params: Any) -> Mapping[str, Any]:
    value = params
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TimeSleepValidationError("time_sleep 输入 JSON 无效") from exc
    if not isinstance(value, Mapping):
        raise TimeSleepValidationError("time_sleep 输入必须是对象")
    for key in ("params", "_input"):
        if isinstance(value.get(key), Mapping):
            value = value[key]
    return value


def build_request(params: Any) -> TimeSleepRequest:
    value = _resolve(params)
    seconds = value.get("seconds")
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise TimeSleepValidationError("seconds 必须是整数秒")
    if seconds < 0:
        raise TimeSleepValidationError("seconds 不能为负数")
    return TimeSleepRequest(seconds)


def run_time_sleep(
    params: Any,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    request = build_request(params)
    sleeper(request.seconds)
    return {"seconds": request.seconds}


__all__ = [
    "TimeSleepRequest",
    "TimeSleepValidationError",
    "build_request",
    "run_time_sleep",
]
