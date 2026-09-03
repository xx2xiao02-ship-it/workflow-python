"""Real Coze fixture audit for “时长计算时间线规划” item 1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .coze_adapter_127095 import run_both
from .equivalence import compare_outputs
from .equivalence_127095 import compare_captured_outputs


RUN_ID = "7668196429877870619"
ITEM = "1"
ITEMS = tuple(str(index) for index in range(1, 10))


def _fixture_path(kind: str) -> Path:
    return (
        Path(__file__).parents[1]
        / "samples"
        / "real"
        / f"run-{RUN_ID}-127095-item-{ITEM}-{kind}.json"
    )


def _item_fixture_path(item: str, kind: str) -> Path:
    return (
        Path(__file__).parents[1]
        / "samples"
        / "real"
        / f"run-{RUN_ID}-127095-item-{item}-{kind}.json"
    )


def load_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    input_data = json.loads(_fixture_path("input").read_text(encoding="utf-8"))
    coze_output = json.loads(_fixture_path("coze-output").read_text(encoding="utf-8"))
    return input_data, coze_output


def run_real_fixture_audit() -> dict[str, Any]:
    input_data, coze_output = load_fixture()
    original, migrated = run_both(input_data)
    return {
        "run_id": RUN_ID,
        "item": ITEM,
        "original_vs_migrated": compare_outputs(original, migrated),
        "coze_vs_original": compare_captured_outputs(coze_output, original),
        "coze_vs_migrated": compare_captured_outputs(coze_output, migrated),
        "original": original,
        "migrated": migrated,
        "coze_output": coze_output,
    }


def run_real_item_fixture_audit(item: str) -> dict[str, Any]:
    input_data = json.loads(_item_fixture_path(item, "input").read_text(encoding="utf-8"))
    coze_output = json.loads(_item_fixture_path(item, "coze-output").read_text(encoding="utf-8"))
    original, migrated = run_both(input_data)
    return {
        "run_id": RUN_ID,
        "item": item,
        "original_vs_migrated": compare_outputs(original, migrated),
        "coze_vs_original": compare_captured_outputs(coze_output, original),
        "coze_vs_migrated": compare_captured_outputs(coze_output, migrated),
        "original": original,
        "migrated": migrated,
        "coze_output": coze_output,
    }


def run_real_batch_fixture_audit() -> dict[str, dict[str, Any]]:
    """Run the same three-way audit for all nine captured batch items."""

    return {item: run_real_item_fixture_audit(item) for item in ITEMS}
