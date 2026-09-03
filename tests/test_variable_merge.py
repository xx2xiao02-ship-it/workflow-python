from __future__ import annotations

import json

import pytest

from workflow_1256.variable_merge import VariableMergeValidationError, run_variable_merge


def test_variable_merge_uses_yaml_order_and_skips_empty_branch() -> None:
    assert run_variable_merge({
        "branch_outputs": {
            "137386": {"output_url": ""},
            "1178381": {"output_url": "https://example.invalid/host.mp4"},
            "1693143": {"output_url": "https://example.invalid/later.mp4"},
        }
    }) == {"output_url": "https://example.invalid/host.mp4"}


def test_variable_merge_accepts_json_wrapped_candidates() -> None:
    payload = json.dumps({"output_url_candidates": [None, {"output_url": "u"}]})
    assert run_variable_merge(payload) == {"output_url": "u"}


def test_variable_merge_does_not_fabricate_url() -> None:
    with pytest.raises(VariableMergeValidationError):
        run_variable_merge({"branch_outputs": {"137386": {}, "1178381": {}, "1693143": {}}})


def test_variable_merge_is_available_from_contract_runner() -> None:
    from pathlib import Path
    import sys

    scripts = Path(__file__).parents[1] / "skills" / "daily-update-te" / "scripts"
    sys.path.insert(0, str(scripts))
    from run_node_contract import NODE_NAMES, _load_runner

    assert "variable_merge" in NODE_NAMES
    assert _load_runner("variable_merge")({"137386": {"output_url": "u"}}) == {"output_url": "u"}
