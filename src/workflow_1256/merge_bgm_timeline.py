"""1256 节点 165818 ``merge_bgm_timeline`` 的严格契约适配器。

原节点是独立的 BGM 融合插件。当前仓库没有它的源码或公开接口，因此本模块
只负责输入/输出契约和显式 transport 注入，不实现下载、裁剪、淡入淡出或混音。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any


class BgmMergeValidationError(ValueError):
    """165818 输入或输出不符合原节点契约。"""


class BgmMergeTransportRequired(RuntimeError):
    """未注入 165818 的真实融合 transport。"""


def _resolve(params: Any) -> Mapping[str, Any]:
    value = params
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BgmMergeValidationError("merge_bgm_timeline 输入 JSON 无效") from exc
    for key in ("input", "params", "_input"):
        if isinstance(value, Mapping) and isinstance(value.get(key), Mapping):
            value = value[key]
    if not isinstance(value, Mapping):
        raise BgmMergeValidationError("merge_bgm_timeline 输入必须是对象")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BgmMergeValidationError(f"{field} JSON 无效") from exc
    if not isinstance(value, list):
        raise BgmMergeValidationError(f"{field} 必须是数组")
    return list(value)


def build_request(params: Any) -> dict[str, Any]:
    value = _resolve(params)
    audio_urls = _list(value.get("audio_urls"), "audio_urls")
    timelines = _list(value.get("timelines"), "timelines")
    transition_schemes = _list(value.get("transition_schemes"), "transition_schemes")
    if not all(
        isinstance(item, str)
        and item.strip()
        and item.startswith(("http://", "https://"))
        for item in audio_urls
    ):
        raise BgmMergeValidationError("audio_urls 必须是 http(s) 字符串数组")
    for index, timeline in enumerate(timelines):
        if not isinstance(timeline, Mapping):
            raise BgmMergeValidationError(f"timelines[{index}] 必须是对象")
        start = timeline.get("start")
        end = timeline.get("end")
        if isinstance(start, bool) or not isinstance(start, int):
            raise BgmMergeValidationError(f"timelines[{index}].start 必须是微秒整数")
        if isinstance(end, bool) or not isinstance(end, int):
            raise BgmMergeValidationError(f"timelines[{index}].end 必须是微秒整数")
        if start < 0 or end <= start:
            raise BgmMergeValidationError(f"timelines[{index}] 时间范围无效")
    if not all(isinstance(item, str) for item in transition_schemes):
        raise BgmMergeValidationError("transition_schemes 必须是字符串数组")
    if len(audio_urls) != len(timelines):
        raise BgmMergeValidationError("audio_urls 与 timelines 数量必须一致")
    if len(transition_schemes) != max(0, len(timelines) - 1):
        raise BgmMergeValidationError(
            "transition_schemes 数量必须等于 timelines 数量减一"
        )
    return {
        "audio_urls": audio_urls,
        "timelines": [dict(item) for item in timelines],
        "transition_schemes": transition_schemes,
    }


def normalize_response(response: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise BgmMergeValidationError("165818 输出必须是对象")
    audio_url = response.get("audio_url")
    audio_url_list = response.get("audio_url_list")
    duration = response.get("duration")
    if not isinstance(audio_url, str) or not audio_url.strip():
        raise BgmMergeValidationError("165818.audio_url 必须是非空字符串")
    if not isinstance(audio_url_list, list) or not all(
        isinstance(item, str) and item.strip() for item in audio_url_list
    ):
        raise BgmMergeValidationError("165818.audio_url_list 必须是字符串数组")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise BgmMergeValidationError("165818.duration 必须是数字")
    if duration < 0:
        raise BgmMergeValidationError("165818.duration 不能为负数")
    return {
        "audio_url": audio_url,
        "audio_url_list": list(audio_url_list),
        "duration": duration,
    }


def run_bgm_merge(
    params: Any,
    *,
    transport: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    request = build_request(params)
    if transport is None:
        raise BgmMergeTransportRequired(
            "未配置 165818 merge_bgm_timeline 真实融合 transport；不能用 CapCut audio_timelines 替代"
        )
    response = transport(request)
    return normalize_response(response)


__all__ = [
    "BgmMergeTransportRequired",
    "BgmMergeValidationError",
    "build_request",
    "normalize_response",
    "run_bgm_merge",
]
