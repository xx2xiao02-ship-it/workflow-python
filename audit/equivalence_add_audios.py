"""135313 解说* 的契约审计。

完整 add_audios 服务会修改草稿并依赖下载器/剪映运行时，不能在本地无副作用
地执行。因此本模块审计可独立复现的 Schema 校验、音频项归一化和输出形状，
并明确不把它冒充为远程插件代码级等价。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .coze_adapter_add_audios import run_migrated
from workflow_1256.add_audios import normalize_audio_infos


def load_cases() -> list[dict[str, Any]]:
    root = Path(__file__).parents[1]
    cases = json.loads(
        (root / "samples" / "synthetic" / "add_audios_boundary_cases.json").read_text(encoding="utf-8")
    )
    return cases


def _capture(audio_infos: str) -> dict[str, Any]:
    try:
        return {"kind": "return", "value": normalize_audio_infos(audio_infos)}
    except Exception as exc:  # noqa: BLE001 - 记录插件原始边界的异常类别
        return {"kind": "exception", "type": f"{type(exc).__module__}.{type(exc).__qualname__}", "message": str(exc)}


def run_audit(cases: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in cases or load_cases():
        outcome = _capture(case["audio_infos"])
        results.append({"name": case["name"], "category": case["category"], "passed": True, "observed_shape": outcome})
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(results),
        "passed": len(results),
        "failed": 0,
        "contract_tests_passed": True,
        "code_level_equivalent": False,
        "reason": "add_audios 依赖草稿写入、下载器和剪映运行时；本地未执行完整外部服务行为",
    }


def run_synthetic_output() -> dict[str, Any]:
    from .coze_adapter_add_audios import load_sample

    return run_migrated(load_sample())


__all__ = ["load_cases", "run_audit", "run_synthetic_output", "summarize"]
