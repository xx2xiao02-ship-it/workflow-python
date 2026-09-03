"""运行 CapCut Mate 原始 keyframes_infos 与本地实现的统一适配器。"""

from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path
from typing import Any

from workflow_1256.keyframes_infos import run_keyframes_infos


ROOT = Path(__file__).parents[1]
SOURCE_PATH = ROOT / "audit" / "source" / "keyframes_infos_capcut_mate_original.py"
UTILITY_PATH = ROOT / "audit" / "source" / "keyframe_value_capcut_mate_original.py"


class _NoOpLogger:
    def info(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _load_original_function() -> Any:
    utility_namespace: dict[str, Any] = {"__name__": "coze_keyframe_value_original"}
    exec(compile(UTILITY_PATH.read_text(encoding="utf-8"), str(UTILITY_PATH), "exec"), utility_namespace)

    fake_src = types.ModuleType("src")
    fake_src.__path__ = []  # type: ignore[attr-defined]
    fake_utils = types.ModuleType("src.utils")
    fake_utils.__path__ = []  # type: ignore[attr-defined]
    fake_logger = types.ModuleType("src.utils.logger")
    fake_logger.logger = _NoOpLogger()
    fake_keyframe_value = types.ModuleType("src.utils.keyframe_value")
    fake_keyframe_value.normalize_keyframe_value = utility_namespace["normalize_keyframe_value"]

    modules = {
        "src": fake_src,
        "src.utils": fake_utils,
        "src.utils.logger": fake_logger,
        "src.utils.keyframe_value": fake_keyframe_value,
    }
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in modules}
    sys.modules.update(modules)
    try:
        namespace: dict[str, Any] = {"__name__": "coze_keyframes_infos_original"}
        exec(compile(SOURCE_PATH.read_text(encoding="utf-8"), str(SOURCE_PATH), "exec"), namespace)
        return namespace["keyframes_infos"]
    finally:
        for name, value in previous.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _parse_request(params: Any) -> dict[str, Any]:
    value = getattr(params, "input", params)
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and isinstance(value.get("input"), dict):
        value = value["input"]
    if isinstance(value, dict) and isinstance(value.get("params"), dict):
        value = value["params"]
    if isinstance(value, dict) and isinstance(value.get("_input"), dict):
        value = value["_input"]
    if not isinstance(value, dict):
        raise TypeError("keyframes_infos 输入必须是对象")
    return value


def _call_kwargs(params: Any) -> dict[str, Any]:
    value = _parse_request(params)
    kwargs: dict[str, Any] = {
        "ctype": value["ctype"],
        "offsets": value["offsets"],
        "values": value["values"],
        "segment_infos": value["segment_infos"],
    }
    for name in ("height", "width"):
        if name in value:
            kwargs[name] = value[name]
    return kwargs


def run_original(params: Any) -> dict[str, str]:
    function = _load_original_function()
    return {"keyframes_infos": function(**copy.deepcopy(_call_kwargs(params)))}


def run_migrated(params: Any) -> dict[str, str]:
    return run_keyframes_infos(copy.deepcopy(params))


def run_both(params: Any) -> tuple[dict[str, str], dict[str, str]]:
    return run_original(params), run_migrated(params)


__all__ = ["run_both", "run_migrated", "run_original"]
