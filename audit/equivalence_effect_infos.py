from __future__ import annotations

import json
from typing import Any

from .coze_adapter_effect_infos import run_migrated, run_original


def compare_case(params: Any) -> list[dict[str, Any]]:
    try: original = {"kind": "return", "value": run_original(params)}
    except Exception as exc: original = {"kind": "exception", "type": type(exc).__name__, "message": str(exc)}
    try: migrated = {"kind": "return", "value": run_migrated(params)}
    except Exception as exc: migrated = {"kind": "exception", "type": type(exc).__name__, "message": str(exc)}
    if original.get("kind") == migrated.get("kind") == "exception" and original.get("message") == migrated.get("message"):
        return []
    if original.get("kind") == migrated.get("kind") == "return":
        left = json.loads(original["value"]["infos"])
        right = json.loads(migrated["value"]["infos"])
        if left == right and list(original["value"]) == list(migrated["value"]): return []
    return [{"path": "$", "original": original, "migrated": migrated}]


__all__ = ["compare_case"]
