"""逐镜坑位规划：先锁定语义旁白与真实 TTS 时间线，再设计画面。

这是冻结编导链路的升级层，不改变 Coze 节点 127095 的原始兼容实现：
``真实 TTS 大分段`` → ``语义旁白切分`` → ``小镜头坑位`` → ``视觉设计``。
视觉导演只能为已存在的坑位补充画面语义，不能再改旁白边界、镜头数量或时长。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from .shot_pacing import ShotPacingValidationError, plan_global_shot_pacing
from .timeline_planning import run_timeline_planning


class ShotSlotPlanningValidationError(ValueError):
    """语义、TTS 时间线或视觉设计无法形成一一对应的镜头坑位。"""


_PUNCTUATION = frozenset("，、；：。！？!?\n")
_CONNECTOR_PATTERN = re.compile("但是|而且|因为|所以|如果|然后|同时|却|并且|而")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShotSlotPlanningValidationError(f"{field} 必须是非空字符串")
    return value.strip()


def _narration_text(value: Any, field: str) -> str:
    """校验旁白存在，但绝不修改已锁定的空白字符。

    ``narration_text`` 不同于展示型的 ``source_text``：它需要与真实 TTS
    文案逐字符对应。特别是段内换行可能恰好落在两个镜头坑位之间，若用
    ``strip()`` 清洗会让下游拼接时丢失原文字符。
    """

    if not isinstance(value, str) or not value.strip():
        raise ShotSlotPlanningValidationError(f"{field} 必须是非空字符串")
    return value


def _timeline(value: Any, index: int) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ShotSlotPlanningValidationError(f"timelines[{index}] 必须是对象")
    start, end = value.get("start"), value.get("end")
    if isinstance(start, bool) or not isinstance(start, int):
        raise ShotSlotPlanningValidationError(f"timelines[{index}].start 必须是整数微秒")
    if isinstance(end, bool) or not isinstance(end, int) or end <= start:
        raise ShotSlotPlanningValidationError(f"timelines[{index}] 必须满足 end > start")
    return {"start": start, "end": end}


def _effective_length(text: str) -> int:
    return max(
        1,
        sum(
            1
            for char in text
            if not char.isspace()
            and not unicodedata.category(char).startswith(("P", "Z", "C"))
        ),
    )


def _semantic_atoms(text: str) -> list[str]:
    """先在自然标点处断开，并把标点保留在前一个语义单元内。"""

    atoms: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char in _PUNCTUATION:
            atom = text[start : index + 1]
            if atom:
                atoms.append(atom)
            start = index + 1
    if start < len(text):
        atoms.append(text[start:])
    return [atom for atom in atoms if atom]


def _split_long_atom(atom: str) -> tuple[str, str]:
    """标点不足时的保底切分，优先在连接词前切。"""

    if len(atom) < 4:
        raise ShotSlotPlanningValidationError("旁白语义单元过短，无法再切出独立镜头坑位")
    midpoint = len(atom) // 2
    candidates = [
        match.start()
        for match in _CONNECTOR_PATTERN.finditer(atom)
        if 2 <= match.start() <= len(atom) - 2
    ]
    split_at = min(candidates, key=lambda value: (abs(value - midpoint), value)) if candidates else midpoint
    split_at = min(len(atom) - 2, max(2, split_at))
    return atom[:split_at], atom[split_at:]


def split_narration_semantically(text: str, required_slot_count: int) -> list[str]:
    """将一段原旁白切为固定数量、首尾相连的语义坑位文本。"""

    if isinstance(required_slot_count, bool) or not isinstance(required_slot_count, int) or required_slot_count < 1:
        raise ShotSlotPlanningValidationError("required_slot_count 必须是正整数")
    source = _text(text, "segment")
    if required_slot_count == 1:
        return [source]
    atoms = _semantic_atoms(source)
    while len(atoms) < required_slot_count:
        candidate_index = max(range(len(atoms)), key=lambda index: (_effective_length(atoms[index]), -index))
        left, right = _split_long_atom(atoms[candidate_index])
        atoms[candidate_index : candidate_index + 1] = [left, right]

    groups: list[str] = []
    atom_index = 0
    remaining_weight = sum(_effective_length(atom) for atom in atoms)
    for slot_index in range(required_slot_count):
        remaining_slots = required_slot_count - slot_index
        remaining_atoms = len(atoms) - atom_index
        take_count = 1
        if remaining_slots == 1:
            take_count = remaining_atoms
        else:
            target = remaining_weight / remaining_slots
            current_weight = _effective_length(atoms[atom_index])
            while atom_index + take_count < len(atoms) - (remaining_slots - 1):
                next_weight = _effective_length(atoms[atom_index + take_count])
                if abs((current_weight + next_weight) - target) > abs(current_weight - target):
                    break
                current_weight += next_weight
                take_count += 1
        selected = atoms[atom_index : atom_index + take_count]
        groups.append("".join(selected))
        remaining_weight -= sum(_effective_length(atom) for atom in selected)
        atom_index += take_count
    if "".join(groups) != source:
        raise ShotSlotPlanningValidationError("语义切分后旁白未能完整还原原始文案")
    return groups


def plan_tts_semantic_shot_slots(
    timelines: Sequence[Mapping[str, Any]],
    segments: Sequence[str],
    segment_beats: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """历史兼容实现；当前 live 链路使用 :func:`plan_stt_caption_shot_slots`。"""

    if not (len(timelines) == len(segments) == len(segment_beats)):
        raise ShotSlotPlanningValidationError("TTS 时间线、原始分段与节拍必须一一对应")
    if not timelines:
        raise ShotSlotPlanningValidationError("至少需要一个 TTS 大分段")
    try:
        pacing_plan = plan_global_shot_pacing(timelines, segment_beats)
    except ShotPacingValidationError as exc:
        raise ShotSlotPlanningValidationError(str(exc)) from exc

    groups: list[dict[str, Any]] = []
    for index, (raw_timeline, raw_segment, pacing) in enumerate(
        zip(timelines, segments, pacing_plan["groups"], strict=True)
    ):
        timeline = _timeline(raw_timeline, index)
        segment = _text(raw_segment, f"segments[{index}]")
        required_count = int(pacing["required_shot_count"])
        narration_parts = split_narration_semantically(segment, required_count)
        duration_s = (timeline["end"] - timeline["start"]) / 1_000_000
        placeholder_shots = [
            {
                "source_text": f"第{slot_index}个语义坑位",
                "narration_text": narration_text,
                "clip_role": "待视觉设计",
                "story_beat": "旁白语义坑位已冻结",
            }
            for slot_index, narration_text in enumerate(narration_parts, start=1)
        ]
        timeline_result = run_timeline_planning(
            {
                "duration": duration_s,
                "shots": placeholder_shots,
                "timelines": timeline,
                "min_shot_seconds": 2.0,
                "max_shot_seconds": 5.0,
            }
        )
        if len(timeline_result.get("timelines") or []) != required_count:
            raise ShotSlotPlanningValidationError(
                f"g{index + 1:02d} 无法用真实 TTS 时长生成 {required_count} 个镜头坑位"
            )
        slots: list[dict[str, Any]] = []
        for slot_index, (narration_text, slot_timeline, int_duration) in enumerate(
            zip(narration_parts, timeline_result["timelines"], timeline_result["int_duration"], strict=True),
            start=1,
        ):
            slot_duration_s = (slot_timeline["end"] - slot_timeline["start"]) / 1_000_000
            slots.append(
                {
                    "slot_index": slot_index,
                    "narration_text": narration_text,
                    "timeline": slot_timeline,
                    "duration_s": round(slot_duration_s, 6),
                    "int_duration": int(int_duration),
                }
            )
        groups.append(
            {
                "group_index": index,
                "group_id": f"g{index + 1:02d}",
                "group_timeline": timeline,
                "required_shot_count": required_count,
                "slots": slots,
            }
        )
    return {
        "policy": "tts_timeline_then_semantic_slot_then_visual_design_v1",
        "pacing_plan": pacing_plan,
        "groups": groups,
    }


def _stt_window(value: Mapping[str, Any], field: str) -> tuple[int, int]:
    start = value.get("start_us", value.get("start"))
    end = value.get("end_us", value.get("end"))
    if isinstance(start, bool) or not isinstance(start, int):
        raise ShotSlotPlanningValidationError(f"{field}.start_us 必须是整数微秒")
    if isinstance(end, bool) or not isinstance(end, int) or end <= start:
        raise ShotSlotPlanningValidationError(f"{field}.end_us 必须大于 start_us")
    return start, end


def _join_stt_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if left[-1].isspace() or right[0].isspace():
        return left + right
    return left + right


def _split_text_by_ratio(text: str, ratio: float) -> tuple[str, str]:
    if len(text) < 2:
        raise ShotSlotPlanningValidationError("STT 长字幕文本不足以拆分")
    target = max(1, min(len(text) - 1, round(len(text) * ratio)))
    candidates = [
        index + 1
        for index, char in enumerate(text)
        if char in _PUNCTUATION and 1 <= index + 1 < len(text)
    ]
    if candidates:
        target = min(candidates, key=lambda index: abs(index - target))
    else:
        whitespace = [
            index
            for index, char in enumerate(text)
            if char.isspace() and 1 <= index < len(text) - 1
        ]
        if whitespace:
            target = min(whitespace, key=lambda index: abs(index - target))
    left = text[:target]
    right = text[target:]
    if not left.strip() or not right.strip():
        raise ShotSlotPlanningValidationError("STT 长字幕拆分后出现空文本")
    return left, right


def _merge_stt_atoms(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "text": _join_stt_text(str(left["text"]), str(right["text"])),
        "start_us": int(left["start_us"]),
        "end_us": int(right["end_us"]),
        "source_caption_ids": list(left.get("source_caption_ids") or []) + list(right.get("source_caption_ids") or []),
        "word_boundaries": list(left.get("word_boundaries") or []) + list(right.get("word_boundaries") or []),
    }


def _split_stt_atom(atom: Mapping[str, Any], *, min_us: int, max_us: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    remaining = dict(atom)
    while int(remaining["end_us"]) - int(remaining["start_us"]) > max_us:
        start = int(remaining["start_us"])
        end = int(remaining["end_us"])
        duration = end - start
        target = min(start + max_us, end - min_us)
        boundaries = [
            int(item["end_us"])
            for item in remaining.get("word_boundaries") or []
            if isinstance(item, Mapping)
            and isinstance(item.get("end_us"), int)
            and start + min_us <= int(item["end_us"]) <= target
        ]
        boundary = min(boundaries, key=lambda value: abs(value - target)) if boundaries else target
        if boundary <= start or boundary >= end:
            raise ShotSlotPlanningValidationError("STT 长字幕无法形成合法的 2.5–5 秒镜头坑位")
        left_text, right_text = _split_text_by_ratio(
            str(remaining["text"]), (boundary - start) / duration
        )
        left = {
            "text": left_text,
            "start_us": start,
            "end_us": boundary,
            "source_caption_ids": list(remaining.get("source_caption_ids") or []),
            "word_boundaries": [],
        }
        remaining = {
            "text": right_text,
            "start_us": boundary,
            "end_us": end,
            "source_caption_ids": list(remaining.get("source_caption_ids") or []),
            "word_boundaries": [
                dict(item)
                for item in remaining.get("word_boundaries") or []
                if isinstance(item, Mapping) and int(item.get("end_us", end)) > boundary
            ],
        }
        result.append(left)
    result.append(remaining)
    return result


def plan_stt_caption_shot_slots(
    captions: Sequence[Mapping[str, Any]],
    group_timelines: Sequence[Mapping[str, Any]],
    *,
    min_seconds: float = 2.5,
    max_seconds: float = 5.0,
) -> dict[str, Any]:
    """以 STT 字幕时间片为基础，合并短片段并拆分长片段。

    TTS 大分段是不可跨越的外层边界。只有字幕候选经过本地归一化后，才
    进入镜头细化；字幕本身仍保留 STT 的原始切分和时间线。
    """

    if min_seconds <= 0 or max_seconds < min_seconds:
        raise ShotSlotPlanningValidationError("STT 坑位阈值必须满足 0 < min_seconds <= max_seconds")
    if not captions or not group_timelines:
        raise ShotSlotPlanningValidationError("STT 字幕和 TTS 大分段不能为空")
    min_us = int(round(min_seconds * 1_000_000))
    max_us = int(round(max_seconds * 1_000_000))
    expected_groups = [f"g{index + 1:02d}" for index in range(len(group_timelines))]
    grouped: dict[str, list[dict[str, Any]]] = {group_id: [] for group_id in expected_groups}
    for index, raw_caption in enumerate(captions):
        if not isinstance(raw_caption, Mapping):
            raise ShotSlotPlanningValidationError(f"captions[{index}] 必须是对象")
        group_id = str(raw_caption.get("group_id") or "").strip()
        if group_id not in grouped:
            raise ShotSlotPlanningValidationError(f"captions[{index}] 引用了未知 group_id：{group_id}")
        text = str(raw_caption.get("text") or "").strip()
        if not text:
            raise ShotSlotPlanningValidationError(f"captions[{index}].text 不能为空")
        start, end = _stt_window(raw_caption, f"captions[{index}]")
        grouped[group_id].append({
            "text": text,
            "start_us": start,
            "end_us": end,
            "source_caption_ids": [str(raw_caption.get("caption_id") or f"{group_id}.stt.{index + 1:03d}")],
            "word_boundaries": [
                {"start_us": int(word["start_us"]), "end_us": int(word["end_us"])}
                for word in raw_caption.get("words") or []
                if isinstance(word, Mapping)
                and isinstance(word.get("start_us"), int)
                and isinstance(word.get("end_us"), int)
                and int(word["end_us"]) > int(word["start_us"])
            ],
        })

    groups: list[dict[str, Any]] = []
    all_slots = 0
    total_duration_us = 0
    for group_index, (group_id, raw_group_timeline) in enumerate(zip(expected_groups, group_timelines, strict=True)):
        if not isinstance(raw_group_timeline, Mapping):
            raise ShotSlotPlanningValidationError(f"group_timelines[{group_index}] 必须是对象")
        group_start, group_end = _stt_window(raw_group_timeline, f"group_timelines[{group_index}]")
        atoms = grouped[group_id]
        if not atoms:
            raise ShotSlotPlanningValidationError(f"{group_id} 没有 STT 字幕，不能规划镜头坑位")
        for atom_index, atom in enumerate(atoms):
            if atom["start_us"] < group_start or atom["end_us"] > group_end:
                raise ShotSlotPlanningValidationError(f"{group_id} 的 STT 字幕超出 TTS 大分段边界")
            if atom_index:
                previous = atoms[atom_index - 1]
                boundary = (int(previous["end_us"]) + int(atom["start_us"])) // 2
                previous["end_us"] = boundary
                atom["start_us"] = boundary
        atoms[0]["start_us"] = group_start
        atoms[-1]["end_us"] = group_end

        merged: list[dict[str, Any]] = []
        pending: dict[str, Any] | None = None
        for atom in atoms:
            pending = dict(atom) if pending is None else _merge_stt_atoms(pending, atom)
            if int(pending["end_us"]) - int(pending["start_us"]) >= min_us:
                merged.append(pending)
                pending = None
        if pending is not None:
            if merged:
                merged[-1] = _merge_stt_atoms(merged[-1], pending)
            else:
                merged.append(pending)

        slots: list[dict[str, Any]] = []
        group_duration_us = group_end - group_start
        for atom in merged:
            slots.extend(_split_stt_atom(atom, min_us=min_us, max_us=max_us))
        for slot_index, slot in enumerate(slots, start=1):
            duration_us = int(slot["end_us"]) - int(slot["start_us"])
            allow_short = group_duration_us < min_us
            if duration_us < min_us and not allow_short:
                raise ShotSlotPlanningValidationError(f"{group_id} 仍有低于 {min_seconds:g} 秒的坑位")
            if duration_us > max_us:
                raise ShotSlotPlanningValidationError(f"{group_id} 仍有超过 {max_seconds:g} 秒的坑位")
            slots[slot_index - 1] = {
                "slot_index": slot_index,
                "narration_text": str(slot["text"]),
                "timeline": {"start": int(slot["start_us"]), "end": int(slot["end_us"])},
                "duration_s": round(duration_us / 1_000_000, 6),
                "int_duration": int(math.ceil(duration_us / 1_000_000)),
                "source_caption_ids": list(slot.get("source_caption_ids") or []),
                "allow_short": allow_short,
            }
        all_slots += len(slots)
        total_duration_us += group_duration_us
        groups.append({
            "group_index": group_index,
            "group_id": group_id,
            "group_timeline": {"start": group_start, "end": group_end},
            "required_shot_count": len(slots),
            "slots": slots,
        })
    return {
        "policy": "stt_caption_timeline_merge_below_2_5_split_above_5_v1",
        "thresholds": {"merge_below_seconds": min_seconds, "split_above_seconds": max_seconds},
        "pacing_plan": {
            "policy": "stt_caption_timeline",
            "planned_average_seconds": round(total_duration_us / max(all_slots, 1) / 1_000_000, 6),
            "slot_count": all_slots,
        },
        "groups": groups,
    }


def merge_visual_design_into_locked_slots(
    slots: Sequence[Mapping[str, Any]], visual_result: Mapping[str, Any]
) -> dict[str, Any]:
    """把视觉导演结果写入既有坑位，旁白和时间线始终以冻结数据为准。"""

    raw_shots = visual_result.get("shots") if isinstance(visual_result, Mapping) else None
    if not isinstance(raw_shots, list) or len(raw_shots) != len(slots):
        raise ShotSlotPlanningValidationError("视觉设计输出数量必须与冻结镜头坑位完全一致")
    expected_narration = [_narration_text(slot.get("narration_text"), "slot.narration_text") for slot in slots]
    merged_shots: list[dict[str, Any]] = []
    timelines: list[dict[str, int]] = []
    clip_duration: list[float] = []
    int_duration: list[int] = []
    for index, (slot, visual) in enumerate(zip(slots, raw_shots, strict=True), start=1):
        if not isinstance(visual, Mapping):
            raise ShotSlotPlanningValidationError(f"视觉设计 shots[{index - 1}] 必须是对象")
        timeline = _timeline(slot.get("timeline"), index - 1)
        duration_s = (timeline["end"] - timeline["start"]) / 1_000_000
        merged_shots.append(
            {
                "source_text": _text(visual.get("source_text"), f"shots[{index - 1}].source_text"),
                # 197742 只提供视觉叙事字段。原旁白来自 TTS+语义切分后的
                # 锁定坑位，模型即便漏传、清洗换行或改写 narration_text，
                # 也不能改变字幕、时间轴及后续素材生产的权威文本。
                "narration_text": expected_narration[index - 1],
                "clip_role": _text(visual.get("clip_role"), f"shots[{index - 1}].clip_role"),
                "story_beat": _text(visual.get("story_beat"), f"shots[{index - 1}].story_beat"),
                "clip_duration": round(duration_s, 6),
            }
        )
        timelines.append(timeline)
        clip_duration.append(round(duration_s, 6))
        int_duration.append(int(slot.get("int_duration") or math.ceil(duration_s)))
    return {
        "shots": merged_shots,
        "clip_duration": clip_duration,
        "int_duration": int_duration,
        "timelines": timelines,
    }


__all__ = [
    "ShotSlotPlanningValidationError",
    "merge_visual_design_into_locked_slots",
    "plan_stt_caption_shot_slots",
    "plan_tts_semantic_shot_slots",
    "split_narration_semantically",
]
