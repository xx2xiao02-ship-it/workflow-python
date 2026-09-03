"""“get_task_result”插件的离线等价适配层。"""

from __future__ import annotations

import time
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


TASK_URL_PREFIX = "https://api.aishuch.com/v1/tasks/"
ABSOLUTE_DEADLINE = 172.0
POLL_INTERVAL = 30.0


class ActiveCrashError(Exception):
    """原插件主动触发的熔断异常。"""


class GetTaskResultTransportRequired(RuntimeError):
    """没有显式传输层时阻止真实网络调用。"""


@dataclass(frozen=True)
class GetTaskResultRequest:
    task_id: str
    api_key: str


class GetTaskResultTransport(Protocol):
    def __call__(self, request: GetTaskResultRequest) -> Mapping[str, Any]: ...


class Clock(Protocol):
    def time(self) -> float: ...


def _get_input_value(value: Any, key: str, default: Any = "") -> Any:
    if hasattr(value, key):
        return getattr(value, key)
    if hasattr(value, "get"):
        return value.get(key, default)
    return default


def build_request(params: Any) -> GetTaskResultRequest | dict[str, Any]:
    task_id = _get_input_value(params, "task_id", "")
    api_key = _get_input_value(params, "api_key", "")
    task_id = str(task_id).strip().strip("'").strip('"')
    api_key = str(api_key).strip().strip("'").strip('"')

    if not task_id:
        return {"error": True, "message": "缺失 task_id"}
    if not api_key:
        return {"error": True, "message": "缺失 api_key"}
    if not api_key.startswith("Bearer "):
        api_key = f"Bearer {api_key}"
    return GetTaskResultRequest(task_id=task_id, api_key=api_key)


class _SystemClock:
    @staticmethod
    def time() -> float:
        return time.time()


def run_get_task_result(
    params: Any,
    *,
    transport: GetTaskResultTransport
    | Callable[[GetTaskResultRequest], Mapping[str, Any]]
    | None = None,
    clock: Clock | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    request_or_error = build_request(params)
    if isinstance(request_or_error, dict):
        return request_or_error
    if transport is None:
        raise GetTaskResultTransportRequired(
            "未配置 get_task_result 传输层；当前只允许离线契约测试"
        )

    clock = clock or _SystemClock()
    request = request_or_error
    start_time = clock.time()
    attempt = 0

    while True:
        try:
            raw_result = transport(request)
            data_obj = raw_result.get("data", {})
            status = data_obj.get("status", "unknown")

            if status == "completed":
                try:
                    image_url = data_obj["result"]["images"][0]["url"][0]
                except Exception:
                    image_url = ""
                elapsed_time = clock.time() - start_time
                return {
                    "code": 200,
                    "status": "completed",
                    "image_url": image_url,
                    "msg": f"成功！共查询 {attempt + 1} 次，总耗时 {elapsed_time:.1f} 秒",
                }

            if status == "failed":
                error_msg = data_obj.get("error", {}).get("message", "未知错误")
                return {
                    "code": 500,
                    "status": "failed",
                    "message": f"绘图失败：{error_msg}",
                }

            current_elapsed = clock.time() - start_time
            if current_elapsed >= ABSOLUTE_DEADLINE:
                raise ActiveCrashError(
                    f"【极限熔断】已给足 {current_elapsed:.1f} 秒，最后一次查询仍未完成，立刻重启节点！"
                )

            remaining_time = ABSOLUTE_DEADLINE - current_elapsed
            sleep_time = min(POLL_INTERVAL, remaining_time)
            sleep(sleep_time)
            attempt += 1

        except ActiveCrashError:
            raise
        except urllib.error.HTTPError as error:
            return {
                "error": True,
                "status_code": error.code,
                "raw_response": error.read().decode("utf-8")[:300],
            }
        except Exception as error:
            sleep(2)
            if clock.time() - start_time >= ABSOLUTE_DEADLINE:
                raise ActiveCrashError("【极限熔断】网络抖动重试期间超时，立刻重启节点！")


def handler(args: Any, *, transport: GetTaskResultTransport | None = None) -> dict[str, Any]:
    value = getattr(args, "input", args)
    return run_get_task_result(value, transport=transport)


def to_coze_node_output(raw_result: Mapping[str, Any]) -> dict[str, Any]:
    """按 YAML 声明的 6 个输出字段投影插件原始返回值。

    页面真实成功记录显示：源码的 ``msg`` 未映射到声明字段 ``message``，
    而平台补充展示 ``errorBody`` 和 ``isSuccess``。这里保留这一层显式，
    不把平台包装逻辑混入原插件算法。
    """

    status = raw_result.get("status")
    return {
        "code": raw_result.get("code"),
        "errorBody": raw_result.get("errorBody"),
        "image_url": raw_result.get("image_url"),
        "isSuccess": status == "completed",
        "message": raw_result.get("message"),
        "status": status,
    }
