"""110697 外层批处理的脱敏契约适配器。

外层 Coze 批处理容器没有独立 Python 源码；这里的“Coze”函数只表达
页面已确认的投影规则，不冒充原始实现：逐项取 video_query 的
public_video_url，并保持批处理项顺序。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from workflow_1256.batch import run_video_generation_batch


def run_observed_coze_projection(query_outputs: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """按真实页面登记的外层输出映射生成观察基线。"""

    return {
        "public_video_url_list": [
            output.get("public_video_url") or "" for output in query_outputs
        ]
    }


def run_migrated(
    params: Any,
    *,
    generate_runner,
    query_runner,
) -> dict[str, list[str]]:
    return run_video_generation_batch(
        params,
        generate_runner=generate_runner,
        query_runner=query_runner,
    )
