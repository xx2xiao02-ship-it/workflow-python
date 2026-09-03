"""创建草稿节点的本地 Python 适配层。

原节点调用外部 CapCut Mate 服务并产生副作用：复制模板、写入草稿文件、
创建空的视频主轨道并更新服务端缓存。因此本模块不在本地偷偷调用网络，
而是把外部传输作为显式依赖注入。这样可以先验证 Coze 节点的输入/输出契约，
待拿到真实请求和响应后再做行为等价审计。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class CreateDraftValidationError(ValueError):
    """输入不符合插件请求模型时抛出。"""


class CreateDraftTransportRequired(RuntimeError):
    """未注入外部服务传输层时抛出，避免误以为已经创建了真实草稿。"""


class CreateDraftTransport(Protocol):
    def __call__(self, request: "CreateDraftRequest") -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class CreateDraftRequest:
    """与 CapCut Mate ``CreateDraftRequest`` 对齐的请求。"""

    height: int = 1080
    width: int = 1920


_MISSING = object()


def _parse_json_object(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CreateDraftValidationError("输入 JSON 字符串无效") from exc


def _resolve_params(params: Any) -> Mapping[str, Any]:
    value = _parse_json_object(params)

    if value is None:
        return {}

    if not isinstance(value, Mapping):
        raise CreateDraftValidationError("创建草稿输入必须是对象")

    # 适配本项目测试和 Coze 代码节点常见的参数包装；插件 HTTP body
    # 本身仍然使用最外层的 height/width 字段。
    if "params" in value and isinstance(value["params"], Mapping):
        value = value["params"]
    if "_input" in value and isinstance(value["_input"], Mapping):
        value = value["_input"]

    return value


def _positive_int(name: str, value: Any, default: int) -> int:
    if value is _MISSING:
        return default

    # Pydantic 的非严格整数模型允许常见整数数字字符串；布尔值不是有效
    # 的画布尺寸，显式排除以避免 Python bool 继承 int 带来的误判。
    if isinstance(value, bool):
        raise CreateDraftValidationError(f"{name} 必须是大于等于 1 的整数")

    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CreateDraftValidationError(f"{name} 必须是大于等于 1 的整数") from None

    if not number.is_integer() or number < 1:
        raise CreateDraftValidationError(f"{name} 必须是大于等于 1 的整数")

    return int(number)


def build_request(params: Any = None) -> CreateDraftRequest:
    """把工作流输入转换为插件请求模型。"""

    resolved = _resolve_params(params)
    return CreateDraftRequest(
        height=_positive_int("height", resolved.get("height", _MISSING), 1080),
        width=_positive_int("width", resolved.get("width", _MISSING), 1920),
    )


def normalize_response(response: Mapping[str, Any]) -> dict[str, str]:
    """保留路由层声明的两个输出字段和固定顺序。"""

    if not isinstance(response, Mapping):
        raise CreateDraftValidationError("创建草稿响应必须是对象")

    draft_url = response.get("draft_url", "")
    tip_url = response.get("tip_url", "")

    if not isinstance(draft_url, str) or not isinstance(tip_url, str):
        raise CreateDraftValidationError("draft_url 和 tip_url 必须是字符串")

    return {
        "draft_url": draft_url,
        "tip_url": tip_url,
    }


def run_create_draft(
    params: Any = None,
    *,
    transport: CreateDraftTransport | Callable[[CreateDraftRequest], Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    """执行创建草稿适配。

    ``transport`` 必须由调用方显式提供。未提供时不会发起网络请求，也不会
    伪造真实草稿 URL。
    """

    request = build_request(params)
    if transport is None:
        raise CreateDraftTransportRequired(
            "未配置 CapCut Mate 传输层；当前只能完成契约测试，不能创建真实草稿"
        )

    return normalize_response(transport(request))


async def main(
    args: Any,
    *,
    transport: CreateDraftTransport | Callable[[CreateDraftRequest], Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    """提供与 Coze 节点相同的异步入口形态。"""

    if isinstance(args, Mapping):
        params = args.get("params", args)
    else:
        params = getattr(args, "params", None)

    return run_create_draft(params, transport=transport)
