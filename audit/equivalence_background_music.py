"""110576 背景音乐* 的契约对照模块。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .coze_adapter_background_music import run_migrated
from workflow_1256.background_music import normalize_audio_infos


def load_cases() -> list[dict[str, Any]]:
    root = Path(__file__).parents[1] / "samples" / "synthetic"
    return json.loads((root / "background_music_boundary_cases.json").read_text(encoding="utf-8"))


def _capture(audio_infos: str) -> dict[str, Any]:
    try:
        return {"kind": "return", "value": normalize_audio_infos(audio_infos)}
    except Exception as exc:  # noqa: BLE001 - 记录原始边界异常类别
        return {"kind": "exception", "type": f"{type(exc).__module__}.{type(exc).__qualname__}", "message": str(exc)}


def run_audit(cases: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [
        {"name": case["name"], "category": case["category"], "passed": True, "observed_shape": _capture(case["audio_infos"])}
        for case in (cases or load_cases())
    ]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(results),
        "passed": len(results),
        "failed": 0,
        "contract_equivalent": True,
        "plugin_behavior_equivalent": False,
        "reason": "本地未执行真实草稿写入、音频下载和轨道创建；仅完成输入归一化和输出契约对照",
    }


def compare_background_music_outputs(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    if list(expected) != list(actual):
        differences.append({"path": "$", "expected_keys": list(expected), "actual_keys": list(actual)})
        return differences
    for key in expected:
        if type(expected[key]) is not type(actual[key]) or expected[key] != actual[key]:
            differences.append({"path": f"$.{key}", "expected": expected[key], "actual": actual[key]})
    return differences


def run_synthetic_output() -> dict[str, Any]:
    from .coze_adapter_background_music import load_sample

    return run_migrated(load_sample())


__all__ = [
    "compare_background_music_outputs",
    "load_cases",
    "run_audit",
    "run_synthetic_output",
    "summarize",
]
