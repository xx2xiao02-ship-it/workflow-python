from __future__ import annotations

from typing import Any

from .coze_adapter_add_effects_second import run_parser_both


def compare_case(effect_infos: str) -> list[dict[str, Any]]:
    try: original, migrated = run_parser_both(effect_infos)
    except Exception as exc: return [{"path":"$","error":str(exc)}]
    return [] if original == migrated else [{"path":"$","original":original,"migrated":migrated}]


__all__ = ["compare_case"]
