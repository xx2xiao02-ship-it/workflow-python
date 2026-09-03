from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

from workflow_1256.add_effects import parse_effects_data, run_add_effects

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "audit" / "source" / "add_effects_capcut_mate_original.py"


def _original_parser() -> Any:
    namespace = {"__name__":"add_effects_original"}
    exec(compile(SOURCE.read_text(encoding="utf-8"), str(SOURCE), "exec"), namespace)
    return namespace["parse_effects_data"]


def run_original_parser(effect_infos: str) -> Any: return _original_parser()(effect_infos)
def run_migrated_parser(effect_infos: str) -> Any: return parse_effects_data(effect_infos)
def run_contract(params: Any, executor: Any) -> dict[str, Any]: return run_add_effects(params, executor=executor)


__all__ = ["run_contract", "run_migrated_parser", "run_original_parser"]
