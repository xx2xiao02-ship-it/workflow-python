"""特效信息节点 197721 的原始/迁移运行适配器。"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

from workflow_1256.effect_infos import run_effect_infos

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "audit" / "source" / "effect_infos_capcut_mate_original.py"


def _load_original() -> Any:
    fake_src = types.ModuleType("src")
    fake_src.__path__ = []  # type: ignore[attr-defined]
    fake_utils = types.ModuleType("src.utils")
    fake_utils.__path__ = []  # type: ignore[attr-defined]
    fake_logger = types.ModuleType("src.utils.logger")
    fake_logger.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    modules = {"src": fake_src, "src.utils": fake_utils, "src.utils.logger": fake_logger}
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in modules}
    sys.modules.update(modules)
    try:
        namespace = {"__name__": "effect_infos_original"}
        exec(compile(SOURCE.read_text(encoding="utf-8"), str(SOURCE), "exec"), namespace)
        return namespace["effect_infos"]
    finally:
        for name, value in previous.items():
            if value is missing: sys.modules.pop(name, None)
            else: sys.modules[name] = value


def run_original(params: Any) -> dict[str, str]:
    value = json.loads(params) if isinstance(params, str) else params
    if isinstance(value, dict) and isinstance(value.get("input"), dict): value = value["input"]
    return {"infos": _load_original()(value["effects"], value["timelines"])}


def run_migrated(params: Any) -> dict[str, str]:
    return run_effect_infos(params)


def run_both(params: Any) -> tuple[dict[str, str], dict[str, str]]:
    return run_original(params), run_migrated(params)


__all__ = ["run_both", "run_migrated", "run_original"]
