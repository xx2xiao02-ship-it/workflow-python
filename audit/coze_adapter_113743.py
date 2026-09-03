"""节点 113743 原始 Coze 代码与迁移实现的本地运行适配器。"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workflow_1256.prompt_generation import run_prompt_generation


SOURCE_PATH = Path(__file__).parent / "source" / "113743_coze_original.py"


def _load_original_main():
    namespace: dict[str, Any] = {"__name__": "coze_node_113743_original"}
    source = SOURCE_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(SOURCE_PATH), "exec"), namespace)
    return namespace["main"]


async def _run_original_async(params: Any) -> dict[str, Any]:
    original_main = _load_original_main()
    return await original_main(SimpleNamespace(params=copy.deepcopy(params)))


def run_original(params: Any) -> dict[str, Any]:
    return asyncio.run(_run_original_async(params))


def run_migrated(params: Any) -> dict[str, Any]:
    return run_prompt_generation(copy.deepcopy(params))


def run_both(params: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return run_original(params), run_migrated(params)
