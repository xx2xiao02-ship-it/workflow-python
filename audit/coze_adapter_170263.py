"""节点 170263 原始 Coze 代码与新实现的本地运行适配器。"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workflow_1256.end_frame_extension import run_end_frame_extension


SOURCE_PATH = Path(__file__).parent / "source" / "170263_coze_original.py"


def _load_original_main():
    namespace: dict[str, Any] = {"__name__": "coze_node_170263_original"}
    source = SOURCE_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(SOURCE_PATH), "exec"), namespace)
    return namespace["main"]


async def _run_original_async(params: Any) -> dict[str, Any]:
    original_main = _load_original_main()
    return await original_main(SimpleNamespace(params=copy.deepcopy(params)))


def run_original(params: Any) -> dict[str, Any]:
    return asyncio.run(_run_original_async(params))


def run_migrated(params: Any) -> dict[str, Any]:
    return run_end_frame_extension(copy.deepcopy(params))


def run_both(params: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return run_original(params), run_migrated(params)
