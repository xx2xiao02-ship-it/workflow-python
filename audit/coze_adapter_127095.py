"""Run the read-only Coze source and the Python port with identical inputs."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workflow_1256.timeline_planning import run_timeline_planning


SOURCE_PATH = Path(__file__).parent / "source" / "127095_coze_original.py"


def _load_original_main():
    namespace: dict[str, Any] = {
        "Args": Any,
        "Output": dict,
        "__name__": "coze_node_127095_original",
    }
    source = SOURCE_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(SOURCE_PATH), "exec"), namespace)
    return namespace["main"]


async def _run_original_async(params: Any) -> dict[str, Any]:
    original_main = _load_original_main()
    args = SimpleNamespace(params=copy.deepcopy(params))
    return await original_main(args)


def run_original(params: Any) -> dict[str, Any]:
    return asyncio.run(_run_original_async(params))


def run_migrated(params: Any) -> dict[str, Any]:
    return run_timeline_planning(copy.deepcopy(params))


def run_both(params: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return run_original(params), run_migrated(params)
