"""1256 工作流“关键帧信息”节点的离线等价实现。

保持 CapCut Mate keyframes_infos 的源码行为：offsets/values 使用 ``|``
分隔字符串；输出按 segment_infos 顺序、再按 offset 顺序展开；offset 是
片段内相对微秒偏移；结果是 JSON 字符串。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def normalize_keyframe_value(
    ctype: str,
    value: float,
    width: int | None = None,
    height: int | None = None,
    *,
    assume_pixel: bool = False,
) -> float:
    if ctype == "KFTypePositionX" and width is not None and width > 0:
        if assume_pixel or abs(value) > 1.0:
            return value / width
    elif ctype == "KFTypePositionY" and height is not None and height > 0:
        if assume_pixel or abs(value) > 1.0:
            return value / height
    return value


def calculate_relative_time_offset(offset_percent: int, duration: int) -> int:
    return int((offset_percent / 100.0) * duration)


def keyframes_infos(
    ctype: str,
    offsets: str,
    values: str,
    segment_infos: list[Mapping[str, Any]],
    height: int | None = None,
    width: int | None = None,
) -> str:
    offset_list = [int(x) for x in offsets.split("|")]
    value_list = [float(x) for x in values.split("|")]

    if len(offset_list) != len(value_list):
        raise ValueError(
            f"offsets length ({len(offset_list)}) does not match values length ({len(value_list)})"
        )

    keyframes: list[dict[str, Any]] = []
    for segment_info in segment_infos:
        segment_id = segment_info["id"]
        start = segment_info["start"]
        end = segment_info["end"]
        duration = end - start
        for offset_percent, value in zip(offset_list, value_list):
            keyframes.append({
                "offset": calculate_relative_time_offset(offset_percent, duration),
                "property": ctype,
                "segment_id": segment_id,
                "value": normalize_keyframe_value(
                    ctype, value, width, height, assume_pixel=True
                ),
            })
    return json.dumps(keyframes, ensure_ascii=False)


def _parse_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    return json.loads(text) if text else value


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
        raise TypeError("keyframes_infos 输入必须是对象")
    return value


def run_keyframes_infos(params: Any) -> dict[str, str]:
    value = _resolve_params(params)
    kwargs: dict[str, Any] = {
        "ctype": value["ctype"],
        "offsets": value["offsets"],
        "values": value["values"],
        "segment_infos": value["segment_infos"],
    }
    for name in ("height", "width"):
        if name in value:
            kwargs[name] = value[name]
    return {"keyframes_infos": keyframes_infos(**kwargs)}


__all__ = ["calculate_relative_time_offset", "keyframes_infos", "run_keyframes_infos"]
