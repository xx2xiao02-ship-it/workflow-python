"""Python implementation of Coze node ``时长计算时间线规划``.

This module intentionally mirrors the read-only Coze source snapshot. It does
not call models, plugins, network services, or use the unused ``segments``
input for additional logic.
"""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Mapping
from typing import Any


MIN_FULL_TENTHS = 40
MIN_LAST_TENTHS = 1
MAX_SHOT_TENTHS = 100


def _parse_value(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()

        if text:
            try:
                return json.loads(text)
            except Exception:
                return value

    return value


def _as_list(value: Any) -> list[Any]:
    value = _parse_value(value)
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    value = _parse_value(value)
    return value if isinstance(value, dict) else {}


def _clean_text(value: Any) -> str:
    if value is None:
        return ""

    return str(value).strip()


def _to_int(value: Any) -> int | None:
    try:
        return int(round(float(value)))
    except Exception:
        return None


def _to_tenths(value: Any) -> int:
    try:
        number = float(value)

        if number <= 0:
            return 0

        return int(round(number * 10))
    except Exception:
        return 0


def _get_timeline(value: Any) -> dict[str, Any]:
    value = _parse_value(value)

    if isinstance(value, dict):
        return value

    if isinstance(value, list) and len(value) == 1:
        first = _parse_value(value[0])

        if isinstance(first, dict):
            return first

    return {}


def _effective_text_len(text: Any) -> int:
    count = 0

    for char in _clean_text(text):
        if char.isspace():
            continue

        category = unicodedata.category(char)

        if (
            category.startswith("P")
            or category.startswith("Z")
            or category.startswith("C")
        ):
            continue

        count += 1

    return max(count, 1)


def _get_shot_count_bounds(
    total_tenths: int,
    *,
    max_shot_tenths: int = MAX_SHOT_TENTHS,
    min_shot_tenths: int = MIN_FULL_TENTHS,
) -> tuple[int, int]:
    min_count = max(
        1,
        math.ceil(total_tenths / max_shot_tenths)
    )

    # 原 Coze 节点的默认逻辑允许最后一条镜头只剩 0.1 秒；升级链路在显式
    # 指定 min_shot_seconds 后，所有镜头都必须守住该下限。
    last_min_tenths = MIN_LAST_TENTHS if min_shot_tenths == MIN_FULL_TENTHS else min_shot_tenths
    max_count = max(
        1,
        math.floor(
            (total_tenths - last_min_tenths) / min_shot_tenths
        ) + 1
    )

    return min_count, max_count


def _allocate_durations(
    total_tenths: int,
    source_text_list: list[str],
    *,
    max_shot_tenths: int = MAX_SHOT_TENTHS,
    min_shot_tenths: int = MIN_FULL_TENTHS,
) -> tuple[list[int], list[float], list[int]]:
    shot_count = len(source_text_list)

    if shot_count == 0:
        return [], [], []

    base_units = [min_shot_tenths] * shot_count
    if min_shot_tenths == MIN_FULL_TENTHS:
        # 保持未配置升级参数时的 Coze 原始尾镜行为，保证离线等价。
        base_units[-1] = MIN_LAST_TENTHS

    min_total = sum(base_units)
    max_total = shot_count * max_shot_tenths

    if total_tenths < min_total:
        return [], [], []

    if total_tenths > max_total:
        return [], [], []

    duration_units = list(base_units)
    remaining = total_tenths - min_total

    weights = [
        _effective_text_len(text)
        for text in source_text_list
    ]

    extras = [0] * shot_count

    max_extra = [
        max_shot_tenths - base
        for base in base_units
    ]

    for _ in range(remaining):
        candidates = [
            index
            for index in range(shot_count)
            if extras[index] < max_extra[index]
        ]

        if not candidates:
            break

        target_index = min(
            candidates,
            key=lambda index: (
                (extras[index] + 1) / weights[index],
                -weights[index],
                index
            )
        )

        extras[target_index] += 1

    for index in range(shot_count):
        duration_units[index] += extras[index]

    clip_duration = [
        round(value / 10, 1)
        for value in duration_units
    ]

    int_duration = [
        max(4, int(math.ceil(value / 10.0)))
        for value in duration_units
    ]

    return duration_units, clip_duration, int_duration


def _build_timelines(
    start: int,
    end: int,
    duration_units: list[int],
    *,
    max_shot_seconds: float | None = None,
    min_shot_seconds: float | None = None,
) -> list[dict[str, int]]:
    if not duration_units:
        return []

    total_units = sum(duration_units)
    total_span = end - start

    if total_units <= 0 or total_span <= 0:
        return []

    result: list[dict[str, int]] = []
    current_start = start
    accumulated_units = 0

    max_span = (
        int(round(max_shot_seconds * 1_000_000))
        if max_shot_seconds is not None
        else None
    )
    min_span = (
        int(round(min_shot_seconds * 1_000_000))
        if min_shot_seconds is not None
        else None
    )
    if min_span is not None and min_span <= 0:
        return []
    if max_span is not None and min_span is not None and min_span > max_span:
        return []
    if min_span is not None and total_span < min_span * len(duration_units):
        return []
    remaining_span = total_span
    for index, units in enumerate(duration_units):
        accumulated_units += units

        if index == len(duration_units) - 1:
            current_end = end
        elif max_span is None and min_span is None:
            current_end = start + int(round(total_span * accumulated_units / total_units))
        else:
            remaining_slots = len(duration_units) - index - 1
            ideal_span = int(round(total_span * units / total_units))
            lower_bound = max(1, remaining_span - (max_span or remaining_span) * remaining_slots)
            upper_bound = remaining_span - (min_span or 1) * remaining_slots
            if upper_bound < lower_bound:
                return []
            current_span = min(upper_bound, max(lower_bound, ideal_span))
            if max_span is not None:
                current_span = min(max_span, current_span)
            if min_span is not None:
                current_span = max(min_span, current_span)
            current_end = current_start + current_span

        if current_end <= current_start:
            current_end = current_start + 1

        result.append({
            "start": current_start,
            "end": current_end
        })

        remaining_span -= current_end - current_start
        current_start = current_end

    return result


def _empty_result() -> dict[str, Any]:
    return {
        "shots": [],
        "clip_duration": [],
        "int_duration": [],
        "timelines": []
    }


def _run_params(params: dict[str, Any]) -> dict[str, Any]:
    total_tenths = _to_tenths(params.get("duration"))
    configured_max_tenths = _to_tenths(params.get("max_shot_seconds"))
    max_shot_tenths = configured_max_tenths or MAX_SHOT_TENTHS
    configured_min_tenths = _to_tenths(params.get("min_shot_seconds"))
    min_shot_tenths = configured_min_tenths or MIN_FULL_TENTHS
    if min_shot_tenths <= 0 or max_shot_tenths < min_shot_tenths:
        return _empty_result()
    raw_shots = _as_list(params.get("shots"))
    source_timeline = _get_timeline(params.get("timelines"))

    timeline_start = _to_int(source_timeline.get("start"))
    timeline_end = _to_int(source_timeline.get("end"))

    if total_tenths <= 0 or not raw_shots:
        return _empty_result()

    if timeline_start is None or timeline_end is None:
        return _empty_result()

    if timeline_end <= timeline_start:
        return _empty_result()

    min_count, max_count = _get_shot_count_bounds(
        total_tenths,
        max_shot_tenths=max_shot_tenths,
        min_shot_tenths=min_shot_tenths,
    )

    if len(raw_shots) < min_count or len(raw_shots) > max_count:
        return _empty_result()

    duration_weight_text_list: list[str] = []
    clean_shots: list[dict[str, Any]] = []

    for raw_shot in raw_shots:
        shot = _as_dict(raw_shot)

        source_text = _clean_text(shot.get("source_text"))
        narration_text = _clean_text(shot.get("narration_text"))
        clip_role = _clean_text(shot.get("clip_role"))
        story_beat = _clean_text(shot.get("story_beat"))

        if not source_text or not clip_role or not story_beat:
            return _empty_result()

        duration_weight_text_list.append(narration_text or source_text)

        clean_shot = {
            "source_text": source_text,
            "clip_role": clip_role,
            "story_beat": story_beat
        }
        if narration_text:
            clean_shot["narration_text"] = narration_text
        clean_shots.append(clean_shot)

    duration_units, clip_duration, int_duration = _allocate_durations(
        total_tenths,
        duration_weight_text_list,
        max_shot_tenths=max_shot_tenths,
        min_shot_tenths=min_shot_tenths,
    )

    if not duration_units:
        return _empty_result()

    timelines = _build_timelines(
        timeline_start,
        timeline_end,
        duration_units,
        max_shot_seconds=max_shot_tenths / 10 if configured_max_tenths else None,
        min_shot_seconds=min_shot_tenths / 10 if configured_min_tenths else None,
    )

    if not timelines:
        return _empty_result()

    for index, shot in enumerate(clean_shots):
        shot["clip_duration"] = clip_duration[index]

    return {
        "shots": clean_shots,
        "clip_duration": clip_duration,
        "int_duration": int_duration,
        "timelines": timelines
    }


def _resolve_params(args: Any) -> dict[str, Any]:
    params = getattr(args, "params", None)

    if not isinstance(params, dict):
        if isinstance(args, dict):
            params = args.get("params", args)
        else:
            params = {}

    if isinstance(params.get("_input"), dict):
        params = params["_input"]

    return params


def run_timeline_planning(params: Any = None) -> dict[str, Any]:
    """Run the node with the same parameter wrapper behavior as Coze."""

    return _run_params(_resolve_params(type("Args", (), {"params": params})()))


async def main(args: Any) -> dict[str, Any]:
    """Coze-compatible async entry point."""

    if isinstance(args, Mapping):
        params = args.get("params", args)
    else:
        params = getattr(args, "params", None)

    return _run_params(_resolve_params(type("Args", (), {"params": params})()))
