"""Image-2 两个插件的旧代码/新实现逐字段对照工具。"""

from __future__ import annotations

from typing import Any

from .coze_adapter_image_plugins import (
    ExecutionOutcome,
    run_migrated_create,
    run_migrated_poll,
    run_original_create,
    run_original_poll,
)


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _repr(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_repr(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _repr(item) for key, item in value.items()}
    return repr(value)


def compare_values(left: Any, right: Any, path: str = "$") -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if _json_type(left) != _json_type(right):
        return [{
            "path": path,
            "kind": "type",
            "original_type": _json_type(left),
            "migrated_type": _json_type(right),
            "original": _repr(left),
            "migrated": _repr(right),
        }]
    if isinstance(left, dict):
        if list(left) != list(right):
            mismatches.append({
                "path": path,
                "kind": "field_order",
                "original": list(left),
                "migrated": list(right),
            })
        for key in left:
            if key not in right:
                mismatches.append({"path": f"{path}.{key}", "kind": "missing_field"})
            else:
                mismatches.extend(compare_values(left[key], right[key], f"{path}.{key}"))
        for key in right:
            if key not in left:
                mismatches.append({"path": f"{path}.{key}", "kind": "extra_field"})
        return mismatches
    if isinstance(left, list):
        if len(left) != len(right):
            mismatches.append({
                "path": path,
                "kind": "array_count",
                "original_count": len(left),
                "migrated_count": len(right),
            })
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            mismatches.extend(compare_values(left_item, right_item, f"{path}[{index}]"))
        return mismatches
    if left != right:
        mismatches.append({
            "path": path,
            "kind": "value",
            "original": _repr(left),
            "migrated": _repr(right),
        })
    return mismatches


def compare_outcomes(original: ExecutionOutcome, migrated: ExecutionOutcome) -> list[dict[str, Any]]:
    if original.raised or migrated.raised:
        mismatches: list[dict[str, Any]] = []
        if original.exception_type != migrated.exception_type:
            mismatches.append({
                "path": "$.exception_type",
                "kind": "value",
                "original": original.exception_type,
                "migrated": migrated.exception_type,
            })
        if original.exception_message != migrated.exception_message:
            mismatches.append({
                "path": "$.exception_message",
                "kind": "value",
                "original": original.exception_message,
                "migrated": migrated.exception_message,
            })
        return mismatches
    return compare_values(original.value, migrated.value)


def audit_create_case(name: str, params: Any, events: list[Any]) -> dict[str, Any]:
    original, _ = run_original_create(params, events)
    migrated, _ = run_migrated_create(params, events)
    mismatches = compare_outcomes(original, migrated)
    return {
        "name": name,
        "plugin": "create_image_task",
        "passed": not mismatches,
        "original": original,
        "migrated": migrated,
        "mismatches": mismatches,
    }


def audit_poll_case(name: str, params: Any, events: list[Any]) -> dict[str, Any]:
    original, _, _ = run_original_poll(params, events)
    migrated, _, _ = run_migrated_poll(params, events)
    mismatches = compare_outcomes(original, migrated)
    return {
        "name": name,
        "plugin": "get_task_result",
        "passed": not mismatches,
        "original": original,
        "migrated": migrated,
        "mismatches": mismatches,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [result for result in results if not result["passed"]]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failed_cases": [result["name"] for result in failed],
        "code_level_equivalent": not failed,
    }
