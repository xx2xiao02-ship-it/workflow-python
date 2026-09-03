"""8364 工作流 ``save_draft`` 节点契约。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class SaveDraftValidationError(ValueError):
    """保存草稿输入或响应不符合节点契约。"""


class SaveDraftTransportRequired(RuntimeError):
    """未注入 CapCut Mate 保存 transport。"""


@dataclass(frozen=True)
class SaveDraftRequest:
    draft_url: str


class SaveDraftTransport(Protocol):
    def __call__(self, request: SaveDraftRequest) -> Mapping[str, Any]: ...


def build_request(params: Any) -> SaveDraftRequest:
    value = params
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SaveDraftValidationError("save_draft 输入 JSON 无效") from exc
    if isinstance(value, Mapping) and isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value, Mapping) and isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    if not isinstance(value, Mapping) or not isinstance(value.get("draft_url"), str):
        raise SaveDraftValidationError("save_draft.draft_url 必须是字符串")
    if not value["draft_url"].strip():
        raise SaveDraftValidationError("save_draft.draft_url 不能为空")
    return SaveDraftRequest(draft_url=value["draft_url"])


def normalize_response(response: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(response, Mapping):
        raise SaveDraftValidationError("save_draft 响应必须是对象")
    draft_url = response.get("draft_url")
    message = response.get("message", "")
    if not isinstance(draft_url, str) or not draft_url:
        raise SaveDraftValidationError("save_draft 响应缺少有效 draft_url")
    if not isinstance(message, str):
        raise SaveDraftValidationError("save_draft.message 必须是字符串")
    return {"draft_url": draft_url, "message": message}


def run_save_draft(
    params: Any,
    *,
    transport: SaveDraftTransport | Callable[[SaveDraftRequest], Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    request = build_request(params)
    if transport is None:
        raise SaveDraftTransportRequired("未配置 CapCut Mate 保存 transport")
    return normalize_response(transport(request))


__all__ = [
    "SaveDraftRequest",
    "SaveDraftTransportRequired",
    "SaveDraftValidationError",
    "build_request",
    "normalize_response",
    "run_save_draft",
]
