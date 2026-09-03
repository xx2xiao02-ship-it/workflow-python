"""1256 工作流“背景音乐*”(110576)的本地 Python 契约实现。

该节点与“解说*”共用 capcut-mate ``add_audios`` 接口规则，但保留独立
的节点入口，方便记录不同的上游来源、样本和验收状态。真实草稿写入
必须通过显式注入的 executor 完成；默认执行不会访问外部服务。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .add_audios import (
    AddAudiosTransportRequired,
    AudioInfoValidationError,
    normalize_audio_infos,
    run_add_audios,
)


def run_background_music(
    params: Any,
    *,
    executor: Callable[[str, str, list[dict[str, Any]]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """执行节点 110576，保持原始输出字段和顺序。"""

    return run_add_audios(params, executor=executor)


def main(
    params: Any,
    *,
    executor: Callable[[str, str, list[dict[str, Any]]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return run_background_music(params, executor=executor)


__all__ = [
    "AddAudiosTransportRequired",
    "AudioInfoValidationError",
    "main",
    "normalize_audio_infos",
    "run_background_music",
]
