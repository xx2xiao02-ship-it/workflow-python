from __future__ import annotations

from typing import Any

from .coze_adapter_add_effects import run_migrated_parser, run_original_parser


def compare_case(effect_infos: str) -> list[dict[str, Any]]:
    def capture(fn: Any) -> dict[str, Any]:
        try: return {"kind":"return","value":fn(effect_infos)}
        except Exception as exc: return {"kind":"exception","message":str(exc),"detail":getattr(exc,"detail",None)}
    original, migrated = capture(run_original_parser), capture(run_migrated_parser)
    if original == migrated: return []
    if original.get("kind") == migrated.get("kind") == "exception":
        original_text = original.get("detail") or original.get("message")
        migrated_text = migrated.get("detail") or migrated.get("message")
        if original_text == migrated_text: return []
    return [{"path":"$","original":original,"migrated":migrated}]


__all__ = ["compare_case"]
