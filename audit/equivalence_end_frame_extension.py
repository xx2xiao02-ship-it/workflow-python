"""“首尾帧顺延”节点（170263）的逐字段旧新对照。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .coze_adapter_170263 import run_both
from .equivalence import compare_outputs


@dataclass(frozen=True)
class AuditCase:
    name: str
    category: str
    params: Any


def load_cases() -> list[AuditCase]:
    root = Path(__file__).parents[1]
    sample_dir = root / "samples" / "synthetic"
    main_input = json.loads((sample_dir / "end_frame_extension_input.json").read_text(encoding="utf-8"))
    raw_cases = json.loads((sample_dir / "end_frame_extension_cases.json").read_text(encoding="utf-8"))
    return [AuditCase("synthetic_sample", "existing synthetic", main_input)] + [
        AuditCase(item["name"], item["category"], item["params"])
        for item in raw_cases
    ]


def audit_case(case: AuditCase) -> dict[str, Any]:
    original, migrated = run_both(case.params)
    mismatches = compare_outputs(original, migrated)
    return {
        "name": case.name,
        "category": case.category,
        "passed": not mismatches,
        "original": original,
        "migrated": migrated,
        "mismatches": mismatches,
    }


def run_audit(cases: list[AuditCase] | None = None) -> list[dict[str, Any]]:
    return [audit_case(case) for case in cases or load_cases()]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [item for item in results if not item["passed"]]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failed_cases": [item["name"] for item in failed],
        "code_level_equivalent": not failed,
    }
