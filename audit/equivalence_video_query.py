"""video_query 节点的逐字段旧新对照模块。"""

from __future__ import annotations

from typing import Any

from .coze_adapter_video_query import run_both
from .equivalence import compare_outputs


def audit_case(name: str, params: Any, events: list[Any]) -> dict[str, Any]:
    original, migrated, _, _, original_clock, migrated_clock = run_both(params, events)
    mismatches = compare_outputs(original, migrated)
    if original_clock.sleeps != migrated_clock.sleeps:
        mismatches.append({
            "path": "$.sleep_schedule",
            "kind": "value",
            "original": original_clock.sleeps,
            "migrated": migrated_clock.sleeps,
        })
    return {
        "name": name,
        "passed": not mismatches,
        "original": original,
        "migrated": migrated,
        "original_sleeps": original_clock.sleeps,
        "migrated_sleeps": migrated_clock.sleeps,
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
