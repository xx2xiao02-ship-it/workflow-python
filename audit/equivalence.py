"""通用结构化输出对照工具。"""

from __future__ import annotations

from typing import Any

def json_type(value: Any) -> str:
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


def _value_repr(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_value_repr(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _value_repr(item) for key, item in value.items()}
    return repr(value)


def compare_outputs(original: dict[str, Any], migrated: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []

    original_order = list(original)
    migrated_order = list(migrated)
    if original_order != migrated_order:
        mismatches.append({
            "path": "$",
            "kind": "field_order",
            "original": original_order,
            "migrated": migrated_order,
        })

    def compare(left: Any, right: Any, path: str) -> None:
        left_type = json_type(left)
        right_type = json_type(right)

        if left_type != right_type:
            mismatches.append({
                "path": path,
                "kind": "type",
                "original_type": left_type,
                "migrated_type": right_type,
                "original": _value_repr(left),
                "migrated": _value_repr(right),
            })
            return

        if isinstance(left, dict):
            left_keys = list(left)
            right_keys = list(right)
            for key in left_keys:
                if key not in right:
                    mismatches.append({"path": f"{path}.{key}", "kind": "missing_field"})
                else:
                    compare(left[key], right[key], f"{path}.{key}")
            for key in right_keys:
                if key not in left:
                    mismatches.append({"path": f"{path}.{key}", "kind": "extra_field"})
            if left_keys != right_keys:
                mismatches.append({
                    "path": path,
                    "kind": "object_field_order",
                    "original": left_keys,
                    "migrated": right_keys,
                })
            return

        if isinstance(left, list):
            if len(left) != len(right):
                mismatches.append({
                    "path": path,
                    "kind": "array_count",
                    "original_count": len(left),
                    "migrated_count": len(right),
                    "original": _value_repr(left),
                    "migrated": _value_repr(right),
                })
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                compare(left_item, right_item, f"{path}[{index}]")
            return

        if left != right:
            mismatches.append({
                "path": path,
                "kind": "value",
                "original": _value_repr(left),
                "migrated": _value_repr(right),
            })

    compare(original, migrated, "$")
    return mismatches


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [result for result in results if not result["passed"]]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failed_cases": [result["name"] for result in failed],
        "code_level_equivalent": not failed,
    }
