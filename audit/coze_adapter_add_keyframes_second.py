"""节点 1962357 的原始解析器与 Python 实现适配器。"""

from __future__ import annotations

import copy
from typing import Any

from .coze_adapter_add_keyframes import run_original_parser, run_migrated_parser
from workflow_1256.add_keyframes_second import run_add_keyframes


def run_original(params: Any) -> list[dict[str, Any]]:
    value = params
    if isinstance(value, str):
        import json
        value = json.loads(value)
    if isinstance(value, dict) and isinstance(value.get("input"), dict):
        value = value["input"]
    return run_original_parser(value["keyframes"])


def run_migrated(params: Any) -> list[dict[str, Any]]:
    value = params
    if isinstance(value, str):
        import json
        value = json.loads(value)
    if isinstance(value, dict) and isinstance(value.get("input"), dict):
        value = value["input"]
    return run_migrated_parser(value["keyframes"])


def run_contract(params: Any, executor: Any) -> dict[str, str]:
    return run_add_keyframes(copy.deepcopy(params), executor=executor)


__all__ = ["run_contract", "run_migrated", "run_original"]
