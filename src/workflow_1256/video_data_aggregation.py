"""8364 节点 167309「视频数据汇总」的 Python 移植。

原节点是 JavaScript 纯逻辑：先用原始视频片段建立时间片，再按输入顺序
让新视频片段覆盖原片段，最后合并相邻且来源、URL、来源索引都相同的片段。
时间单位保持微秒，不调用网络。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class VideoDataAggregationValidationError(ValueError):
    """输入不符合 167309 原始代码校验。"""


def _parse(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise VideoDataAggregationValidationError("视频数据汇总输入 JSON 无效") from exc


def _resolve(params: Any) -> Mapping[str, Any]:
    value = _parse(params)
    if isinstance(value, Mapping) and isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value, Mapping) and isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    if not isinstance(value, Mapping):
        raise VideoDataAggregationValidationError("视频数据汇总输入必须是对象")
    return value


def _number(value: Any, name: str) -> int:
    if value is None:
        number = 0
    elif isinstance(value, bool):
        number = 1 if value else 0
    else:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise VideoDataAggregationValidationError(f"{name} 必须是安全整数，当前值为 {value}") from exc
    if not float(number).is_integer() or abs(int(number)) > 9_007_199_254_740_991:
        raise VideoDataAggregationValidationError(f"{name} 必须是安全整数，当前值为 {value}")
    return int(number)


def _validate_timeline(value: Any, name: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise VideoDataAggregationValidationError(f"{name} 必须是包含 start、end 的对象")
    start = _number(value.get("start"), f"{name}.start")
    end = _number(value.get("end"), f"{name}.end")
    if end <= start:
        raise VideoDataAggregationValidationError(
            f"{name} 时间无效：end 必须大于 start，当前为 {start} → {end}"
        )
    return {"start": start, "end": end}


def _validate_inputs(value: Mapping[str, Any]) -> tuple[list[Any], list[Any], list[str], list[Any]]:
    url_list = value.get("url_list")
    new_timelines = value.get("new_timelines")
    public_urls = value.get("public_video_url_list")
    timelines = value.get("timelines")
    for item, name in (
        (url_list, "url_list"),
        (new_timelines, "new_timelines"),
        (public_urls, "public_video_url_list"),
        (timelines, "timelines"),
    ):
        if not isinstance(item, list):
            raise VideoDataAggregationValidationError(f"{name} 必须是数组")
    if len(url_list) != len(new_timelines):
        raise VideoDataAggregationValidationError(
            f"url_list 和 new_timelines 必须一一对应：当前长度分别为 {len(url_list)} 和 {len(new_timelines)}"
        )
    if len(public_urls) != len(timelines):
        raise VideoDataAggregationValidationError(
            f"public_video_url_list 和 timelines 必须一一对应：当前长度分别为 {len(public_urls)} 和 {len(timelines)}"
        )
    for index, timeline in enumerate(timelines):
        _validate_timeline(timeline, f"timelines[{index}]")
    for index, timeline in enumerate(new_timelines):
        _validate_timeline(timeline, f"new_timelines[{index}]")
    for name, urls in (("url_list", url_list), ("public_video_url_list", public_urls)):
        for index, url in enumerate(urls):
            if not isinstance(url, str) or not url.strip():
                raise VideoDataAggregationValidationError(f"{name}[{index}] 必须是非空字符串")
    return url_list, new_timelines, public_urls, timelines


def _apply_overlay(segments: list[dict[str, Any]], overlay: dict[str, Any]) -> list[dict[str, Any]]:
    if overlay["end"] <= overlay["start"]:
        raise VideoDataAggregationValidationError(
            f"new timeline 时间无效：end 必须大于 start，当前为 {overlay['start']} → {overlay['end']}"
        )
    result: list[dict[str, Any]] = []
    for segment in segments:
        if segment["end"] <= overlay["start"] or segment["start"] >= overlay["end"]:
            result.append(segment)
            continue
        if segment["start"] < overlay["start"]:
            left = dict(segment)
            left["end"] = overlay["start"]
            result.append(left)
        if segment["end"] > overlay["end"]:
            right = dict(segment)
            right["start"] = overlay["end"]
            result.append(right)
    result.append(dict(overlay))
    return result


def _merge_adjacent(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for current in segments:
        if current["end"] <= current["start"]:
            continue
        previous = result[-1] if result else None
        if (
            previous
            and previous["end"] == current["start"]
            and previous["url"] == current["url"]
            and previous["source"] == current["source"]
            and previous["source_index"] == current["source_index"]
        ):
            previous["end"] = current["end"]
        else:
            result.append(dict(current))
    return result


def run_video_data_aggregation(params: Any) -> dict[str, Any]:
    value = _resolve(params)
    url_list, new_timelines, public_urls, timelines = _validate_inputs(value)

    segments = [
        {
            "start": _number(timeline["start"], f"timelines[{index}].start"),
            "end": _number(timeline["end"], f"timelines[{index}].end"),
            "url": public_urls[index],
            "source": "public",
            "source_index": index,
        }
        for index, timeline in enumerate(timelines)
    ]
    segments.sort(key=lambda item: (item["start"], item["end"]))

    for index, timeline in enumerate(new_timelines):
        overlay_timeline = _validate_timeline(timeline, f"new_timelines[{index}]")
        overlay = {
            **overlay_timeline,
            "url": url_list[index],
            "source": "new",
            "source_index": index,
        }
        segments = _apply_overlay(segments, overlay)

    segments = _merge_adjacent(sorted(segments, key=lambda item: (item["start"], item["end"])))
    super_timelines = [{"start": item["start"], "end": item["end"]} for item in segments]
    super_urls = [item["url"] for item in segments]
    super_segments = [
        {
            "start": item["start"],
            "end": item["end"],
            "url": item["url"],
            "source": item["source"],
            "source_index": item["source_index"],
        }
        for item in segments
    ]
    return {
        "super_timelines": super_timelines,
        "super_url_list": super_urls,
        "super_segments": super_segments,
    }


__all__ = [
    "VideoDataAggregationValidationError",
    "run_video_data_aggregation",
]
