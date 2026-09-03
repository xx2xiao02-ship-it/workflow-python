"""节点 1962357 的逐项旧新解析对照。"""

from __future__ import annotations

from typing import Any

from .coze_adapter_add_keyframes_second import run_migrated, run_original


def _capture(fn: Any, value: Any) -> dict[str, Any]:
    try:
        return {"kind": "return", "value": fn(value)}
    except Exception as exc:  # noqa: BLE001
        return {"kind": "exception", "type": f"{type(exc).__module__}.{type(exc).__qualname__}", "message": str(exc), "detail": getattr(exc, "detail", None)}


def compare_case(params: Any) -> list[dict[str, Any]]:
    original = _capture(run_original, params)
    migrated = _capture(run_migrated, params)
    if original == migrated:
        return []
    if original.get("kind") == migrated.get("kind") == "exception":
        if (original.get("message"), original.get("detail")) == (migrated.get("message"), migrated.get("detail")):
            return []
    return [{"path": "$", "kind": "parser_result", "original": original, "migrated": migrated}]


__all__ = ["compare_case"]
