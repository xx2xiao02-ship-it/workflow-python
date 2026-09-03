"""没有模型源码时的“图生视频提示词导演”契约对照工具。

这里不伪造原代码等价结论，只比较固定响应经过本地 Schema 归一化后的结果，
并明确标注 code_level_equivalent 不适用。
"""

from __future__ import annotations

from typing import Any

from .equivalence import compare_outputs


def audit_reference_response(reference: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    mismatches = compare_outputs(reference, actual)
    return {
        "passed": not mismatches,
        "reference": reference,
        "actual": actual,
        "mismatches": mismatches,
        "code_level_equivalent": False,
        "reason": "LLM 节点没有可执行原始 Python 源码；此处仅为固定响应契约对照",
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [item for item in results if not item["passed"]]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "code_level_equivalent": False,
        "reason": "LLM 内部模型调用和随机响应未在本地执行",
    }
