"""“create_image_task”插件的离线等价适配层。

原插件负责提交异步生图任务。本模块保留原代码的参数清洗、请求负载、
重试条件和返回字段；网络传输必须由调用方显式注入，默认不会访问外部服务。
"""

from __future__ import annotations

import json
import time
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .material_models import IMAGE_MODEL_API_IDS, normalize_image_model


CREATE_URL = "https://api.aishuch.com/v1/images/generations"
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 2.0


class CreateImageTaskTransportRequired(RuntimeError):
    """没有显式传输层时阻止真实网络调用。"""


@dataclass(frozen=True)
class CreateImageTaskRequest:
    """原插件一次 POST 请求的可审计表示。"""

    api_key: str
    prompt: str
    n: int
    size: str
    resolution: str
    image_urls: list[Any]
    official_fallback: bool
    image_model: str = "image2"


class CreateImageTaskTransport(Protocol):
    def __call__(self, request: CreateImageTaskRequest) -> Mapping[str, Any]: ...


def _resolve_params(value: Any) -> Mapping[str, Any]:
    # 与原代码对 args.input 的三路判断保持一致。
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return value.__dict__
    try:
        return dict(value)
    except Exception:
        return {}


def _text(value: Any) -> str:
    return str(value).strip()


def _is_digit_string(value: Any) -> bool:
    return str(value).isdigit()


def build_request(params: Any) -> CreateImageTaskRequest | dict[str, Any]:
    """按原插件规则清洗输入；缺失必填字段时返回原始错误对象。"""

    resolved = _resolve_params(params)

    raw_api_key = _text(resolved.get("api_key", ""))
    if not raw_api_key or raw_api_key == "None":
        return {"error": True, "message": "缺失必填参数: api_key"}
    api_key = raw_api_key if raw_api_key.startswith("Bearer ") else f"Bearer {raw_api_key}"

    prompt = _text(resolved.get("prompt", ""))
    if not prompt or prompt == "None":
        return {"error": True, "message": "提示词不能为空"}

    raw_n = resolved.get("n", 1)
    n = int(raw_n) if raw_n is not None and _is_digit_string(raw_n) else 1
    size = resolved.get("size", "auto") or "auto"
    resolution = resolved.get("resolution", "1k") or "1k"

    raw_urls = resolved.get("image_urls", [])
    image_urls = raw_urls if isinstance(raw_urls, list) else []
    official_fallback = bool(resolved.get("official_fallback", False))
    try:
        image_model = normalize_image_model(resolved.get("image_model", "auto"))
    except ValueError as exc:
        return {"error": True, "message": str(exc)}

    return CreateImageTaskRequest(
        api_key=api_key,
        prompt=prompt,
        n=n,
        size=str(size),
        resolution=str(resolution),
        image_urls=list(image_urls),
        official_fallback=official_fallback,
        image_model=image_model,
    )


def _request_payload(request: CreateImageTaskRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": IMAGE_MODEL_API_IDS[request.image_model],
        "prompt": request.prompt,
        "n": request.n,
        "size": request.size,
        "resolution": request.resolution,
        "official_fallback": request.official_fallback,
    }
    if len(request.image_urls) > 0:
        payload["image_urls"] = list(request.image_urls)
    return payload


def _transport_call(
    transport: CreateImageTaskTransport | Callable[[CreateImageTaskRequest], Mapping[str, Any]],
    request: CreateImageTaskRequest,
) -> Mapping[str, Any]:
    return transport(request)


def run_create_image_task(
    params: Any,
    *,
    transport: CreateImageTaskTransport
    | Callable[[CreateImageTaskRequest], Mapping[str, Any]]
    | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """离线执行原插件逻辑；``transport`` 不提供时绝不发起网络请求。"""

    request_or_error = build_request(params)
    if isinstance(request_or_error, dict):
        return request_or_error
    if transport is None:
        raise CreateImageTaskTransportRequired(
            "未配置 create_image_task 传输层；当前只允许离线契约测试"
        )

    request = request_or_error
    retry_delay = INITIAL_RETRY_DELAY
    for attempt in range(MAX_RETRIES):
        try:
            raw_result = _transport_call(transport, request)
            data_array = raw_result.get("data", [])

            if data_array and isinstance(data_array, list) and len(data_array) > 0:
                task_id = data_array[0].get("task_id")
                if task_id:
                    return {
                        "code": 202,
                        "status": "processing",
                        "task_id": task_id,
                        "attempted": attempt + 1,
                        "message": "任务创建成功！已转交下游处理",
                    }

            if attempt < MAX_RETRIES - 1:
                sleep(retry_delay)
                continue
            return {
                "error": True,
                "message": "接口调用成功，但未返回有效的 task_id，可能 API 结构已变更。",
            }

        except urllib.error.HTTPError as error:
            if error.code in [400, 401, 403, 404]:
                return {
                    "error": True,
                    "status_code": error.code,
                    "message": (
                        "客户端致命错误，请检查参数或 API_KEY: "
                        + error.read().decode("utf-8")[:200]
                    ),
                }
            if attempt < MAX_RETRIES - 1:
                sleep(retry_delay)
                retry_delay *= 1.5
                continue
            return {
                "error": True,
                "status_code": error.code,
                "message": "远端服务器持续不可用或超载，创建任务失败。",
            }
        except Exception as error:
            if attempt < MAX_RETRIES - 1:
                sleep(retry_delay)
                retry_delay *= 1.5
                continue
            return {
                "error": True,
                "message": f"网络请求失败，已重试 {MAX_RETRIES} 次: {error}",
            }

    return {"error": True, "message": "任务创建最终失败。"}


def make_payload_for_audit(params: Any) -> dict[str, Any] | None:
    """返回脱离鉴权后的请求体，供新旧请求形状对照测试。"""

    request_or_error = build_request(params)
    if isinstance(request_or_error, dict):
        return None
    return _request_payload(request_or_error)


def to_coze_node_output(raw_result: Mapping[str, Any]) -> dict[str, Any]:
    """按 YAML 声明的 5 个输出字段投影插件原始返回值。

    Coze 页面会把源码未返回的 ``image_url`` 显示为 null，并忽略源码中
    未声明的 ``attempted``；该投影不参与原代码分支逻辑。
    """

    return {
        "code": raw_result.get("code"),
        "image_url": raw_result.get("image_url"),
        "message": raw_result.get("message"),
        "status": raw_result.get("status"),
        "task_id": raw_result.get("task_id"),
    }


async def main(args: Any, *, transport: CreateImageTaskTransport | None = None) -> dict[str, Any]:
    value = getattr(args, "input", args)
    return run_create_image_task(value, transport=transport)
