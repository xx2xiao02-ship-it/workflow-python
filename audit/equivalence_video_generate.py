"""video_generate 节点的逐字段旧新对照模块。"""

from __future__ import annotations

from typing import Any

from .coze_adapter_video_generate import run_both
from .equivalence import compare_outputs


def audit_case(name: str, params: Any, events: list[Any]) -> dict[str, Any]:
    original, migrated, _, _ = run_both(params, events)
    mismatches = compare_outputs(original, migrated)
    return {
        "name": name,
        "passed": not mismatches,
        "original": original,
        "migrated": migrated,
        "mismatches": mismatches,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [item for item in results if not item["passed"]]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failed_cases": [item["name"] for item in failed],
        "code_level_equivalent": not failed,
    }
