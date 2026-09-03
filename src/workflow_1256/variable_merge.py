"""8364 node 121815 variable merge contract.

The YAML declares an ordered merge group for ``output_url`` with candidates
from nodes 137386, 1178381 and 1693143. This module preserves that order and
selects the first non-empty URL without inventing a value.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


BRANCH_NODE_IDS = ("137386", "1178381", "1693143")


class VariableMergeValidationError(ValueError):
    """Raised when node 121815 cannot produce its required output_url."""


def _unwrap(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _output_url(value: Any) -> str:
    value = _unwrap(value)
    if isinstance(value, Mapping):
        value = value.get("output_url")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _candidate_values(params: Any) -> list[Any]:
    params = _unwrap(params)
    if isinstance(params, Sequence) and not isinstance(params, (str, bytes, bytearray)):
        return list(params)
    if not isinstance(params, Mapping):
        raise VariableMergeValidationError("121815 输入必须是对象或候选数组")

    candidates = params.get("output_url_candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes, bytearray)):
        return list(candidates)

    branch_outputs = params.get("branch_outputs")
    if isinstance(branch_outputs, Mapping):
        return [branch_outputs.get(node_id) for node_id in BRANCH_NODE_IDS]

    return [params.get(node_id) for node_id in BRANCH_NODE_IDS]


def run_variable_merge(params: Any) -> dict[str, str]:
    """Merge the ordered InfiniteTalk query outputs into ``output_url``."""

    for candidate in _candidate_values(params):
        url = _output_url(candidate)
        if url:
            return {"output_url": url}
    raise VariableMergeValidationError(
        "121815 三个分支均未提供非空 output_url：137386、1178381、1693143"
    )


__all__ = ["BRANCH_NODE_IDS", "VariableMergeValidationError", "run_variable_merge"]
