"""节点 1904923 的逐字段旧新结果对照。"""

from __future__ import annotations

from typing import Any

from .equivalence_keyframes_infos import (
    _capture,
    _compare,
    _parsed_keyframes,
    output_shape,
)
from .coze_adapter_keyframes_infos_second import run_migrated, run_original
from .real_fixture_keyframes_infos_second import load_real_input


def compare_case(params: Any) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    original = _capture(run_original, params)
    migrated = _capture(run_migrated, params)
    differences: list[dict[str, Any]] = []
    if original.get("kind") != migrated.get("kind"):
        differences.append({"path": "$", "kind": "outcome_kind", "original": original, "migrated": migrated})
    elif original.get("kind") == "exception":
        for key in ("type", "message"):
            if original.get(key) != migrated.get(key):
                differences.append({"path": f"$.exception.{key}", "kind": "value", "original": original.get(key), "migrated": migrated.get(key)})
    else:
        differences.extend(_compare(original["value"], migrated["value"]))
        left = _parsed_keyframes(original)
        right = _parsed_keyframes(migrated)
        if left is None or right is None:
            differences.append({"path": "$.keyframes_infos", "kind": "invalid_json_string"})
        else:
            differences.extend(_compare(left, right, "$.keyframes_infos.json"))
    return original, migrated, differences


def run_real_structural_audit() -> dict[str, Any]:
    params = load_real_input()
    original, migrated, differences = compare_case(params)
    return {
        "input_segment_count": len(params["segment_infos"]),
        "original_keyframe_count": len(_parsed_keyframes(original) or []),
        "migrated_keyframe_count": len(_parsed_keyframes(migrated) or []),
        "differences": differences,
        "passed": not differences,
        "original_shape": output_shape(original),
        "migrated_shape": output_shape(migrated),
    }


__all__ = ["compare_case", "run_real_structural_audit"]
