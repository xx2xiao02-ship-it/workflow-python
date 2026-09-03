from __future__ import annotations

from typing import Any

from workflow_1256.workflow_end import run_workflow_end


def run_migrated(params: Any) -> dict[str, str]: return run_workflow_end(params)


def run_original_contract(params: Any) -> dict[str, str]:
    value=params
    if isinstance(value,dict) and isinstance(value.get("input"),dict): value=value["input"]
    return {"content": value["draft_url"]}


__all__=["run_migrated","run_original_contract"]
