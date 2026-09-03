from __future__ import annotations

from typing import Any

from .coze_adapter_save_draft import run_migrated, run_original_contract


def compare_case(params: Any) -> list[dict[str, Any]]:
    def transport(request: Any) -> dict[str, str]: return {"draft_url": request.draft_url, "message": "草稿保存成功"}
    try: original = run_original_contract(params, transport)
    except Exception as exc: original = {"error": str(exc)}
    try: migrated = run_migrated(params, transport)
    except Exception as exc: migrated = {"error": str(exc)}
    return [] if original == migrated else [{"path":"$","original":original,"migrated":migrated}]


__all__ = ["compare_case"]
