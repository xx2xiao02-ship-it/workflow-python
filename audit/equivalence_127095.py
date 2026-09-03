"""Detailed old-vs-new comparison for “时长计算时间线规划”."""

from __future__ import annotations

from typing import Any

from .coze_adapter_127095 import run_both
from .equivalence import compare_outputs


def _normalize_json_numbers(value: Any) -> Any:
    """Treat JSON integer and floating-point spellings as the same Number.

    The Coze page serializes an integral ``Number`` such as 9.0 as ``9``.
    This normalization is used only for the page-captured-vs-code comparison;
    the old-vs-new code comparison remains strict about Python value types.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return [_normalize_json_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_json_numbers(item) for key, item in value.items()}
    return value


def compare_captured_outputs(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare page-captured JSON without false int-vs-float Number diffs."""

    return compare_outputs(_normalize_json_numbers(left), _normalize_json_numbers(right))


def audit_case(name: str, params: Any) -> dict[str, Any]:
    original, migrated = run_both(params)
    mismatches = compare_outputs(original, migrated)
    return {
        "name": name,
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
