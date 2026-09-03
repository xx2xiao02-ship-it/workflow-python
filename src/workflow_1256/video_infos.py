"""1256 工作流“ AIGC动画”节点的离线契约实现。

该节点对应 capcut-mate 的 ``video_infos`` 接口。原始服务实现只做三件事：
按较短数组截断、计算每段 ``end - start``、按固定字段顺序序列化为 JSON
字符串。这里保留这些行为，不访问远程剪映服务。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def video_infos(
    video_urls: list[str],
    timelines: list[Mapping[str, Any]],
    height: int | None = None,
    width: int | None = None,
    mask: str | None = None,
    transition: str | None = None,
    transition_duration: int | None = None,
    volume: float | None = 1.0,
) -> str:
    """保持 capcut-mate ``video_infos`` 原始实现的输出行为。"""

    if len(video_urls) != len(timelines):
        min_len = min(len(video_urls), len(timelines))
        video_urls = video_urls[:min_len]
        timelines = timelines[:min_len]

    infos: list[dict[str, Any]] = []
    for video_url, timeline in zip(video_urls, timelines):
        start = timeline["start"]
        end = timeline["end"]
        duration = end - start

        info: dict[str, Any] = {
            "video_url": video_url,
            "start": start,
            "end": end,
            "duration": duration,
        }

        if width is not None:
            info["width"] = width
        if height is not None:
            info["height"] = height
        if mask is not None:
            info["mask"] = mask
        if transition is not None:
            info["transition"] = transition
        if transition_duration is not None:
            info["transition_duration"] = transition_duration
        if volume is not None:
            info["volume"] = volume

        infos.append(info)

    return json.dumps(infos, ensure_ascii=False)


def _parse_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    return json.loads(text)


def _resolve_params(params: Any) -> Mapping[str, Any]:
    value = getattr(params, "input", params)
    value = _parse_json_string(value)
    if isinstance(value, Mapping) and isinstance(value.get("input"), Mapping):
        value = value["input"]
    if isinstance(value, Mapping) and isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value, Mapping) and isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    if not isinstance(value, Mapping):
        raise TypeError("video_infos 输入必须是对象")
    return value


def run_video_infos(params: Any) -> dict[str, str]:
    """运行工作流节点边界，返回 Coze 节点输出字段 ``infos``。"""

    value = _resolve_params(params)
    kwargs: dict[str, Any] = {
        "video_urls": value["video_urls"],
        "timelines": value["timelines"],
    }
    for name in (
        "height",
        "width",
        "mask",
        "transition",
        "transition_duration",
        "volume",
    ):
        if name in value:
            kwargs[name] = value[name]
    return {"infos": video_infos(**kwargs)}


__all__ = ["run_video_infos", "video_infos"]
