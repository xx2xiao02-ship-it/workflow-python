"""8364 结束节点 900001 的最终内容渲染。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class WorkflowEndValidationError(ValueError):
    """结束节点没有收到 save_draft 的有效 draft_url。"""


def run_workflow_end(params: Any) -> dict[str, str]:
    value = params
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise WorkflowEndValidationError("结束节点输入 JSON 无效") from exc
    if not isinstance(value, Mapping):
        raise WorkflowEndValidationError("结束节点输入必须是对象")
    for key in ("params", "_input"):
        if isinstance(value.get(key), Mapping):
            value = value[key]
    draft_url = value.get("draft_url")
    if not isinstance(draft_url, str) or not draft_url.strip():
        raise WorkflowEndValidationError("draft_url 必须来自 143635 save_draft 且不能为空")
    return {"content": draft_url.strip()}


__all__ = ["WorkflowEndValidationError", "run_workflow_end"]
