"""116616 输出逐字段、逐数组项和时间线对照工具。"""

from __future__ import annotations

from typing import Any


VOLATILE_DEBUG_FIELDS = {
    "assemble_ms",
    "build_ms",
    "gpt_ms",
    "mini_ms",
    "plan_validate_ms",
    "total_ms",
}


def compare_results(reference: Any, actual: Any) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []

    def visit(left: Any, right: Any, path: str) -> None:
        if type(left) is not type(right):
            mismatches.append({
                "path": path,
                "kind": "type",
                "reference": type(left).__name__,
                "actual": type(right).__name__,
            })
            return
        if isinstance(left, dict):
            if list(left) != list(right):
                mismatches.append({
                    "path": path,
                    "kind": "field_order",
                    "reference": list(left),
                    "actual": list(right),
                })
            for key in left:
                if key not in right:
                    mismatches.append({"path": f"{path}.{key}", "kind": "missing_field"})
                else:
                    visit(left[key], right[key], f"{path}.{key}")
            return
        if isinstance(left, list):
            if len(left) != len(right):
                mismatches.append({
                    "path": path,
                    "kind": "array_count",
                    "reference": len(left),
                    "actual": len(right),
                })
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                visit(left_item, right_item, f"{path}[{index}]")
            return
        if left != right:
            debug_field = path.removeprefix("$.debug.")
            if path.startswith("$.debug.") and debug_field in VOLATILE_DEBUG_FIELDS:
                return
            mismatches.append({
                "path": path,
                "kind": "value",
                "reference": left,
                "actual": right,
            })

    visit(reference, actual, "$")
    return mismatches


def output_shape(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "fields": list(value),
        "field_types": {key: type(item).__name__ for key, item in value.items()},
        "prompt_count": len(value.get("prompt", [])),
        "ref_image_count": len(value.get("ref_image", [])),
        "motion_seed_count": len(value.get("motion_seed", [])),
        "timeline_count": len(value.get("timelines", [])),
        "duration_count": len(value.get("int_duration", [])),
        "timeline_start_end": [
            [item.get("start"), item.get("end")]
            for item in value.get("timelines", [])
            if isinstance(item, dict)
        ],
        "duration_values": list(value.get("int_duration", [])),
        "debug_fields": list(value.get("debug", {})) if isinstance(value.get("debug"), dict) else None,
    }


__all__ = ["compare_results", "output_shape", "VOLATILE_DEBUG_FIELDS"]
