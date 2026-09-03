"""``directors_v2`` 原始源码与 Python 移植的无网络运行适配器。

原始源码中的密钥赋值已在脱敏副本中清空。运行时只注入两个固定的模型文本
响应，并把源码的网络函数替换为内存函数，因此不会访问任何外部服务。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workflow_1256.directors_v2_logic import (
    build_units,
    run_directors_v2_logic,
)


SOURCE_PATH = Path(__file__).parents[0] / "source" / "129109_coze_original_sanitized.py"


def load_original_namespace() -> dict[str, Any]:
    source = SOURCE_PATH.read_text(encoding="utf-8-sig")
    namespace: dict[str, Any] = {"__name__": "directors_v2_original_sandbox"}
    exec(compile(source, str(SOURCE_PATH), "exec"), namespace)
    return namespace


def _model_payloads(input_value: dict[str, Any], reference_output: dict[str, Any]) -> tuple[str, str]:
    text = input_value["text"]
    units = build_units(text)
    cursor = 0
    raw_segments: list[dict[str, Any]] = []

    for item in reference_output["segment_beats"]:
        target = item["segment_text"]
        ids: list[str] = []
        combined = ""
        while cursor < len(units) and combined != target:
            ids.append(units[cursor]["id"])
            combined += units[cursor]["text"]
            cursor += 1
        if combined != target:
            raise AssertionError("真实 segment_text 无法映射回源码 Unit 顺序")

        beat = item["beats"][0]
        raw_segments.append({
            "u": ids,
            "r": item["rhythm"],
            "g": item["segment_goal"],
            "l": beat["relation"],
            "n": beat["expression_need"],
            "v": beat["route_candidates"],
        })

    plan = reference_output["director_plan"]
    director_payload = {
        "d": {
            "core": plan["core"],
            "tone": plan["tone"],
            "emo": plan["emo"],
            "goal": plan["goal"],
            "open": plan["open"],
            "spine": plan["spine"],
            "arc": plan["arc"],
            "expression_domains": plan["expression_domains"],
            "variation_focus": plan["variation_focus"],
            "rule": plan["rule"],
        },
        "seg": raw_segments,
    }
    pick_payload = {"dir": plan["director_type"], "sub": "", "why": "audit fixture", "hint": []}
    return (
        json.dumps(pick_payload, ensure_ascii=False),
        json.dumps(director_payload, ensure_ascii=False),
    )


def run_original(input_value: dict[str, Any], reference_output: dict[str, Any]) -> dict[str, Any]:
    pick_raw, director_raw = _model_payloads(input_value, reference_output)
    return run_original_with_model_responses(input_value, pick_raw, director_raw)


def run_original_with_model_responses(
    input_value: dict[str, Any],
    pick_raw: str,
    director_raw: str,
) -> dict[str, Any]:
    namespace = load_original_namespace()
    # 仅为通过原源码中的“是否配置密钥”防误调用检查，使用不可用的审计哨兵值；
    # call_ark/call_gpt 已被内存函数覆盖，不会把该值用于网络请求。
    namespace["ARK_API_KEY"] = "audit-dummy"
    namespace["GPT_API_KEY"] = "audit-dummy"
    namespace["call_ark"] = lambda _system, _user: pick_raw
    namespace["call_gpt"] = lambda _system, _user: director_raw
    return namespace["handler"](input_value)


def run_migrated(input_value: dict[str, Any], reference_output: dict[str, Any]) -> dict[str, Any]:
    pick_raw, director_raw = _model_payloads(input_value, reference_output)
    return run_migrated_with_model_responses(input_value, pick_raw, director_raw)


def run_migrated_with_model_responses(
    input_value: dict[str, Any],
    pick_raw: str,
    director_raw: str,
) -> dict[str, Any]:
    return run_directors_v2_logic(
        input_value,
        pick_model=lambda _system, _user: pick_raw,
        director_model=lambda _system, _user: director_raw,
    )
