"""连续静态镜头的节奏纠偏。

剪映 STT 字幕和其微秒时间线始终保持原样。本模块只在视觉槽位层合并
同一大分段内、语义连续的两个静态候选，以避免成串图片让成片退化为幻灯片。
"""

from __future__ import annotations

import math
from copy import deepcopy
from collections.abc import Mapping
from typing import Any


STATIC_STREAK_THRESHOLD = 4
MERGED_AIGC_MAX_DURATION_US = 5_500_000
NATIVE_AIGC_MIN_DURATION_US = 3_000_000


class ConsecutiveStaticPacingError(ValueError):
    """连续静态镜头纠偏的输入不满足既有编导契约。"""


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConsecutiveStaticPacingError(f"{field} 必须是数组")
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConsecutiveStaticPacingError(f"{field} 必须是对象")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConsecutiveStaticPacingError(f"{field} 必须是非空字符串")
    return value.strip()


def _timeline(value: Any, field: str) -> dict[str, int]:
    raw = _mapping(value, field)
    start = raw.get("start_us", raw.get("start"))
    end = raw.get("end_us", raw.get("end"))
    if isinstance(start, bool) or not isinstance(start, int):
        raise ConsecutiveStaticPacingError(f"{field}.start 必须是整数微秒")
    if isinstance(end, bool) or not isinstance(end, int) or end <= start:
        raise ConsecutiveStaticPacingError(f"{field}.end 必须大于 start")
    return {"start": start, "end": end}


def _duration_us(timeline: Mapping[str, int]) -> int:
    return int(timeline["end"]) - int(timeline["start"])


def _pacing_metadata(*, action: str, weight: int, source_slot_ids: list[str]) -> dict[str, Any]:
    return {
        "policy": "consecutive_static_weighted_aigc_v1",
        "action": action,
        "force_aigc": True,
        "static_streak_threshold": STATIC_STREAK_THRESHOLD,
        "static_streak_weight": weight,
        "source_slot_ids": source_slot_ids,
        "merged_aigc_max_duration_us": MERGED_AIGC_MAX_DURATION_US,
    }


