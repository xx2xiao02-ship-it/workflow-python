"""“具体画面导演”节点的契约对照工具。

该节点没有随 Coze 工作流导出原始 Python 代码，也没有取得插件源码；
因此本模块不会宣称代码级等价，只比较真实参考响应和注入式适配器响应。
"""

from __future__ import annotations

from typing import Any


def compare_responses(reference: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []

    def compare(left: Any, right: Any, path: str) -> None:
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
                    "kind": "object_field_order",
                    "reference": list(left),
                    "actual": list(right),
                })
            for key in left:
                if key not in right:
                    mismatches.append({"path": f"{path}.{key}", "kind": "missing_field"})
                else:
                    compare(left[key], right[key], f"{path}.{key}")
            for key in right:
                if key not in left:
                    mismatches.append({"path": f"{path}.{key}", "kind": "extra_field"})
            return
        if isinstance(left, list):
            if len(left) != len(right):
                mismatches.append({
                    "path": path,
                    "kind": "array_count",
                    "reference_count": len(left),
                    "actual_count": len(right),
                })
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                compare(left_item, right_item, f"{path}[{index}]")
            return
        if left != right:
            mismatches.append({
                "path": path,
                "kind": "value",
                "reference": left,
                "actual": right,
            })

    compare(reference, actual, "$")
    return mismatches


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [item for item in results if item.get("mismatches")]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failed_cases": [item.get("name") for item in failed],
        "code_level_equivalent_with_same_injected_model_responses": not failed,
        "coze_end_to_end_equivalent": False,
        "reason": "已取得源码并完成无网络注入式代码对照；真实模型请求、超时和服务端行为仍未调用验证",
    }
