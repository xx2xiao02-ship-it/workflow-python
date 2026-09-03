from __future__ import annotations

from typing import Any

from workflow_1256.save_draft import run_save_draft


def run_migrated(params: Any, transport: Any) -> dict[str, str]:
    return run_save_draft(params, transport=transport)


def run_original_contract(params: Any, transport: Any) -> dict[str, str]:
    value = params
    if isinstance(value, str):
        import json
        value = json.loads(value)
    if isinstance(value, dict) and isinstance(value.get("input"), dict): value = value["input"]
    draft_url = value["draft_url"]
    result = transport(type("SaveDraftRequest", (), {"draft_url": draft_url})())
    return {"draft_url": result["draft_url"], "message": result.get("message", "")}


__all__ = ["run_migrated", "run_original_contract"]