def apply_consecutive_static_video_pacing(
    code_list: list[Mapping[str, Any]],
    director_lock: Mapping[str, Any],
    media_route_lock: Mapping[str, Any],
) -> dict[str, Any]:
    """将第 4 个连续静态候选起的视觉节奏纠偏为视频优先。

    优先合并当前连续序列最后两个同组槽位；合并窗口不得超过 5.5 秒。
    若两个槽位无法安全合并，选择本序列内最近的 3 秒以上静态槽位提升为
    AIGC 视频。两个动作都会中断静态连续计数，但不会修改字幕、旁白或其
    原始 CapCut STT 时间线。
    """

    groups = deepcopy(_list(code_list, "Code_list"))
    lock = _mapping(director_lock, "director_lock")
    locked_shots = _list(lock.get("shots"), "director_lock.shots")
    route_lock = _mapping(media_route_lock, "media_route_lock")
    routes = _list(route_lock.get("routes"), "media_route_lock.routes")
    if len(routes) != len(locked_shots):
        raise ConsecutiveStaticPacingError("媒体路由与冻结镜头数量不一致")

    records: list[dict[str, Any]] = []
    cursor = 0
    for group_index, raw_group in enumerate(groups):
        group = _mapping(raw_group, f"Code_list[{group_index}]")
        shots = _list(group.get("shots"), f"Code_list[{group_index}].shots")
        timelines = _list(group.get("timelines"), f"Code_list[{group_index}].timelines")
        if len(shots) != len(timelines):
            raise ConsecutiveStaticPacingError(
                f"Code_list[{group_index}] 的 shots 与 timelines 数量不一致"
            )
        for slot_index, (raw_shot, raw_timeline) in enumerate(zip(shots, timelines, strict=True)):
            if cursor >= len(locked_shots):
                raise ConsecutiveStaticPacingError("Code_list 镜头数超过冻结镜头数")
            shot = _mapping(raw_shot, f"Code_list[{group_index}].shots[{slot_index}]")
            timeline = _timeline(raw_timeline, f"Code_list[{group_index}].timelines[{slot_index}]")
            locked = _mapping(locked_shots[cursor], f"director_lock.shots[{cursor}]")
            route = _mapping(routes[cursor], f"media_route_lock.routes[{cursor}]")
            shot_id = _text(locked.get("shot_id"), f"director_lock.shots[{cursor}].shot_id")
            group_id = _text(locked.get("group_id"), f"director_lock.shots[{cursor}].group_id")
            if route.get("shot_id") != shot_id:
                raise ConsecutiveStaticPacingError("媒体路由 shot_id 与冻结镜头顺序不一致")
            locked_timeline = _timeline(locked.get("timeline"), f"{shot_id}.timeline")
            if timeline != locked_timeline:
                raise ConsecutiveStaticPacingError(f"{shot_id} 的 Code_list 时间线与冻结镜头不一致")
            records.append({
                "index": cursor,
                "group_index": group_index,
                "slot_index": slot_index,
                "shot_id": shot_id,
                "group_id": group_id,
                "timeline": timeline,
                "media_type": route.get("media_type"),
            })
            cursor += 1
    if cursor != len(locked_shots):
        raise ConsecutiveStaticPacingError("Code_list 镜头数与冻结镜头数不一致")

    merge_pairs: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    promoted: list[tuple[dict[str, Any], int]] = []
    unresolved_runs: list[list[str]] = []
    static_run: list[dict[str, Any]] = []
    for record in records:
        if record["media_type"] != "static_image":
            static_run = []
            continue
        static_run.append(record)
        if len(static_run) < STATIC_STREAK_THRESHOLD:
            continue
        weight = len(static_run) - STATIC_STREAK_THRESHOLD + 1
        previous = static_run[-2]
        current = static_run[-1]
        combined_duration_us = _duration_us(previous["timeline"]) + _duration_us(current["timeline"])
        same_semantic_group = (
            previous["group_index"] == current["group_index"]
            and previous["slot_index"] + 1 == current["slot_index"]
            and previous["group_id"] == current["group_id"]
        )
        if same_semantic_group and combined_duration_us <= MERGED_AIGC_MAX_DURATION_US:
            merge_pairs.append((previous, current, weight))
            static_run = []
            continue
        promotable = next(
            (
                candidate for candidate in reversed(static_run)
                if NATIVE_AIGC_MIN_DURATION_US <= _duration_us(candidate["timeline"]) <= MERGED_AIGC_MAX_DURATION_US
            ),
            None,
        )
        if promotable is not None:
            promoted.append((promotable, weight))
            static_run = []
        else:
            unresolved_runs.append([str(item["shot_id"]) for item in static_run])
            static_run = []

    promotions_by_location = {
        (record["group_index"], record["slot_index"]): weight
        for record, weight in promoted
    }
    for group_index, raw_group in enumerate(groups):
        group = dict(_mapping(raw_group, f"Code_list[{group_index}]"))
        shots = [dict(_mapping(item, f"Code_list[{group_index}].shots")) for item in _list(group.get("shots"), f"Code_list[{group_index}].shots")]
        timelines = [_timeline(item, f"Code_list[{group_index}].timelines") for item in _list(group.get("timelines"), f"Code_list[{group_index}].timelines")]
        for slot_index, shot in enumerate(shots):
            weight = promotions_by_location.get((group_index, slot_index))
            if weight is None:
                continue
            source_id = next(
                record["shot_id"] for record, candidate_weight in promoted
                if record["group_index"] == group_index and record["slot_index"] == slot_index and candidate_weight == weight
            )
            shot["media_pacing"] = _pacing_metadata(
                action="promote_single_static_to_aigc",
                weight=weight,
                source_slot_ids=[source_id],
            )
        relevant_pairs = [pair for pair in merge_pairs if pair[0]["group_index"] == group_index]
        for left, right, weight in sorted(relevant_pairs, key=lambda item: item[0]["slot_index"], reverse=True):
            slot_index = int(left["slot_index"])
            left_shot = shots[slot_index]
            right_shot = shots[slot_index + 1]
            left_timeline = timelines[slot_index]
            right_timeline = timelines[slot_index + 1]
            merged_timeline = {"start": left_timeline["start"], "end": right_timeline["end"]}
            merged_duration_s = _duration_us(merged_timeline) / 1_000_000
            merged_shot = {
                **left_shot,
                "source_text": "；".join([
                    _text(left_shot.get("source_text"), "left.source_text"),
                    _text(right_shot.get("source_text"), "right.source_text"),
                ]),
                "clip_role": "连续动作推进",
                "story_beat": "→".join([
                    _text(left_shot.get("story_beat"), "left.story_beat"),
                    _text(right_shot.get("story_beat"), "right.story_beat"),
                ]),
                "narration_text": _text(left_shot.get("narration_text"), "left.narration_text") + _text(right_shot.get("narration_text"), "right.narration_text"),
                "clip_duration": round(merged_duration_s, 3),
                "media_pacing": _pacing_metadata(
                    action="merge_adjacent_static_to_aigc",
                    weight=weight,
                    source_slot_ids=[str(left["shot_id"]), str(right["shot_id"])],
                ),
            }
            shots[slot_index:slot_index + 2] = [merged_shot]
            timelines[slot_index:slot_index + 2] = [merged_timeline]
        group["shots"] = shots
        group["timelines"] = timelines
        group["clip_duration"] = [round(_duration_us(item) / 1_000_000, 3) for item in timelines]
        group["int_duration"] = [math.ceil(value) for value in group["clip_duration"]]
        groups[group_index] = group

    return {
        "code_list": groups,
        "policy": {
            "name": "consecutive_static_weighted_aigc_v1",
            "static_streak_threshold": STATIC_STREAK_THRESHOLD,
            "merged_aigc_max_duration_us": MERGED_AIGC_MAX_DURATION_US,
            "subtitle_timeline_mutated": False,
            "original_visual_slot_count": len(records),
            "result_visual_slot_count": sum(len(_list(group.get("shots"), "Code_list.shots")) for group in groups),
            "merged_spans": [
                {
                    "source_slot_ids": [str(left["shot_id"]), str(right["shot_id"])],
                    "duration_us": _duration_us(left["timeline"]) + _duration_us(right["timeline"]),
                    "static_streak_weight": weight,
                }
                for left, right, weight in merge_pairs
            ],
            "promoted_single_slot_ids": [str(record["shot_id"]) for record, _weight in promoted],
            "unresolved_static_runs": unresolved_runs,
        },
    }


__all__ = [
    "ConsecutiveStaticPacingError",
    "MERGED_AIGC_MAX_DURATION_US",
    "STATIC_STREAK_THRESHOLD",
    "apply_consecutive_static_video_pacing",
]
