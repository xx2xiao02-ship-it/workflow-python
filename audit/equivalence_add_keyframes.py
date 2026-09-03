"""添加关键帧的解析行为和 Coze 输出投影对照。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .coze_adapter_add_keyframes import run_migrated_parser, run_original_parser


@dataclass(frozen=True)
class AddKeyframesAuditCase:
    name: str
    category: str
    params: Any


def load_cases() -> list[AddKeyframesAuditCase]:
    root = Path(__file__).parents[1] / "samples" / "synthetic"
    main = json.loads((root / "add_keyframes_input.json").read_text(encoding="utf-8"))
    cases = json.loads((root / "add_keyframes_boundary_cases.json").read_text(encoding="utf-8"))
    return [AddKeyframesAuditCase("synthetic_sample", "synthetic", main)] + [
        AddKeyframesAuditCase(item["name"], item["category"], item["params"]) for item in cases
    ]


def _capture(fn: Any, value: Any) -> dict[str, Any]:
    try: return {"kind": "return", "value": fn(value)}
    except Exception as exc:  # noqa: BLE001
        return {
            "kind": "exception",
            "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc),
            "detail": getattr(exc, "detail", None),
        }


def compare_parser_case(keyframes: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    original = _capture(run_original_parser, keyframes)
    migrated = _capture(run_migrated_parser, keyframes)
    if original.get("kind") == migrated.get("kind") == "exception":
        if (original.get("message"), original.get("detail")) == (migrated.get("message"), migrated.get("detail")):
            return original, migrated, []
    elif original == migrated:
        return original, migrated, []
    return original, migrated, [{"path": "$", "kind": "parser_result", "original": original, "migrated": migrated}]


def run_audit(cases: list[AddKeyframesAuditCase] | None = None) -> list[dict[str, Any]]:
    results = []
    for case in cases or load_cases():
        params = case.params
        if isinstance(params, str): params = json.loads(params)
        if isinstance(params, dict) and isinstance(params.get("input"), dict): params = params["input"]
        original, migrated, differences = compare_parser_case(params.get("keyframes", ""))
        results.append({"name": case.name, "category": case.category, "passed": not differences, "differences": differences, "original": original, "migrated": migrated})
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [item for item in results if not item["passed"]]
    return {"total": len(results), "passed": len(results)-len(failed), "failed": len(failed), "parser_equivalent": not failed, "differences": [d for item in failed for d in item["differences"]]}


__all__ = ["compare_parser_case", "load_cases", "run_audit", "summarize"]
