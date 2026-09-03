"""video_infos 原始源码与新实现的离线运行适配器。"""

from __future__ import annotations

import copy
import json
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from workflow_1256.video_infos import run_video_infos


SOURCE_PATH = Path(__file__).parent / "source" / "video_infos_capcut_mate_original.py"
_MISSING = object()


class _NoOpLogger:
    def info(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _load_original_function():
    """在不修改原文件的前提下，为原源码注入其缺失的日志依赖。"""

    source = SOURCE_PATH.read_text(encoding="utf-8")
    namespace: dict[str, Any] = {"__name__": "coze_video_infos_original"}
    fake_src = types.ModuleType("src")
    fake_src.__path__ = []  # type: ignore[attr-defined]
    fake_utils = types.ModuleType("src.utils")
    fake_utils.__path__ = []  # type: ignore[attr-defined]
    fake_logger = types.ModuleType("src.utils.logger")
    fake_logger.logger = _NoOpLogger()
    modules = {
        "src": fake_src,
        "src.utils": fake_utils,
        "src.utils.logger": fake_logger,
    }
    previous = {name: sys.modules.get(name, _MISSING) for name in modules}
    sys.modules.update(modules)
    try:
        exec(compile(source, str(SOURCE_PATH), "exec"), namespace)
    finally:
        for name, value in previous.items():
            if value is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    return namespace["video_infos"]


def _parse_request(params: Any) -> Mapping[str, Any]:
    value = getattr(params, "input", params)
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, Mapping) and isinstance(value.get("input"), Mapping):
        value = value["input"]
    if isinstance(value, Mapping) and isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value, Mapping) and isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    if not isinstance(value, Mapping):
        raise TypeError("video_infos 输入必须是对象")
    return value


def _call_kwargs(params: Any) -> dict[str, Any]:
    value = _parse_request(params)
    kwargs: dict[str, Any] = {
        "video_urls": value["video_urls"],
        "timelines": value["timelines"],
    }
    for name in (
        "height",
        "width",
        "mask",
        "transition",
        "transition_duration",
        "volume",
    ):
        if name in value:
            kwargs[name] = value[name]
    return kwargs


def run_original(params: Any) -> dict[str, str]:
    function = _load_original_function()
    return {"infos": function(**_call_kwargs(copy.deepcopy(params)))}


def run_migrated(params: Any) -> dict[str, str]:
    return run_video_infos(copy.deepcopy(params))


def run_both(params: Any) -> tuple[dict[str, str], dict[str, str]]:
    return run_original(params), run_migrated(params)


__all__ = ["run_both", "run_migrated", "run_original"]
