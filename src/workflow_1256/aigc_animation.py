"""1256 工作流“AIGC动画*”(174651)的本地 Python 契约实现。

本模块只负责输入归一化、已读 add_videos 规则的边界行为和输出投影。
真实草稿下载、轨道写入和素材处理必须通过显式注入 executor；默认执行不会
调用网络、模型或插件。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any


class AIGCAnimationValidationError(ValueError):
    """AIGC动画* 的输入或输出契约错误。"""


class AIGCAnimationTransportRequired(RuntimeError):
    """未注入 add_videos 草稿执行器。"""


OPTIONAL_FIELDS = (
    "scene_timelines",
    "alpha",
    "scale_x",
    "scale_y",
    "transform_x",
    "transform_y",
)


def _resolve_params(params: Any) -> Mapping[str, Any]:
    value = getattr(params, "input", params)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AIGCAnimationValidationError("节点输入 JSON 无效") from exc
    for key in ("input", "params", "_input"):
        if isinstance(value, Mapping) and isinstance(value.get(key), Mapping):
            value = value[key]
    if not isinstance(value, Mapping):
        raise AIGCAnimationValidationError("AIGC动画* 输入必须是对象")
    return value


def _parse_video_infos(video_infos: str) -> list[Any]:
    try:
        data = json.loads(video_infos)
    except json.JSONDecodeError as exc:
        raise AIGCAnimationValidationError(f"JSON parse error: {exc.msg}") from exc
    if not isinstance(data, list):
        raise AIGCAnimationValidationError("video_infos should be a list")
    return data


def normalize_video_infos(video_infos: str) -> list[dict[str, Any]]:
    """按 capcut-mate add_videos.py 的 parse_video_data 规则归一化。"""

    if not isinstance(video_infos, str):
        raise AIGCAnimationValidationError("video_infos 必须是 JSON 字符串")
    data = _parse_video_infos(video_infos)
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise AIGCAnimationValidationError(f"the {index}th item should be a dict")
        required_fields = ["video_url", "start", "end"]
        missing_fields = [field for field in required_fields if field not in item]
        if missing_fields:
            raise AIGCAnimationValidationError(
                f"the {index}th item is missing required fields: {', '.join(missing_fields)}"
            )
        if not isinstance(item["start"], (int, float)) or item["start"] < 0:
            raise AIGCAnimationValidationError(f"the {index}th item has invalid start time")
        if not isinstance(item["end"], (int, float)) or item["end"] <= item["start"]:
            raise AIGCAnimationValidationError(f"the {index}th item has invalid end time")

        start = int(item["start"])
        end = int(item["end"])
        if end <= start:
            raise AIGCAnimationValidationError(f"the {index}th item has invalid end time")

        if "duration" in item:
            duration = item["duration"]
            if not isinstance(duration, (int, float)) or duration <= 0:
                raise AIGCAnimationValidationError(f"the {index}th item has invalid duration")
            duration = int(duration)
        else:
            duration = end - start

        processed = {
            "video_url": item["video_url"],
            "width": item.get("width"),
            "height": item.get("height"),
            "start": start,
            "end": end,
            "duration": duration,
            "mask": item.get("mask", None),
            "transition": item.get("transition", None),
            "transition_duration": item.get("transition_duration", None),
            "volume": 1.0 if item.get("volume") is None else item.get("volume"),
        }
        try:
            if processed["volume"] < 0 or processed["volume"] > 10:
                processed["volume"] = 1.0
        except TypeError as exc:
            raise AIGCAnimationValidationError(f"the {index}th item has invalid volume") from exc
        normalized.append(processed)
    return normalized


def _is_valid_scene_timeline(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if "start" not in value or "end" not in value:
        return False
    try:
        return value["end"] > value["start"]
    except TypeError:
        return False


def has_valid_scene_timelines(scene_timelines: Any, video_count: int) -> bool:
    """复现源码：只有每个视频都有有效场景时间线时才启用连续拼接。"""

    if not scene_timelines or not isinstance(scene_timelines, list):
        return False
    if len(scene_timelines) < video_count:
        return False
    return all(_is_valid_scene_timeline(scene_timelines[i]) for i in range(video_count))


def derive_segment_timeline(video_infos: str, scene_timelines: Any = None) -> list[dict[str, int]]:
    """只根据已读源码计算片段时间线，用于离线审计，不生成动态 ID。"""

    videos = normalize_video_infos(video_infos)
    if not videos:
        raise AIGCAnimationValidationError("video_infos 不能为空")
    keep_continuity = has_valid_scene_timelines(scene_timelines, len(videos))
    current_track_end = 0
    result: list[dict[str, int]] = []
    for index, video in enumerate(videos):
        start, end = video["start"], video["end"]
        scene = scene_timelines[index] if isinstance(scene_timelines, list) and index < len(scene_timelines) else None
        if keep_continuity and index > 0 and current_track_end > 0:
            original_duration = video["end"] - video["start"]
            start = current_track_end
            end = start + original_duration
        display_duration = end - start
        actual_duration = display_duration
        if isinstance(scene, Mapping):
            scene_duration = scene["end"] - scene["start"]
            if scene_duration > 0:
                actual_duration = scene_duration
        current_track_end = start + actual_duration
        result.append({"index": index, "start": start, "end": end, "duration": actual_duration})
    return result


def _string_array(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AIGCAnimationValidationError(f"{field} 必须是字符串数组")
    return list(value)


def _segment_infos(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AIGCAnimationValidationError("segment_infos 必须是数组")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise AIGCAnimationValidationError(f"segment_infos[{index}] 必须是对象")
        if not isinstance(item.get("id"), str):
            raise AIGCAnimationValidationError(f"segment_infos[{index}].id 必须是字符串")
        if not isinstance(item.get("start"), int) or not isinstance(item.get("end"), int):
            raise AIGCAnimationValidationError(f"segment_infos[{index}] 的 start/end 必须是整数微秒")
        result.append(dict(item))
    return result


def project_add_videos_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """按原始 YAML 输出顺序投影并校验 add_videos 响应。"""

    if not isinstance(response.get("draft_url"), str):
        raise AIGCAnimationValidationError("draft_url 必须是字符串")
    if not isinstance(response.get("track_id"), str):
        raise AIGCAnimationValidationError("track_id 必须是字符串")
    return {
        "draft_url": response["draft_url"],
        "segment_ids": _string_array(response.get("segment_ids"), "segment_ids"),
        "segment_infos": _segment_infos(response.get("segment_infos")),
        "track_id": response["track_id"],
        "video_ids": _string_array(response.get("video_ids"), "video_ids"),
    }


Executor = Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]


def run_aigc_animation(params: Any, *, executor: Executor | None = None) -> dict[str, Any]:
    """执行 174651 本地契约；真实草稿写入必须显式注入 executor。"""

    value = _resolve_params(params)
    draft_url = value.get("draft_url")
    if not isinstance(draft_url, str) or not draft_url:
        raise AIGCAnimationValidationError("draft_url 必须是非空字符串")
    video_infos = value.get("video_infos")
    normalized = normalize_video_infos(video_infos)
    if not normalized:
        raise AIGCAnimationValidationError("video_infos 不能为空")

    options = {key: value[key] for key in OPTIONAL_FIELDS if key in value}
    if "scene_timelines" in options:
        timelines = options["scene_timelines"]
        if timelines is not None and not isinstance(timelines, list):
            raise AIGCAnimationValidationError("scene_timelines 必须是数组或 null")
    if executor is None:
        raise AIGCAnimationTransportRequired("未注入 add_videos 草稿写入执行器")
    response = executor(draft_url, video_infos, options)
    if not isinstance(response, Mapping):
        raise AIGCAnimationValidationError("add_videos 执行器输出必须是对象")
    return project_add_videos_response(response)


def main(params: Any, *, executor: Executor | None = None) -> dict[str, Any]:
    return run_aigc_animation(params, executor=executor)


__all__ = [
    "AIGCAnimationTransportRequired",
    "AIGCAnimationValidationError",
    "derive_segment_timeline",
    "has_valid_scene_timelines",
    "main",
    "normalize_video_infos",
    "project_add_videos_response",
    "run_aigc_animation",
]
