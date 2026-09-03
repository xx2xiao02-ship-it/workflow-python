"""关键帧信息节点的原始源码/新实现逐字段对照。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .coze_adapter_keyframes_infos import run_migrated, run_original


@dataclass(frozen=True)
class KeyframesAuditCase:
    name: str
    category: str
    params: Any


def load_cases() -> list[KeyframesAuditCase]:
    root = Path(__file__).parents[1] / "samples" / "synthetic"
    main = json.loads((root / "keyframes_infos_input.json").read_text(encoding="utf-8"))
    boundary = json.loads((root / "keyframes_infos_boundary_cases.json").read_text(encoding="utf-8"))
    return [KeyframesAuditCase("synthetic_sample", "synthetic", main)] + [
        KeyframesAuditCase(item["name"], item["category"], item["params"])
        for item in boundary
    ]


def _capture(call: Callable[[Any], dict[str, str]], params: Any) -> dict[str, Any]:
    try:
        return {"kind": "return", "value": call(params)}
    except Exception as exc:  # noqa: BLE001 - audit records original behavior
        return {
            "kind": "exception",
            "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc),
        }


def _compare(left: Any, right: Any, path: str = "$") -> list[dict[str, Any]]:
    if type(left) is not type(right):
        return [{"path": path, "kind": "type", "original": left, "migrated": right}]
    if isinstance(left, dict):
        differences: list[dict[str, Any]] = []
        if list(left) != list(right):
            differences.append({"path": path, "kind": "field_order", "original": list(left), "migrated": list(right)})
        for key in left:
            if key not in right:
                differences.append({"path": f"{path}.{key}", "kind": "missing_field"})
            else:
                differences.extend(_compare(left[key], right[key], f"{path}.{key}"))
        for key in right:
            if key not in left:
                differences.append({"path": f"{path}.{key}", "kind": "extra_field"})
        return differences
    if isinstance(left, list):
        differences = []
        if len(left) != len(right):
            differences.append({"path": path, "kind": "array_count", "original": len(left), "migrated": len(right)})
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            differences.extend(_compare(left_item, right_item, f"{path}[{index}]"))
        return differences
    if left != right:
        return [{"path": path, "kind": "value", "original": left, "migrated": right}]
    return []


def _parsed_keyframes(outcome: dict[str, Any]) -> Any:
    if outcome.get("kind") != "return":
        return None
    output = outcome.get("value", {})
    if not isinstance(output, dict) or not isinstance(output.get("keyframes_infos"), str):
        return None
    return json.loads(output["keyframes_infos"])


def compare_case(params: Any) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    original = _capture(run_original, params)
    migrated = _capture(run_migrated, params)
    differences: list[dict[str, Any]] = []
    if original.get("kind") != migrated.get("kind"):
        differences.append({"path": "$", "kind": "outcome_kind", "original": original, "migrated": migrated})
    elif original.get("kind") == "exception":
        for key in ("type", "message"):
            if original.get(key) != migrated.get(key):
                differences.append({"path": f"$.exception.{key}", "kind": "value", "original": original.get(key), "migrated": migrated.get(key)})
    else:
        differences.extend(_compare(original["value"], migrated["value"]))
        left = _parsed_keyframes(original)
        right = _parsed_keyframes(migrated)
        if left is None or right is None:
            differences.append({"path": "$.keyframes_infos", "kind": "invalid_json_string"})
        else:
            differences.extend(_compare(left, right, "$.keyframes_infos.json"))
    return original, migrated, differences


def output_shape(outcome: dict[str, Any]) -> dict[str, Any]:
    if outcome.get("kind") == "exception":
        return {"kind": "exception", "type": outcome.get("type"), "message": outcome.get("message")}
    output = outcome.get("value", {})
    items = _parsed_keyframes(outcome) or []
    return {
        "kind": "return",
        "field_order": list(output),
        "field_types": {key: type(value).__name__ for key, value in output.items()},
        "keyframes_count": len(items),
        "item_field_order": [list(item) for item in items],
        "offsets": [item.get("offset") for item in items],
        "properties": [item.get("property") for item in items],
        "segment_ids": [item.get("segment_id") for item in items],
        "values": [item.get("value") for item in items],
    }


def run_audit(cases: list[KeyframesAuditCase] | None = None) -> list[dict[str, Any]]:
    results = []
    for case in cases or load_cases():
        original, migrated, differences = compare_case(case.params)
        results.append({
            "name": case.name,
            "category": case.category,
            "passed": not differences,
            "differences": differences,
            "original_shape": output_shape(original),
            "migrated_shape": output_shape(migrated),
        })
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [result for result in results if not result["passed"]]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "code_level_equivalent": not failed,
        "differences": [item for result in failed for item in result["differences"]],
    }


__all__ = ["compare_case", "load_cases", "run_audit", "summarize"]
