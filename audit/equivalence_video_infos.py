"""115723 “AIGC动画”原始源码与新实现的逐字段对照。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .coze_adapter_video_infos import run_migrated, run_original


@dataclass(frozen=True)
class VideoInfosAuditCase:
    name: str
    category: str
    params: Any


def load_cases() -> list[VideoInfosAuditCase]:
    root = Path(__file__).parents[1]
    sample_dir = root / "samples" / "synthetic"
    main_input = json.loads((sample_dir / "video_infos_input.json").read_text(encoding="utf-8"))
    boundary_cases = json.loads(
        (sample_dir / "video_infos_boundary_cases.json").read_text(encoding="utf-8")
    )
    return [VideoInfosAuditCase("synthetic_sample", "synthetic", main_input)] + [
        VideoInfosAuditCase(item["name"], item["category"], item["params"])
        for item in boundary_cases
    ]


def _capture(call: Callable[[Any], dict[str, str]], params: Any) -> dict[str, Any]:
    try:
        return {"kind": "return", "value": call(params)}
    except Exception as exc:  # noqa: BLE001 - 审计必须记录原始异常行为
        return {
            "kind": "exception",
            "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc),
        }


def _parsed_infos(outcome: dict[str, Any]) -> Any:
    if outcome.get("kind") != "return":
        return None
    output = outcome["value"]
    if not isinstance(output, dict) or not isinstance(output.get("infos"), str):
        return None
    return json.loads(output["infos"])


def _compare_values(left: Any, right: Any, path: str = "$") -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    if type(left) is not type(right):
        return [{"path": path, "kind": "type", "original": left, "migrated": right}]
    if isinstance(left, dict):
        if list(left) != list(right):
            differences.append({"path": path, "kind": "field_order", "original": list(left), "migrated": list(right)})
        for key in left:
            if key not in right:
                differences.append({"path": f"{path}.{key}", "kind": "missing_field"})
            else:
                differences.extend(_compare_values(left[key], right[key], f"{path}.{key}"))
        for key in right:
            if key not in left:
                differences.append({"path": f"{path}.{key}", "kind": "extra_field"})
        return differences
    if isinstance(left, list):
        if len(left) != len(right):
            differences.append({"path": path, "kind": "array_count", "original": len(left), "migrated": len(right)})
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            differences.extend(_compare_values(left_item, right_item, f"{path}[{index}]"))
        return differences
    if left != right:
        differences.append({"path": path, "kind": "value", "original": left, "migrated": right})
    return differences


def compare_case(params: Any) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    original = _capture(run_original, params)
    migrated = _capture(run_migrated, params)
    differences: list[dict[str, Any]] = []
    if original.get("kind") != migrated.get("kind"):
        differences.append({"path": "$", "kind": "outcome_kind", "original": original, "migrated": migrated})
    elif original.get("kind") == "exception":
        for key in ("type", "message"):
            if original.get(key) != migrated.get(key):
                differences.append({"path": f"$.exception.{key}", "kind": "value", "original": original.get(key), "migrated": migrated.get(key)})
    else:
        differences.extend(_compare_values(original["value"], migrated["value"]))
        original_infos = _parsed_infos(original)
        migrated_infos = _parsed_infos(migrated)
        if original_infos is None or migrated_infos is None:
            differences.append({"path": "$.infos", "kind": "invalid_json_string"})
        else:
            differences.extend(_compare_values(original_infos, migrated_infos, "$.infos.json"))
    return original, migrated, differences


def output_shape(outcome: dict[str, Any]) -> dict[str, Any]:
    if outcome.get("kind") == "exception":
        return {"kind": "exception", "type": outcome.get("type"), "message": outcome.get("message")}
    output = outcome.get("value", {})
    infos = _parsed_infos(outcome)
    return {
        "kind": "return",
        "field_order": list(output) if isinstance(output, dict) else [],
        "field_types": {key: type(value).__name__ for key, value in output.items()} if isinstance(output, dict) else {},
        "infos_count": len(infos) if isinstance(infos, list) else None,
        "item_field_order": [list(item) for item in infos] if isinstance(infos, list) and all(isinstance(item, dict) for item in infos) else [],
        "timeline_start_end": [[item.get("start"), item.get("end")] for item in infos if isinstance(item, dict)] if isinstance(infos, list) else [],
        "duration_values": [item.get("duration") for item in infos if isinstance(item, dict)] if isinstance(infos, list) else [],
    }


def run_audit(cases: list[VideoInfosAuditCase] | None = None) -> list[dict[str, Any]]:
    results = []
    for case in cases or load_cases():
        original, migrated, differences = compare_case(case.params)
        results.append({
            "name": case.name,
            "category": case.category,
            "passed": not differences,
            "original_shape": output_shape(original),
            "migrated_shape": output_shape(migrated),
            "mismatches": differences,
        })
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [item for item in results if not item["passed"]]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failed_cases": [item["name"] for item in failed],
        "code_level_equivalent": not failed,
    }


__all__ = ["compare_case", "load_cases", "output_shape", "run_audit", "summarize"]
