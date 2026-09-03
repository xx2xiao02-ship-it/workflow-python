"""镜头精细化批处理节点的结构和顺序对照工具。"""

from __future__ import annotations

from typing import Any


def compare_results(reference: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []

    def visit(left: Any, right: Any, path: str) -> None:
        if type(left) is not type(right):
            mismatches.append({"path": path, "kind": "type", "reference": type(left).__name__, "actual": type(right).__name__})
            return
        if isinstance(left, dict):
            if list(left) != list(right):
                mismatches.append({"path": path, "kind": "field_order", "reference": list(left), "actual": list(right)})
            for key in left:
                if key not in right:
                    mismatches.append({"path": f"{path}.{key}", "kind": "missing_field"})
                else:
                    visit(left[key], right[key], f"{path}.{key}")
            return
        if isinstance(left, list):
            if len(left) != len(right):
                mismatches.append({"path": path, "kind": "array_count", "reference": len(left), "actual": len(right)})
            for i, pair in enumerate(zip(left, right)):
                visit(pair[0], pair[1], f"{path}[{i}]")
            return
        if left != right:
            mismatches.append({"path": path, "kind": "value", "reference": left, "actual": right})

    visit(reference, actual, "$")
    return mismatches
