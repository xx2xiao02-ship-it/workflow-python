from __future__ import annotations

from typing import Any

from .coze_adapter_effect_infos import run_original, run_migrated
from workflow_1256.effect_infos import run_effect_infos


def run_second(params: Any) -> dict[str, str]: return run_effect_infos(params)
def run_both_second(params: Any) -> tuple[dict[str, str], dict[str, str]]: return run_original(params), run_second(params)


__all__ = ["run_both_second", "run_second"]
