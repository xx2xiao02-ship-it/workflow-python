"""116930 原 add_videos 摘录与新实现的同输入运行适配器。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from workflow_1256.digital_human import normalize_video_infos

from .source.source_116930_add_videos_service_excerpt import parse_video_data


def _params(params: Any) -> Mapping[str, Any]:
    if not isinstance(params, Mapping):
        raise TypeError("审计输入必须是对象")
    return params


def run_original_normalization(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """运行只读源码摘录中可脱离草稿副作用的纯解析部分。"""

    value = _params(params)
    return parse_video_data(value["video_infos"])


def run_migrated_normalization(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = _params(params)
    return normalize_video_infos(value["video_infos"])


__all__ = ["run_migrated_normalization", "run_original_normalization"]

