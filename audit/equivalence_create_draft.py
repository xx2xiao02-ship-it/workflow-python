"""创建草稿节点的响应契约对照工具。

该插件节点没有随 Coze 工作流导出原始 Python 代码，不能伪造“原代码 vs
新代码”的代码级等价结论。本模块只比较给定的参考响应与适配器响应，供拿到
真实 Coze/插件响应后继续使用。
"""

from __future__ import annotations

from typing import Any


def compare_responses(reference: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []

    if list(reference) != list(actual):
        mismatches.append({
            "path": "$",
            "kind": "field_order",
            "reference": list(reference),
            "actual": list(actual),
        })

    for key in reference:
        if key not in actual:
            mismatches.append({"path": f"$.{key}", "kind": "missing_field"})
            continue
        if type(reference[key]) is not type(actual[key]):
            mismatches.append({
                "path": f"$.{key}",
                "kind": "type",
                "reference": type(reference[key]).__name__,
                "actual": type(actual[key]).__name__,
            })
        elif reference[key] != actual[key]:
            mismatches.append({
                "path": f"$.{key}",
                "kind": "value",
                "reference": reference[key],
                "actual": actual[key],
            })

    for key in actual:
        if key not in reference:
            mismatches.append({"path": f"$.{key}", "kind": "extra_field"})

    return mismatches


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [item for item in results if item.get("mismatches")]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failed_cases": [item.get("name") for item in failed],
        "coze_code_level_equivalent": False,
        "reason": "插件节点没有随工作流导出原始 Python 代码；当前仅完成响应契约对照",
    }
