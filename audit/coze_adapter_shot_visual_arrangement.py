"""116616 原始 Coze 源码的离线运行适配器。

适配器只在内存中加载脱敏源码，并替换其 ``post_retry`` 函数。
因此原始代码中的 requests、API URL 和鉴权值都不会被调用或读取。
"""

from __future__ import annotations

import json
import sys
import types
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from workflow_1256.shot_visual_arrangement import run_shot_visual_arrangement


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_SOURCE = ROOT / "audit" / "source" / "116616_coze_original_sanitized.py"


class _Args:
    @classmethod
    def __class_getitem__(cls, _item: Any) -> type["_Args"]:
        return cls


class _Input:
    pass


class _Output:
    pass


@lru_cache(maxsize=1)
def load_original_namespace() -> dict[str, Any]:
    """编译脱敏原代码；不导入真实 Coze runtime。"""

    runtime_module = types.ModuleType("runtime")
    runtime_module.Args = _Args
    typings_module = types.ModuleType("typings")
    shot_module = types.ModuleType("typings.Shot_Visual_Arrangement")
    nested_module = types.ModuleType(
        "typings.Shot_Visual_Arrangement.Shot_Visual_Arrangement"
    )
    nested_module.Input = _Input
    nested_module.Output = _Output

    previous = {
        name: sys.modules.get(name)
        for name in (
            "runtime",
            "typings",
            "typings.Shot_Visual_Arrangement",
            "typings.Shot_Visual_Arrangement.Shot_Visual_Arrangement",
        )
    }
    sys.modules["runtime"] = runtime_module
    sys.modules["typings"] = typings_module
    sys.modules["typings.Shot_Visual_Arrangement"] = shot_module
    sys.modules["typings.Shot_Visual_Arrangement.Shot_Visual_Arrangement"] = nested_module
    namespace: dict[str, Any] = {"__file__": str(ORIGINAL_SOURCE), "__name__": "coze_116616_original"}
    try:
        source = ORIGINAL_SOURCE.read_text(encoding="utf-8")
        exec(compile(source, str(ORIGINAL_SOURCE), "exec"), namespace, namespace)
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    return namespace


def _as_model_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if "output_text" in value:
            return str(value["output_text"])
        choices = value.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], Mapping) else {}
            content = message.get("content") if isinstance(message, Mapping) else ""
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _response_data(raw: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": raw}}]}


def _offline_post_retry(
    namespace: Mapping[str, Any],
    gpt_raw: str,
    mini_raw_by_group: Mapping[str, str],
    mini_failure_groups: set[str] | None = None,
):
    mini_failure_groups = mini_failure_groups or set()

    def post_retry(_url: str, _headers: Mapping[str, str], payload: Mapping[str, Any], _timeout: float):
        model = payload.get("model")
        if model == namespace.get("GPT_MODEL"):
            return {"ok": True, "data": _response_data(_as_model_text(gpt_raw))}
        if model == namespace.get("MINI_MODEL"):
            sequence_text = ""
            inputs = payload.get("input")
            if isinstance(inputs, list) and len(inputs) > 1:
                user = inputs[1]
                if isinstance(user, Mapping):
                    content = user.get("content")
                    if isinstance(content, list) and content and isinstance(content[0], Mapping):
                        sequence_text = str(content[0].get("text", ""))
            try:
                sequence = json.loads(sequence_text).get("sequence", [])
                first_id = sequence[0].get("shot_id", "") if sequence else ""
                group_id = first_id.split("_", 1)[0] if first_id else ""
            except Exception:
                group_id = ""
            if group_id in mini_failure_groups:
                raise RuntimeError(f"synthetic mini failure: {group_id}")
            raw = mini_raw_by_group.get(group_id, "{\"materials\": []}")
            return {"ok": True, "data": _response_data(_as_model_text(raw))}
        return {"ok": False, "status": 500, "error": "offline model route not found"}

    return post_retry


def run_original_with_model_responses(
    input_value: Mapping[str, Any] | str,
    gpt_raw: Any,
    mini_raw_by_group: Mapping[str, Any],
    *,
    mini_failure_groups: set[str] | None = None,
) -> dict[str, Any]:
    """以固定模型文本运行原源码；整个过程不触网。"""

    namespace = load_original_namespace()
    original_post_retry = namespace["post_retry"]
    namespace["post_retry"] = _offline_post_retry(
        namespace, gpt_raw, mini_raw_by_group, mini_failure_groups
    )
    try:
        return namespace["handler"](SimpleNamespace(input=input_value))
    finally:
        namespace["post_retry"] = original_post_retry


def run_migrated_with_model_responses(
    input_value: Mapping[str, Any] | str,
    gpt_raw: Any,
    mini_raw_by_group: Mapping[str, Any],
    *,
    mini_failure_groups: set[str] | None = None,
) -> dict[str, Any]:
    """以完全相同的固定模型文本运行新实现。"""

    def gpt_transport(_payload: Mapping[str, Any]) -> str:
        return gpt_raw

    def mini_transport(payload: Mapping[str, Any]) -> str:
        inputs = payload.get("input", [])
        sequence_text = inputs[1]["content"][0]["text"]
        sequence = json.loads(sequence_text).get("sequence", [])
        first_id = sequence[0].get("shot_id", "") if sequence else ""
        group_id = first_id.split("_", 1)[0] if first_id else ""
        if group_id in (mini_failure_groups or set()):
            raise RuntimeError(f"synthetic mini failure: {group_id}")
        return mini_raw_by_group.get(group_id, "{\"materials\": []}")

    return run_shot_visual_arrangement(
        input_value,
        gpt_transport=gpt_transport,
        mini_transport=mini_transport,
    )


def run_original_and_migrated(
    input_value: Mapping[str, Any] | str,
    gpt_raw: Any,
    mini_raw_by_group: Mapping[str, Any],
    *,
    mini_failure_groups: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        run_original_with_model_responses(
            input_value, gpt_raw, mini_raw_by_group,
            mini_failure_groups=mini_failure_groups,
        ),
        run_migrated_with_model_responses(
            input_value, gpt_raw, mini_raw_by_group,
            mini_failure_groups=mini_failure_groups,
        ),
    )


__all__ = [
    "ORIGINAL_SOURCE",
    "load_original_namespace",
    "run_original_with_model_responses",
    "run_migrated_with_model_responses",
    "run_original_and_migrated",
]
