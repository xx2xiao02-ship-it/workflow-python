"""116930 原始 add_videos 纯解析摘录与新实现的逐场景对照。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .coze_adapter_digital_human import (
    run_migrated_normalization,
    run_original_normalization,
)


ROOT = Path(__file__).parents[1]
SAMPLE_DIR = ROOT / "samples" / "synthetic"


def load_cases() -> list[dict[str, Any]]:
    main = json.loads((SAMPLE_DIR / "digital_human_input.json").read_text(encoding="utf-8"))
    boundary = json.loads(
        (SAMPLE_DIR / "digital_human_boundary_cases.json").read_text(encoding="utf-8")
    )
    return [{"name": "synthetic-main", "params": main}, *boundary]


def _error_family(exc: Exception) -> str:
    for error_type in (KeyError, TypeError, ValueError, RuntimeError):
        if isinstance(exc, error_type):
            return error_type.__name__
    return type(exc).__name__


def _capture(fn: Callable[[Any], Any], params: Any) -> dict[str, Any]:
    try:
        return {"ok": True, "value": fn(params)}
    except Exception as exc:  # noqa: BLE001 - 审计必须保留原异常边界
        return {
            "ok": False,
            "error_type": _error_family(exc),
            "error": str(exc),
        }


def compare_case(params: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        _capture(run_original_normalization, params),
        _capture(run_migrated_normalization, params),
    )


def run_audit() -> list[dict[str, Any]]:
    results = []
    for case in load_cases():
        original, migrated = compare_case(case["params"])
        results.append(
            {
                "name": case["name"],
                "original": original,
                "migrated": migrated,
                "same": original == migrated,
            }
        )
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

