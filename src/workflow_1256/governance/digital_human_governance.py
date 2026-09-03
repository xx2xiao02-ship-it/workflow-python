"""数字人镜头的坑位、连续性与占比治理。

数字人是替换某一个已锁定小镜头的可选视觉路线，不能按大分段随意覆盖。
本模块只校验素材是否可进入剪辑层，不发起数字人生成任务。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import DirectorLockedManifest, TimeWindow


DEFAULT_MAX_DIGITAL_HUMAN_SHOT_RATIO = 0.15


class DigitalHumanGovernanceError(ValueError):
    """数字人素材不满足已锁定镜头坑位治理。"""


def normalize_digital_human_policy(policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """规范化可被项目配置覆盖的数字人治理策略。"""

    source = dict(policy or {})
    ratio = source.get("max_shot_ratio", DEFAULT_MAX_DIGITAL_HUMAN_SHOT_RATIO)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        raise DigitalHumanGovernanceError("digital_human_policy.max_shot_ratio 必须在 0 到 1 之间")
    return {
        "max_shot_ratio": float(ratio),
        "forbid_consecutive_shots": True,
        "require_exact_slot_timeline": True,
    }


def validate_digital_human_slots(
    director: DirectorLockedManifest,
    assignments: Sequence[Mapping[str, Any]],
    *,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """验证数字人素材逐条精确替换已锁定的小镜头坑位。

    每条 assignment 必须包含 ``target_shot_id``、``start_us`` 与 ``end_us``。
    ``start/end`` 也可作为兼容输入，但不会容忍时间窗偏移、重复坑位或相邻坑位。
    """

    if not isinstance(director, DirectorLockedManifest):
        raise DigitalHumanGovernanceError("数字人治理需要 DirectorLockedManifest")
    if not isinstance(assignments, Sequence) or isinstance(assignments, (str, bytes)):
        raise DigitalHumanGovernanceError("digital_human_assignments 必须是数组")
    normalized_policy = normalize_digital_human_policy(policy)
    shots = list(director.shots)
    shot_by_id = {shot.shot_id: shot for shot in shots}
    position_by_id = {shot.shot_id: index for index, shot in enumerate(shots)}
    selected_positions: list[int] = []
    selected_ids: set[str] = set()

    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, Mapping):
            raise DigitalHumanGovernanceError(f"digital_human_assignments[{index}] 必须是对象")
        shot_id = assignment.get("target_shot_id")
        if not isinstance(shot_id, str) or shot_id not in shot_by_id:
            raise DigitalHumanGovernanceError(f"digital_human_assignments[{index}].target_shot_id 必须指向锁定小镜头")
        if shot_id in selected_ids:
            raise DigitalHumanGovernanceError(f"数字人镜头坑位重复：{shot_id}")
        shot = shot_by_id[shot_id]
        if shot.production_spec.get("selected_route") != "digital_human":
            raise DigitalHumanGovernanceError(
                f"{shot_id} 不是已锁定的数字人镜头坑位；候选路线不能直接占用坑位"
            )
        start = assignment.get("start_us", assignment.get("start"))
        end = assignment.get("end_us", assignment.get("end"))
        if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int):
            raise DigitalHumanGovernanceError(f"{shot_id} 的 start/end 必须是整数微秒")
        actual = TimeWindow(start, end)
        if actual != shot.timeline:
            raise DigitalHumanGovernanceError(
                f"{shot_id} 的数字人时间窗必须精确等于镜头坑位："
                f"期望 {shot.timeline.start_us}-{shot.timeline.end_us}，实际 {start}-{end}"
            )
        selected_ids.add(shot_id)
        selected_positions.append(position_by_id[shot_id])

    selected_positions.sort()
    if normalized_policy["forbid_consecutive_shots"]:
        for previous, current in zip(selected_positions, selected_positions[1:]):
            if current == previous + 1:
                raise DigitalHumanGovernanceError("数字人镜头不允许连续：请至少间隔一个普通镜头")

    ratio = len(selected_positions) / len(shots) if shots else 0.0
    if ratio > normalized_policy["max_shot_ratio"]:
        raise DigitalHumanGovernanceError(
            f"数字人镜头占比 {ratio:.1%} 超过上限 {normalized_policy['max_shot_ratio']:.1%}"
        )
    return {
        "status": "DIGITAL_HUMAN_GOVERNED",
        "policy": normalized_policy,
        "digital_human_shot_ids": [shots[position].shot_id for position in selected_positions],
        "digital_human_shot_count": len(selected_positions),
        "total_shot_count": len(shots),
        "actual_shot_ratio": ratio,
    }


__all__ = [
    "DEFAULT_MAX_DIGITAL_HUMAN_SHOT_RATIO",
    "DigitalHumanGovernanceError",
    "normalize_digital_human_policy",
    "validate_digital_human_slots",
]
