"""节点 1904923 的原始/迁移统一运行适配器。"""

from __future__ import annotations

import copy
from typing import Any

from .coze_adapter_keyframes_infos import run_original as _run_original
from .coze_adapter_keyframes_infos import run_migrated as _run_migrated


def run_original(params: Any) -> dict[str, str]:
    return _run_original(copy.deepcopy(params))


def run_migrated(params: Any) -> dict[str, str]:
    from workflow_1256.keyframes_infos_second import run_keyframes_infos

    return run_keyframes_infos(copy.deepcopy(params))


def run_both(params: Any) -> tuple[dict[str, str], dict[str, str]]:
    return run_original(params), run_migrated(params)


__all__ = ["run_both", "run_migrated", "run_original"]
