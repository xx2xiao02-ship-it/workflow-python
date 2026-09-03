"""174651 原始 add_videos 纯解析摘录与新实现的对照。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .coze_adapter_aigc_animation import run_migrated_normalization, run_original_normalization


ROOT = Path(__file__).parents[1]
SAMPLE_DIR = ROOT / "samples" / "synthetic"


def load_cases() -> list[dict[str, Any]]:
    main = json.loads((SAMPLE_DIR / "aigc_animation_input.json").read_text(encoding="utf-8"))
    boundary = json.loads((SAMPLE_DIR / "aigc_animation_boundary_cases.json").read_text(encoding="utf-8"))
    return [{"name": "synthetic-main", "params": main}, *boundary]


def _capture(fn, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "value": fn(params)}
    except Exception as exc:  # noqa: BLE001 - 审计需要记录原始异常边界
        return {"ok": False, "error": str(exc)}


def compare_case(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    original = _capture(run_original_normalization, params)
    migrated = _capture(run_migrated_normalization, params)
    return original, migrated


def run_audit() -> list[dict[str, Any]]:
    results = []
    for case in load_cases():
        original, migrated = compare_case(case["params"])
        same = original == migrated
        results.append({"name": case["name"], "original": original, "migrated": migrated, "same": same})
    return results


def summarize(results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = results if results is not None else run_audit()
    return {
        "total": len(rows),
        "passed": sum(1 for row in rows if row["same"]),
        "contract_equivalent": all(row["same"] for row in rows),
        "runtime_side_effects_verified": False,
    }


if __name__ == "__main__":
    print(json.dumps(summarize(), ensure_ascii=False, indent=2))
