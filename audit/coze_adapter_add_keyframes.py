"""添加关键帧节点的原始解析器与本地契约适配器。"""

from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path
from typing import Any

from workflow_1256.add_keyframes import parse_keyframes_data, run_add_keyframes


ROOT = Path(__file__).parents[1]
SOURCE_PATH = ROOT / "audit" / "source" / "add_keyframes_capcut_mate_original.py"
EXCEPTIONS_PATH = ROOT / "audit" / "source" / "exceptions_capcut_mate_original.py"


class _NoOpLogger:
    def info(self, *_args: Any, **_kwargs: Any) -> None: return None
    def warning(self, *_args: Any, **_kwargs: Any) -> None: return None
    def error(self, *_args: Any, **_kwargs: Any) -> None: return None


def _load_original_parser() -> Any:
    fake_src = types.ModuleType("src")
    fake_src.__path__ = []  # type: ignore[attr-defined]
    fake_utils = types.ModuleType("src.utils")
    fake_utils.__path__ = []  # type: ignore[attr-defined]
    fake_logger = types.ModuleType("src.utils.logger")
    fake_logger.logger = _NoOpLogger()
    fake_pydraft = types.ModuleType("src.pyJianYingDraft")
    fake_pydraft.ScriptFile = type("ScriptFile", (), {})
    fake_keyframe = types.ModuleType("src.pyJianYingDraft.keyframe")
    fake_keyframe.KeyframeProperty = type("KeyframeProperty", (), {})
    fake_segment = types.ModuleType("src.pyJianYingDraft.segment")
    fake_segment.VisualSegment = type("VisualSegment", (), {})
    fake_cache = types.ModuleType("src.utils.draft_cache")
    fake_cache.DRAFT_CACHE = {}
    exception_namespace: dict[str, Any] = {"__name__": "capcut_mate_exceptions_original"}
    exec(compile(EXCEPTIONS_PATH.read_text(encoding="utf-8"), str(EXCEPTIONS_PATH), "exec"), exception_namespace)
    fake_exceptions = types.ModuleType("exceptions")
    fake_exceptions.CustomException = exception_namespace["CustomException"]
    fake_exceptions.CustomError = exception_namespace["CustomError"]
    fake_helper = types.ModuleType("src.utils.helper")
    fake_helper.get_url_param = lambda *_args, **_kwargs: ""
    fake_utils.helper = fake_helper
    fake_lock = types.ModuleType("src.utils.draft_lock_manager")
    fake_lock.DraftLockManager = type("DraftLockManager", (), {})
    fake_keyframe_value = types.ModuleType("src.utils.keyframe_value")
    fake_keyframe_value.normalize_keyframe_value = lambda ctype, value, **_kwargs: value

    modules = {
        "src": fake_src,
        "src.utils": fake_utils,
        "src.utils.logger": fake_logger,
        "src.utils.helper": fake_helper,
        "src.pyJianYingDraft": fake_pydraft,
        "src.pyJianYingDraft.keyframe": fake_keyframe,
        "src.pyJianYingDraft.segment": fake_segment,
        "src.utils.draft_cache": fake_cache,
        "src.utils.draft_lock_manager": fake_lock,
        "src.utils.keyframe_value": fake_keyframe_value,
        "exceptions": fake_exceptions,
    }
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in modules}
    sys.modules.update(modules)
    try:
        namespace: dict[str, Any] = {"__name__": "coze_add_keyframes_original"}
        exec(compile(SOURCE_PATH.read_text(encoding="utf-8"), str(SOURCE_PATH), "exec"), namespace)
        return namespace["parse_keyframes_data"]
    finally:
        for name, value in previous.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def run_original_parser(keyframes: str) -> list[dict[str, Any]]:
    return _load_original_parser()(keyframes)


def run_migrated_parser(keyframes: str) -> list[dict[str, Any]]:
    return parse_keyframes_data(keyframes)


def run_original_contract(params: Any, executor: Any) -> dict[str, str]:
    """离线模拟原服务的成功返回，仅用于验证 Coze 输出投影。"""
    value = params
    if isinstance(value, str): value = json.loads(value)
    if isinstance(value, dict) and isinstance(value.get("input"), dict): value = value["input"]
    parsed = run_original_parser(value["keyframes"])
    if not parsed: raise ValueError("无效的关键帧信息")
    return {"draft_url": executor(value["draft_url"], value["keyframes"])["draft_url"]}


def run_migrated_contract(params: Any, executor: Any) -> dict[str, str]:
    return run_add_keyframes(copy.deepcopy(params), executor=executor)


__all__ = ["run_migrated_contract", "run_migrated_parser", "run_original_contract", "run_original_parser"]
