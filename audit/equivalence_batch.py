"""110697 观察基线与 Python 实现的逐字段对照。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from audit.coze_adapter_batch import run_observed_coze_projection, run_migrated


def compare_batch_outputs(
    observed_query_outputs: Sequence[Mapping[str, Any]],
    migrated_output: Mapping[str, Any],
) -> list[dict[str, Any]]:
    expected = run_observed_coze_projection(observed_query_outputs)
    differences: list[dict[str, Any]] = []
    if set(expected) != set(migrated_output):
        differences.append({"path": "$", "expected_keys": list(expected), "actual_keys": list(migrated_output)})
        return differences
    expected_urls = expected["public_video_url_list"]
    actual_urls = migrated_output.get("public_video_url_list")
    if not isinstance(actual_urls, list):
        return [{"path": "$.public_video_url_list", "expected_type": "Array<String>", "actual": actual_urls}]
    if len(expected_urls) != len(actual_urls):
        differences.append({"path": "$.public_video_url_list.length", "expected": len(expected_urls), "actual": len(actual_urls)})
    for index, (expected_url, actual_url) in enumerate(zip(expected_urls, actual_urls)):
        if type(expected_url) is not type(actual_url) or expected_url != actual_url:
            differences.append({
                "path": f"$.public_video_url_list[{index}]",
                "expected": expected_url,
                "actual": actual_url,
            })
    return differences


__all__ = ["compare_batch_outputs", "run_migrated", "run_observed_coze_projection"]
