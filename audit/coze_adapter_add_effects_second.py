from __future__ import annotations

from typing import Any

from workflow_1256.add_effects import run_add_effects
from .coze_adapter_add_effects import run_original_parser, run_migrated_parser


def run_contract_second(params: Any, executor: Any) -> dict[str, Any]: return run_add_effects(params, executor=executor)
def run_parser_both(effect_infos: str) -> tuple[Any, Any]: return run_original_parser(effect_infos), run_migrated_parser(effect_infos)


__all__ = ["run_contract_second", "run_parser_both"]
