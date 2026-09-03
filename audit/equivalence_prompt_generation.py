"""节点 113743 的逐字段旧新代码级对照。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .coze_adapter_113743 import run_both
from .equivalence import compare_outputs, json_type


@dataclass(frozen=True)
class AuditCase:
    name: str
    category: str
    params: Any


def load_cases() -> list[AuditCase]:
    root = Path(__file__).parents[1]
    sample_dir = root / "samples" / "synthetic"
    main_input = json.loads((sample_dir / "prompt_generation_input.json").read_text(encoding="utf-8"))
    boundaries = json.loads((sample_dir / "prompt_generation_boundary_cases.json").read_text(encoding="utf-8"))
    return [AuditCase("synthetic_sample", "synthetic", main_input)] + [
        AuditCase(item["name"], item["category"], item["params"])
        for item in boundaries
    ]


def _timeline_ranges(prompts: Any) -> list[list[str]]:
    if not isinstance(prompts, list):
        return []
    pattern = re.compile(r"\d+(?:\.\d+)?-\d+(?:\.\d+)?(?:秒|s)")
    return [pattern.findall(item) if isinstance(item, str) else [] for item in prompts]


def output_shape(output: dict[str, Any]) -> dict[str, Any]:
    return {
        "field_order": list(output),
        "field_types": {key: json_type(value) for key, value in output.items()},
        "array_counts": {
            key: len(value) for key, value in output.items() if isinstance(value, list)
        },
        "duration_values": list(output.get("int_duration", [])),
        "timeline_ranges": _timeline_ranges(output.get("video_prompt", [])),
        "first_frame_order": list(output.get("ref_image_f", [])),
        "end_frame_order": list(output.get("ref_image_e", [])),
        "camera_fixed_order": list(output.get("camera_fixed", [])),
        "error": output.get("error"),
    }


def audit_case(case: AuditCase) -> dict[str, Any]:
    original, migrated = run_both(case.params)
    mismatches = compare_outputs(original, migrated)
    return {
        "name": case.name,
        "category": case.category,
        "passed": not mismatches,
        "original_shape": output_shape(original),
        "migrated_shape": output_shape(migrated),
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
