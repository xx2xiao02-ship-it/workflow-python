from __future__ import annotations

from typing import Any

from .coze_adapter_workflow_end import run_migrated, run_original_contract


def compare_case(params: Any) -> list[dict[str, Any]]:
    try: original=run_original_contract(params)
    except Exception as exc: original={"error":str(exc)}
    try: migrated=run_migrated(params)
    except Exception as exc: migrated={"error":str(exc)}
    return [] if original==migrated else [{"path":"$","original":original,"migrated":migrated}]


__all__=["compare_case"]
