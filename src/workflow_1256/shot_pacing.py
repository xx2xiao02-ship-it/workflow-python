"""逐镜节奏规划：在真实 TTS 大分段时间线之上统一分配镜头数量。

197742 仍按大分段批处理，但镜头数量不能再由每一段各自四舍五入决定。
本模块先从整片时长计算一个约 4.2 秒/镜的全局预算，再根据大分段的叙事节奏
把预算分配回每一段。它只输出数量约束，不改写 TTS 时间线，也不改变批处理顺序。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


# 4.2 秒保留解释段的完整性，同时给开场和转折留出可感知的加速空间。
# 门禁采用目标值上下 10% 的对称容错，避免真实 TTS 与语义分段的自然误差
# 把可用的逐镜脚本误判为失败；单镜 2--5 秒和字段/时间线契约仍是硬约束。
PREFERRED_SHOT_SECONDS = 4.2
PREFERRED_MIN_SHOT_SECONDS = 2.0
PREFERRED_MAX_SHOT_SECONDS = 5.0
PACING_TOLERANCE_RATIO = 0.10

# 这些值只决定“在全局预算不变时，额外镜头优先给谁”，不是给模型的硬时长。
# 反常识开场、转折和指出错误需要更快的切换；解释和举证可稍稳一些。
RHYTHM_TARGET_SECONDS: dict[str, float] = {
    "hook": 3.2,
    "turn": 3.4,
    "wrong": 3.5,
    "why": 4.8,
    "proof": 5.0,
    "fact": 4.4,
    "method": 4.5,
    "end": 4.6,
}


class ShotPacingValidationError(ValueError):
    """逐镜节奏输入无法与真实 TTS 时间线一一对应。"""


def pacing_average_bounds(
    preferred_shot_seconds: float = PREFERRED_SHOT_SECONDS,
    tolerance_ratio: float = PACING_TOLERANCE_RATIO,
) -> tuple[float, float]:
    """返回全片平均镜头时长的门禁区间。

    容错按目标值的相对比例计算，例如 4.2 秒、10% 对应 3.78--4.62 秒。
    该函数供规划器和真实执行门禁共同使用，避免两处规则再次漂移。
    """

    if preferred_shot_seconds <= 0:
        raise ShotPacingValidationError("preferred_shot_seconds 必须大于 0")
    if not 0 <= tolerance_ratio < 1:
        raise ShotPacingValidationError("tolerance_ratio 必须在 0 到 1 之间")
    return (
        preferred_shot_seconds * (1 - tolerance_ratio),
        preferred_shot_seconds * (1 + tolerance_ratio),
    )


def is_planned_average_within_tolerance(
    planned_average_seconds: float,
    preferred_shot_seconds: float = PREFERRED_SHOT_SECONDS,
    tolerance_ratio: float = PACING_TOLERANCE_RATIO,
) -> bool:
    """判断规划平均时长是否在统一节拍门禁区间内。"""

    minimum, maximum = pacing_average_bounds(preferred_shot_seconds, tolerance_ratio)
    return minimum <= planned_average_seconds <= maximum


@dataclass(frozen=True)
class GroupShotPacing:
    """一个大分段交给 197742 的固定镜头配额。"""

    group_index: int
    duration_seconds: float
    rhythm: str
    min_shot_count: int
    max_shot_count: int
    required_shot_count: int
    rhythm_target_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_index": self.group_index,
            "duration_seconds": round(self.duration_seconds, 6),
            "rhythm": self.rhythm,
            "min_shot_count": self.min_shot_count,
            "max_shot_count": self.max_shot_count,
            "required_shot_count": self.required_shot_count,
            "rhythm_target_seconds": self.rhythm_target_seconds,
        }


def _duration_seconds(raw: Mapping[str, Any], index: int) -> float:
    start, end = raw.get("start"), raw.get("end")
    if isinstance(start, bool) or not isinstance(start, int):
        raise ShotPacingValidationError(f"timelines[{index}].start 必须是整数微秒")
    if isinstance(end, bool) or not isinstance(end, int) or end <= start:
        raise ShotPacingValidationError(f"timelines[{index}] 必须满足 end > start")
    return (end - start) / 1_000_000


def _bounds(duration_seconds: float) -> tuple[int, int]:
    """返回可让常规镜头落在 2--5 秒窗口内的数量范围。"""

    minimum = max(1, math.ceil(duration_seconds / PREFERRED_MAX_SHOT_SECONDS))
    maximum = max(1, math.floor(duration_seconds / PREFERRED_MIN_SHOT_SECONDS))
    return minimum, max(minimum, maximum)


def _rhythm(beat: Mapping[str, Any]) -> str:
    value = beat.get("rhythm")
    return value.strip().lower() if isinstance(value, str) else ""


def _priority(duration_seconds: float, count: int, rhythm_target_seconds: float) -> float:
    """多给一个镜头能改善多少节奏；数值越高，越应优先增加。"""

    current_error = abs(duration_seconds / count - rhythm_target_seconds)
    next_error = abs(duration_seconds / (count + 1) - rhythm_target_seconds)
    return current_error - next_error


def plan_global_shot_pacing(
    timelines: Sequence[Mapping[str, Any]],
    segment_beats: Sequence[Mapping[str, Any]],
    *,
    preferred_shot_seconds: float = PREFERRED_SHOT_SECONDS,
) -> dict[str, Any]:
    """用全片目标均值给每个大分段分配固定小镜头数。

    先将总镜头数固定为 ``round(total / preferred_shot_seconds)``，再把可增加的
    镜头优先分给快节奏段。这样既避免每个大分段各自四舍五入导致全片漂移，也不会
    为了追逐平均值越过 2--5 秒的正常镜头数量边界。
    """

    average_min_seconds, average_max_seconds = pacing_average_bounds(preferred_shot_seconds)
    if len(timelines) != len(segment_beats):
        raise ShotPacingValidationError("TTS 时间线与 segment_beats 必须一一对应")
    if not timelines:
        raise ShotPacingValidationError("至少需要一个 TTS 大分段")

    raw_groups: list[dict[str, Any]] = []
    for index, (timeline, beat) in enumerate(zip(timelines, segment_beats, strict=True)):
        if not isinstance(timeline, Mapping) or not isinstance(beat, Mapping):
            raise ShotPacingValidationError("TTS 时间线与 segment_beats 必须是对象数组")
        duration_seconds = _duration_seconds(timeline, index)
        min_count, max_count = _bounds(duration_seconds)
        rhythm = _rhythm(beat)
        raw_groups.append({
            "group_index": index,
            "duration_seconds": duration_seconds,
            "rhythm": rhythm,
            "min_shot_count": min_count,
            "max_shot_count": max_count,
            "rhythm_target_seconds": RHYTHM_TARGET_SECONDS.get(rhythm, preferred_shot_seconds),
            "required_shot_count": min_count,
        })

    total_duration = sum(item["duration_seconds"] for item in raw_groups)
    desired_total = int(math.floor(total_duration / preferred_shot_seconds + 0.5))
    lower_total = sum(item["min_shot_count"] for item in raw_groups)
    upper_total = sum(item["max_shot_count"] for item in raw_groups)
    required_total = min(upper_total, max(lower_total, desired_total))

    while sum(item["required_shot_count"] for item in raw_groups) < required_total:
        candidates = [item for item in raw_groups if item["required_shot_count"] < item["max_shot_count"]]
        if not candidates:
            break
        selected = max(
            candidates,
            key=lambda item: (
                _priority(
                    item["duration_seconds"],
                    item["required_shot_count"],
                    item["rhythm_target_seconds"],
                ),
                item["duration_seconds"],
                -item["group_index"],
            ),
        )
        selected["required_shot_count"] += 1

    groups = [GroupShotPacing(**item) for item in raw_groups]
    return {
        "policy": "global_target_4_2_seconds_with_10pct_tolerance_and_rhythm_priority",
        "preferred_shot_seconds": preferred_shot_seconds,
        "average_tolerance_ratio": PACING_TOLERANCE_RATIO,
        "average_min_seconds": round(average_min_seconds, 6),
        "average_max_seconds": round(average_max_seconds, 6),
        "total_duration_seconds": round(total_duration, 6),
        "required_shot_count": required_total,
        "planned_average_seconds": round(total_duration / required_total, 6),
        "groups": [item.to_dict() for item in groups],
    }


__all__ = [
    "GroupShotPacing",
    "PREFERRED_MAX_SHOT_SECONDS",
    "PREFERRED_MIN_SHOT_SECONDS",
    "PREFERRED_SHOT_SECONDS",
    "PACING_TOLERANCE_RATIO",
    "RHYTHM_TARGET_SECONDS",
    "ShotPacingValidationError",
    "is_planned_average_within_tolerance",
    "pacing_average_bounds",
    "plan_global_shot_pacing",
]
