from __future__ import annotations

import json
from typing import Any

from .coze_adapter_effect_infos_second import run_both_second


def compare_case(params: Any) -> list[dict[str, Any]]:
    try: original, migrated = run_both_second(params)
    except Exception as exc: return [{"path":"$","error":str(exc)}]
    if original == migrated: return []
    if json.loads(original["infos"]) == json.loads(migrated["infos"]): return []
    return [{"path":"$.infos","original":original,"migrated":migrated}]


__all__ = ["compare_case"]
