"""口播 B-roll 编导 v2 的确定性规划器。

本模块只负责编导决策和可审核脚本，不调用模型、图片、视频或 TTS 服务。
输入可以使用真实 TTS 已切好的大段时间线；输出同时保留 v2 内部审计数据，
并通过 adapter 生成 v1 可消费的 Code_list/LLM_list/timelines。
"""

from __future__ import annotations

import json
import math
import re
import itertools
from collections.abc import Mapping, Sequence
from typing import Any

from ..prompt_generation import run_prompt_generation as run_v1_prompt_generation
from ..story_writer import fallback_opening_title
from ..shot_visual_arrangement import (
    build_prompt as build_v1_frame_prompt,
    fallback_material as build_v1_fallback_material,
)
from .content_model import RuleContentModel, run_content_model_with_retry
from .semantic_model import RuleSemanticModel, run_semantic_model_with_retry
from .story_model import (
    EXPLANATION_VISUAL_TASKS,
    RuleSegmentTypeModel,
    build_segment_types,
    build_story_draft,
    resolve_story_context,
)


class DirectorV2Error(ValueError):
    """v2 输入或规划结果不满足契约。"""


PUNCTUATION = frozenset("，。！？；：、!?;:\n")
HARD_STOP_PUNCTUATION = frozenset("。！？；!?;")
QUOTE_PAIRS = {"“": "”", '"': '"', "「": "」"}
TRAILING_QUOTE_PUNCTUATION = frozenset("。！？；，、：:;,!?")
MIN_SHOT_DURATION_US = 1_500_000
MAX_SHOT_DURATION_US = 5_000_000
OPENING_SHOT_MAX_US = 2_000_000
OPENING_SHOT_TARGET_US = 1_750_000
CONNECTORS = ("但是", "但", "因为", "所以", "如果", "同时", "而", "却", "于是", "不仅", "真正")
PROCESS_MARKERS = ("开始", "正在", "完成", "进入", "离开", "打开", "关闭", "走向", "变成", "逐渐", "然后", "接着", "一边", "多项")
CONTRAST_MARKERS = ("但", "不是", "而是", "以前", "现在", "却", "同时", "与其", "不如", "最好的", "最残酷")
CASE_MARKERS = ("例如", "比如", "你看", "创业者", "一个人就是", "案例")
EXPLANATION_MARKERS = ("因为", "所以", "意味着", "也就是", "说明", "原因", "本质")
EMOTION_MARKERS = ("焦虑", "害怕", "恐慌", "担心", "残酷", "希望", "温度", "共情")
RESULT_MARKERS = ("结果", "最终", "因此", "这就是", "才是", "永远是", "站稳脚跟")
DIRECT_SPEECH_MARKERS = ("你有没有", "你觉得", "你怎么看", "告诉你", "我想说", "我认为", "记住")
CTA_MARKERS = ("评论区", "留言", "告诉我", "你怎么看", "你会怎么")
VIEWPOINT_EMPHASIS_MARKERS = ("真正该", "关键是", "本质是", "记住")
METHOD_MARKERS = ("第一招", "第二招", "第三招", "步骤", "方法", "做法", "诀窍", "技巧", "流程", "演示")
ACTION_MARKERS = (
    "问", "说", "看", "拿", "打开", "关闭", "输入", "整理", "修改", "推", "递", "走", "坐",
    "站", "转身", "抬头", "低头", "敲", "按", "放下", "拿起", "拒绝", "回应", "执行", "操作",
    "展示", "完成", "开始", "进入", "离开", "变成", "移动", "停下", "碰到", "遇到", "打探", "打压",
    "解释", "自证", "追问", "重复", "关心", "拒绝", "闭嘴", "笑",
)
METHOD_CONTEXT_MARKERS = ("你就", "碰到", "遇到", "直接", "全程", "别解释", "别自证")
STATE_TRANSITION_MARKERS = ("从", "到", "变成", "不再", "最终", "结果", "先", "再", "然后", "接着", "最后")
AIGC_DYNAMIC_MARKERS = (
    "动作", "行为", "变化", "切换", "前后", "现场", "场景", "过程", "状态", "移动",
    "走进", "走出", "出现", "消失", "展示", "演示", "对比", "还原", "画面",
)
MEDIA_TYPE_LABELS = {
    "digital_human_video": "数字人",
    "aigc_video": "AIGC",
    "static_image": "图片",
    "explanation_video": "说明动效",
    "overlay_explanation": "叠加说明",
}
REMOTION_TEMPLATES = (
    "text_outline",
    "step_flow",
    "data_compare",
    "quote_highlight",
    "timeline",
    "chart",
)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DirectorV2Error(f"{field} 必须是非空字符串")
    return value


def _timeline(value: Mapping[str, Any], field: str) -> dict[str, int]:
    start, end = value.get("start"), value.get("end")
    if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int):
        raise DirectorV2Error(f"{field} 必须使用整数微秒 start/end")
    if end <= start:
        raise DirectorV2Error(f"{field} 必须满足 end > start")
    return {"start": start, "end": end}


def _duration_us(timeline: Mapping[str, int]) -> int:
    return int(timeline["end"] - timeline["start"])


def _effective_length(value: str) -> int:
    return max(1, sum(1 for char in value if not char.isspace() and char not in PUNCTUATION))


def _spoken_char_count(value: str) -> int:
    """TTS 实际念出的字符数（含标点停顿），镜头时长按它被动分摊。"""

    return max(1, len("".join(value.split())))


def _is_intro_colon(text: str, index: int) -> bool:
    """冒号只在短总起句/方法标题后成为候选切点，避免切断句内解释。"""

    prefix = text[: index + 1].strip()
    suffix = text[index + 1 :].strip()
    return (
        2 <= _effective_length(prefix) <= 22
        and bool(suffix)
        and (
            any(marker in prefix for marker in ("像", "比如", "例如", "第一", "第二", "第三", "招", "情况", "人"))
            or "；" in suffix
            or ";" in suffix
        )
    )


def _semantic_units(text: str) -> list[str]:
    """二级语义段内，按完整意思而不是字数生成三级候选单元。"""

    units: list[str] = []
    start = 0
    quote_char: str | None = None
    pending_cut_index: int | None = None
    for index, char in enumerate(text):
        if quote_char is None and char in QUOTE_PAIRS:
            quote_char = char
            continue
        if quote_char is not None and char == QUOTE_PAIRS[quote_char]:
            # 引号内的句读是语气停顿，不是语义切点；闭合引号后才真正断开。
            if pending_cut_index is not None:
                end = index + 1
                if end < len(text) and text[end] in TRAILING_QUOTE_PUNCTUATION:
                    end += 1
                units.append(text[start:end])
                start = end
                pending_cut_index = None
            quote_char = None
            continue
        if quote_char is not None:
            if char in HARD_STOP_PUNCTUATION:
                pending_cut_index = index
            continue
        should_cut = char in HARD_STOP_PUNCTUATION or (char in "：:" and _is_intro_colon(text, index))
        if not should_cut:
            continue
        part = text[start : index + 1]
        if part.strip():
            units.append(part)
        start = index + 1
    if start < len(text) and text[start:].strip():
        units.append(text[start:])
    if not units:
        return [text]
    # 换行只保留在原文中，不单独制造无旁白镜头。
    return units


def _focus_text(value: str, limit: int = 42) -> str:
    """视觉焦点最多 42 字，优先在标点边界收尾，避免切断语义。"""

    text = value.strip()
    if len(text) <= limit:
        return text
    boundary = max(
        [index for index, char in enumerate(text[:limit]) if char in TRAILING_QUOTE_PUNCTUATION],
        default=None,
    )
    return text[: boundary + 1] if boundary is not None else text[:limit]


def _can_split_for_visual_continuity(text: str) -> int | None:
    """只在同一语义单元内部存在明显的视觉状态转换时，返回可拆的逗号位置。"""

    temporal_openers = ("然后", "接着", "随后", "紧接着", "随即", "这时", "此刻", "最后")
    for index, char in enumerate(text):
        if char not in "，,":
            continue
        left, right = text[: index + 1].strip(), text[index + 1 :].strip()
        if _effective_length(left) < 8 or _effective_length(right) < 8:
            continue
        # 时序连接词开启新的动作阶段，才是画面可切点；转折/因果只改变语气，不改变画面主体。
        if right.startswith(temporal_openers):
            return index
        if _looks_like_visual_action_chain(left) and _looks_like_visual_action_chain(right):
            return index
    return None


def _third_level_shot_parts(text: str, total_duration_us: int) -> list[str]:
    """三级镜头：语义先定候选，标点确认，再按画面连续性拆，时长仅做软复核。"""

    parts = _semantic_units(text)
    # 约 3.5 秒只用来发现“可能需要检查”的长语义单元，不直接按时长切镜。
    total_chars = sum(_spoken_char_count(part) for part in parts)
    result: list[str] = []
    for part in parts:
        estimated_us = round(total_duration_us * _spoken_char_count(part) / total_chars)
        split_at = _can_split_for_visual_continuity(part) if estimated_us > 4_500_000 else None
        if split_at is None:
            result.append(part)
        else:
            result.extend([part[: split_at + 1], part[split_at + 1 :]])
    if "".join(result) != text:
        raise DirectorV2Error("三级镜头分段未完整还原二级原文")
    return result


def _classify_semantic_role(text: str, *, first: bool = False, last: bool = False) -> str:
    value = text.strip()
    if first:
        return "钩子/问题建立"
    if last:
        return "结论/行动引导"
    if any(marker in value for marker in CONTRAST_MARKERS):
        return "观点转折/对比"
    if any(marker in value for marker in CASE_MARKERS):
        return "案例或事实"
    if any(marker in value for marker in EXPLANATION_MARKERS):
        return "观点解释"
    if any(marker in value for marker in RESULT_MARKERS):
        return "结论落点"
    if any(marker in value for marker in EMOTION_MARKERS):
        return "情绪强化"
    return "观点推进"


def _section_name(index: int, count: int, text: str) -> str:
    if index == 0:
        return "片头"
    if index == count - 1:
        return "片尾"
    if any(marker in text for marker in CONTRAST_MARKERS):
        return "观点转折"
    if any(marker in text for marker in CASE_MARKERS):
        return "事实/案例"
    if any(marker in text for marker in EXPLANATION_MARKERS):
        return "观点解释"
    if any(marker in text for marker in RESULT_MARKERS):
        return "结论推进"
    return "主体论述"


def _allocate_tts_durations(
    start: int,
    end: int,
    parts: Sequence[str],
) -> list[dict[str, int]]:
    """按 TTS 大段内文本占比被动推导镜头起止，编导不引入节奏权重或保底时长。"""

    total = end - start
    char_counts = [_spoken_char_count(part) for part in parts]
    char_total = sum(char_counts)
    timelines: list[dict[str, int]] = []
    cursor = start
    cumulative = 0.0
    for index, char_count in enumerate(char_counts):
        if index == len(char_counts) - 1:
            part_end = end
        else:
            cumulative += char_count
            part_end = start + round(total * cumulative / char_total)
            part_end = max(cursor + 1, min(end - (len(char_counts) - index - 1), part_end))
        timelines.append({"start": cursor, "end": part_end})
        cursor = part_end
    return timelines


def _opening_hook_parts(text: str, total_duration_us: int) -> list[str]:
    """片头钩子拆成多个镜头：优先按标点语义边界拆，只有无标点可拆时才按字符兜底。"""

    if total_duration_us < 3_000_000:
        return [text]
    non_space = [index for index, char in enumerate(text) if not char.isspace()]
    chars = len(non_space)
    shot_count = max(2, min(chars, round(total_duration_us / OPENING_SHOT_TARGET_US)))
    while shot_count > 2 and total_duration_us / shot_count < MIN_SHOT_DURATION_US:
        shot_count -= 1
    while shot_count < chars and total_duration_us / shot_count > OPENING_SHOT_MAX_US:
        shot_count += 1
    if shot_count >= chars:
        return [text]

    punctuation_indices = [index for index, char in enumerate(text) if char in TRAILING_QUOTE_PUNCTUATION]
    best_punctuation_cuts: list[int] | None = None
    best_max_duration = total_duration_us
    for size in range(1, min(shot_count, len(punctuation_indices)) + 1):
        for combo in itertools.combinations(punctuation_indices, size):
            cuts = sorted(combo)
            if not cuts or cuts[0] == 0 or cuts[-1] >= len(text) - 1:
                continue
            slices = [text[: cuts[0] + 1]] + [text[cuts[i - 1] + 1 : cuts[i] + 1] for i in range(1, len(cuts))] + [text[cuts[-1] + 1 :]]
            durations = [round(total_duration_us * _spoken_char_count(part) / chars) for part in slices]
            if any(duration < MIN_SHOT_DURATION_US for duration in durations):
                continue
            max_duration = max(durations)
            if max_duration < best_max_duration:
                best_max_duration = max_duration
                best_punctuation_cuts = cuts
    if best_punctuation_cuts is not None:
        parts: list[str] = []
        previous = 0
        for cut in best_punctuation_cuts:
            parts.append(text[previous : cut + 1])
            previous = cut + 1
        parts.append(text[previous:])
        return parts

    boundaries = [round(chars * index / shot_count) for index in range(1, shot_count)]

    def durations_for(cuts: list[int]) -> list[int]:
        lengths = [cuts[0]] + [cuts[index] - cuts[index - 1] for index in range(1, len(cuts))] + [chars - cuts[-1]]
        return [round(total_duration_us * length / chars) for length in lengths]

    def in_range(durations: list[int]) -> bool:
        return all(MIN_SHOT_DURATION_US <= value <= OPENING_SHOT_MAX_US for value in durations)

    options = []
    for cut in boundaries:
        near = [value for value in punctuation_indices if abs(value - cut) <= 2]
        options.append(sorted(set([cut] + near)))
    chosen: list[int] | None = None
    for combo in itertools.product(*options):
        cuts = sorted(set(combo))
        if len(cuts) != shot_count - 1:
            continue
        if in_range(durations_for(cuts)):
            chosen = cuts
            break
    if chosen is None:
        chosen = boundaries

    parts: list[str] = []
    previous = 0
    for boundary in chosen:
        cut_position = non_space[boundary - 1] + 1
        parts.append(text[previous:cut_position])
        previous = cut_position
    parts.append(text[previous:])
    return parts


def _cut_span(span: str, target_chars: int) -> int:
    """在 span 内找最接近 target_chars 的切点，优先标点边界，否则按字符边界。"""

    non_space = [index for index, char in enumerate(span) if not char.isspace()]
    count = len(non_space)
    target = max(1, min(count - 1, target_chars))
    punctuation = [index for index, char in enumerate(span) if char in TRAILING_QUOTE_PUNCTUATION]
    best_cut: int | None = None
    best_distance: int | None = None
    for cut in punctuation:
        prefix_count = sum(1 for index in non_space if index < cut)
        if prefix_count == 0 or prefix_count >= count:
            continue
        distance = abs(prefix_count - target)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_cut = cut
    if best_cut is not None and best_distance <= 3:
        return best_cut + 1
    return non_space[target - 1] + 1


def _fit_shot_duration_bounds(parts: Sequence[str], start: int, end: int, *, min_shot_count: int = 1) -> list[str]:
    """把镜头时长收进 [1.5s, 5s]：先拆超长单元，再贪心打包短单元，时长仍按 TTS 文本占比推导。"""

    total_us = end - start
    char_total = sum(_spoken_char_count(part) for part in parts)

    def duration_of(value: str) -> int:
        return round(total_us * _spoken_char_count(value) / char_total)

    fitted = list(parts)
    while True:
        long_index = next(
            (index for index, part in enumerate(fitted) if duration_of(part) > MAX_SHOT_DURATION_US),
            None,
        )
        if long_index is None:
            break
        span = fitted[long_index]
        target = round(_spoken_char_count(span) / 2)
        cut = _cut_span(span, target)
        fitted = fitted[:long_index] + [span[:cut], span[cut:]] + fitted[long_index + 1 :]

    shots: list[str] = []
    buffer = ""
    processed = 0
    while processed < len(fitted):
        part = fitted[processed]
        processed += 1
        if not buffer:
            buffer = part
            continue
        buffer_us = duration_of(buffer)
        part_us = duration_of(part)
        remaining_after = len(fitted) - processed
        possible_shots_after_merge = len(shots) + 1 + remaining_after
        if buffer_us + part_us <= MAX_SHOT_DURATION_US and possible_shots_after_merge >= min_shot_count:
            buffer += part
            continue
        if buffer_us >= MIN_SHOT_DURATION_US:
            shots.append(buffer)
            buffer = part
            continue
        need_min = MIN_SHOT_DURATION_US - buffer_us
        need_max = MAX_SHOT_DURATION_US - buffer_us
        target_chars = round((need_min + need_max) / 2 * char_total / total_us)
        target_chars = max(1, min(_spoken_char_count(part) - 1, target_chars))
        cut = _cut_span(part, target_chars)
        shots.append(buffer + part[:cut])
        buffer = part[cut:]
    if buffer:
        shots.append(buffer)

    durations = [duration_of(shot) for shot in shots]
    if all(MIN_SHOT_DURATION_US <= value <= MAX_SHOT_DURATION_US for value in durations):
        return shots
    if total_us < MIN_SHOT_DURATION_US:
        return [parts[0]]
    shot_count = max(1, min(len(shots), round(total_us / OPENING_SHOT_TARGET_US)))
    while total_us / shot_count > MAX_SHOT_DURATION_US:
        shot_count += 1
    while shot_count > 1 and total_us / shot_count < MIN_SHOT_DURATION_US:
        shot_count -= 1
    segment_text = "".join(parts)
    plain_indices = [index for index, char in enumerate(segment_text) if not char.isspace()]
    boundaries = [round(len(plain_indices) * index / shot_count) for index in range(1, shot_count)]
    fallback: list[str] = []
    previous = 0
    for boundary in boundaries:
        cut_position = plain_indices[boundary - 1] + 1
        fallback.append(segment_text[previous:cut_position])
        previous = cut_position
    fallback.append(segment_text[previous:])
    return fallback


def _visual_task(text: str) -> str:
    if _looks_like_visual_action_chain(text):
        return "还原案例" if _has_case_or_method_marker(text) else "表现过程"
    if any(marker in text for marker in PROCESS_MARKERS):
        return "表现过程"
    if "AI" in text and sum(text.count(token) for token in ("写", "做", "剪", "回消息")) >= 2:
        return "还原案例"
    if any(marker in text for marker in CONTRAST_MARKERS):
        return "表现对比"
    if any(marker in text for marker in CASE_MARKERS):
        return "还原案例"
    if any(marker in text for marker in EXPLANATION_MARKERS):
        return "解释观点"
    if any(marker in text for marker in EMOTION_MARKERS):
        return "强化情绪"
    if any(marker in text for marker in RESULT_MARKERS):
        return "呈现结果"
    return "直接展示对象"


CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_to_int(value: str) -> int | None:
    if not value or any(char not in CN_DIGITS and char not in CN_UNITS for char in value):
        return None
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
        elif char == "万":
            section = (section + number) * 10000
            total += section
            section = 0
            number = 0
        else:
            section += (number or 1) * CN_UNITS[char]
            number = 0
    total += section + number
    return total


def _extract_numbers(value: str) -> list[dict[str, str]]:
    """从旁白提取可进入图表/对比的数据点（含中文数字），仅做展示用，不改事实。"""

    points: list[dict[str, str]] = []
    units = ("倍", "万", "亿", "年", "小时", "天", "月", "元", "块", "分", "秒", "%", "％")
    for match in re.finditer(
        r"(\d+(?:\.\d+)?|[零一二三四五六七八九十百千万两]+)\s*(" + "|".join(units) + r")?",
        value,
    ):
        raw = match.group(1)
        if not raw:
            continue
        if raw.isdigit():
            number = raw
        else:
            if len(raw) == 1 and not match.group(2):
                # 单个中文数字且无数据单位时大概率是量词（如“一个人”），不作为数据点。
                continue
            converted = _cn_to_int(raw)
            if converted is None:
                continue
            number = str(converted)
        label = value[max(0, match.start() - 6): match.start()].strip().split("，")[-1].split("。")[-1]
        points.append({"label": label[-12:], "value": f"{number}{match.group(2) or ''}"})
    return points


def _extract_quote(value: str) -> str:
    match = re.search(r"[“\"]([^”\"]+)[”\"]", value)
    return match.group(1).strip() if match else ""


def _explanation_spec(text: str, visual_task: str) -> tuple[str, dict[str, Any]]:
    """给说明镜头选 Remotion 模板并生成 props 契约，全部由代码定稿。"""

    value = text.strip()
    focus = _focus_text(value)
    if _extract_quote(value):
        template = "quote_highlight"
    elif _has_case_or_method_marker(value) or any(marker in value for marker in ("第一", "第二", "第三", "首先", "其次", "最后")):
        template = "step_flow"
    elif _extract_numbers(value) and any(marker in value for marker in ("比", "对比", "增长", "下降", "翻倍", "高于", "低于", "倍")):
        template = "data_compare"
    elif _extract_numbers(value):
        template = "chart"
    elif any(marker in value for marker in ("先", "再", "然后", "接着", "随后", "最终", "从", "到")):
        template = "timeline"
    else:
        template = "text_outline"
    points = _extract_numbers(value)
    quote = _extract_quote(value)
    props: dict[str, Any] = {"title": focus}
    if template == "quote_highlight":
        props["quote"] = quote or focus
        props["source"] = focus
    elif template == "step_flow":
        steps = [part.strip() for part in re.split(r"[；;。！？!?]", value) if part.strip()]
        props["steps"] = steps[:8]
    elif template in {"chart", "data_compare"}:
        props["data_points"] = points[:8]
        props["unit"] = ""
    elif template == "timeline":
        events = [part.strip() for part in re.split(r"[；;。！？!?]", value) if part.strip()]
        props["events"] = events[:8]
    else:
        points_list = [part.strip() for part in re.split(r"[；;。！？!?]", value) if part.strip()]
        props["points"] = points_list[:8]
    return template, props


def _shot_explanation_candidate(segment_type: str, visual_task: str) -> bool:
    """说明候选：整段说明段全部候选；混合段只取明确说明型三级小段。"""

    if segment_type == "explanation":
        return True
    if segment_type == "mixed":
        return visual_task in EXPLANATION_VISUAL_TASKS
    return False


def _shot_explanation_mode(
    candidate: bool,
    *,
    digital_human_eligible: bool,
    tier: str | None,
    duration_us: int,
    index: int,
) -> str | None:
    """说明镜头呈现方式偏好：overlay（数字人底+说明层）或 fullscreen（整屏动效）。"""

    if not candidate:
        return None
    if (
        digital_human_eligible
        and tier in {"direct", "audience_address"}
        and duration_us >= 3_000_000
        and index != 0
    ):
        return "overlay"
    return "fullscreen"


def _need_process(text: str, visual_task: str) -> bool:
    return visual_task in {"表现过程", "还原案例"} and _looks_like_visual_action_chain(text)


def _aigc_candidate(text: str, visual_task: str) -> bool:
    """AIGC 候选比“连续多步骤过程”更宽，允许场景还原、对比和动态关系。"""

    value = text.strip()
    if visual_task in {"表现过程", "还原案例", "表现对比"}:
        return True
    if _looks_like_visual_action_chain(value):
        return True
    return any(marker in value for marker in AIGC_DYNAMIC_MARKERS)


def _has_case_or_method_marker(text: str) -> bool:
    return any(marker in text for marker in CASE_MARKERS + METHOD_MARKERS)


def _looks_like_visual_action_chain(text: str) -> bool:
    """识别可被画面还原的行为链，而不是只寻找“然后/完成”等连接词。"""

    value = text.strip()
    action_count = sum(1 for marker in ACTION_MARKERS if marker in value)
    transition_count = sum(1 for marker in STATE_TRANSITION_MARKERS if marker in value)
    has_method = _has_case_or_method_marker(value)
    has_method_context = any(marker in value for marker in METHOD_CONTEXT_MARKERS)
    has_explicit_process = any(marker in value for marker in PROCESS_MARKERS)
    return (
        (action_count >= 2 and transition_count >= 1)
        or (has_method and action_count >= 2)
        or (has_method_context and action_count >= 2)
        or (has_explicit_process and action_count >= 1)
    )


def _digital_human_eligible(
    text: str,
    visual_task: str,
    *,
    is_first: bool,
    is_last: bool,
) -> bool:
    """数字人先取高置信直接表达，目标比例不足时用“你”这类观众直接表达补齐。"""

    value = text.strip()
    if is_first or _looks_like_visual_action_chain(value) or visual_task in {"表现过程", "还原案例"}:
        return False
    if _digital_human_candidate_tier(value, is_last=is_last) == "direct":
        return True
    return "你" in value


def _digital_human_candidate_tier(value: str, *, is_last: bool) -> str:
    value = value.strip()
    direct = any(marker in value for marker in DIRECT_SPEECH_MARKERS)
    question_to_audience = "？" in value and "你" in value
    emphasis = (
        any(marker in value for marker in VIEWPOINT_EMPHASIS_MARKERS)
        and len(value) <= 42
        and not _has_case_or_method_marker(value)
    )
    cta = is_last and any(marker in value for marker in CTA_MARKERS)
    return "direct" if (direct or question_to_audience or emphasis or cta) else "audience_address"


def _raw_media_scores(
    text: str,
    visual_task: str,
    section: str,
    *,
    duration_us: int,
    is_first: bool,
    is_last: bool,
    digital_human_eligible: bool,
    aigc_eligible: bool,
) -> tuple[float, float, float]:
    digital = 4.0
    aigc = 8.0
    image = 24.0
    if digital_human_eligible:
        digital += 52
    if visual_task == "表现过程" or _need_process(text, visual_task):
        aigc += 64
        image -= 10
    if visual_task == "还原案例":
        aigc += 40
    if aigc_eligible:
        aigc += 44
        image -= 12
    if visual_task == "表现对比":
        image += 12
    if visual_task in {"直接展示对象", "强化情绪", "呈现结果"}:
        image += 24
    if not digital_human_eligible:
        digital = min(digital, 8.0)
    if duration_us > 3_000_000:
        released_image_score = image * 2 / 3
        image -= released_image_score
        digital += released_image_score * 0.5
        aigc += released_image_score * 0.5
    elif duration_us > 2_000_000:
        released_image_score = image * 0.5
        image -= released_image_score
        digital += released_image_score * 0.5
        aigc += released_image_score * 0.5
    return max(0.0, digital), max(0.0, aigc), max(0.0, image)


def _normalize_scores(raw: tuple[float, float, float]) -> dict[str, int]:
    total = sum(raw)
    values = [int(round(value / total * 100)) for value in raw]
    values[2] += 100 - sum(values)
    return {"digital_human": values[0], "aigc": values[1], "image": values[2]}


def _select_digital_humans(shots: list[dict[str, Any]], target_ratio: float) -> set[int]:
    total_us = sum(int(shot["timeline"]["end"] - shot["timeline"]["start"]) for shot in shots)
    target_us = total_us * target_ratio
    selected: set[int] = set()
    # 数字人候选 = 非说明镜头的高置信直接表达/观众表达 + 说明镜头中适合叠加的镜头
    # （数字人底 + 说明层，同样计入数字人时长配额）。
    candidates = sorted(
        [
            shot for shot in shots
            if shot["duration_us"] >= 3_000_000
            and shot["index"] != 0
            and (
                (
                    shot["explanation_candidate"]
                    and shot["digital_human_eligible"]
                    and not shot["needs_process"]
                    and shot["digital_human_candidate_tier"] in {"direct", "audience_address"}
                )
                or (
                    not shot["explanation_candidate"]
                    and shot["digital_human_eligible"]
                    and not shot["needs_process"]
                )
            )
        ],
        key=lambda shot: (shot["scores"]["digital_human"], shot["duration_us"]), reverse=True,
    )
    selected_us = 0
    for shot in candidates:
        index = shot["index"]
        if any(abs(index - other) <= 1 for other in selected):
            continue
        if selected_us >= target_us:
            break
        selected.add(index)
        selected_us += shot["duration_us"]
    return selected


def _select_aigc(
    shots: list[dict[str, Any]],
    digital_positions: set[int],
    target_ratio: float | None,
) -> set[int]:
    """固定首镜为 AIGC 后，再从剩余非说明镜头挑选 AIGC。"""

    total_us = sum(int(shot["timeline"]["end"] - shot["timeline"]["start"]) for shot in shots)
    target_us = total_us * target_ratio if target_ratio is not None else None
    candidates = sorted(
        [
            shot for shot in shots
            if shot["index"] not in digital_positions
            and not shot["explanation_candidate"]
            and shot["duration_us"] >= 3_000_000
            and (
                shot["aigc_eligible"]
                or shot["scores"]["aigc"] >= shot["scores"]["image"]
            )
        ],
        key=lambda shot: (shot["scores"]["aigc"], shot["duration_us"]),
        reverse=True,
    )
    # 全片首镜是硬性 AIGC 开场锚点，不受时长和 aigc_target_ratio 影响。
    selected: set[int] = {shots[0]["index"]} if shots else set()
    selected_us = 0
    for shot in candidates:
        if target_us is not None and selected_us >= target_us:
            break
        selected.add(shot["index"])
        selected_us += shot["duration_us"]
    return selected


def _media_type(shot: dict[str, Any], digital_positions: set[int], aigc_positions: set[int]) -> tuple[str, str]:
    if shot["index"] == 0:
        return "aigc_video", "全片首镜硬约束：使用 AIGC 视频建立开场视觉锚点。"
    # 说明段镜头优先定稿：入选数字人配额则叠加说明，否则整屏说明动效。
    # 说明动效不受 v1“低于 3 秒只能静态图片”约束（收口是 Remotion 契约占位）。
    if shot["explanation_candidate"]:
        if shot["index"] in digital_positions:
            return "overlay_explanation", "说明段镜头以数字人出镜为底并叠加说明层，计入数字人配额。"
        return "explanation_video", "说明段镜头使用整屏说明动效（Remotion 模板），不参与数字人/AIGC/图片竞争。"
    # v1 收口的既有硬约束：除首镜外，低于 3 秒只能使用静态图片。
    # 在 v2 分类阶段提前执行，避免用户看到的脚本与下游实际路由不一致。
    if shot["duration_us"] < 3_000_000:
        return "static_image", "镜头低于 3 秒，遵循 v1 收口硬约束使用静态图片。"
    if shot["index"] in digital_positions:
        tier = shot.get("digital_human_candidate_tier")
        if tier == "audience_address":
            return "digital_human_video", "镜头面向观众直接表达，按数字人目标比例补齐入选。"
        return "digital_human_video", "镜头包含高置信直接表达，且在数字人目标时长内入选。"
    if shot["index"] in aigc_positions:
        if shot["scores"]["aigc"] >= shot["scores"]["image"] and not shot["aigc_eligible"]:
            return "aigc_video", "镜头超过 3 秒，图片分减半后由 AIGC 得分胜出，进入 AIGC。"
        return "aigc_video", "数字人配额完成后，镜头具有场景还原、动态关系或视觉变化价值，进入 AIGC。"
    return "static_image", "主要呈现对象、观点状态、情绪或结果，不需要连续过程。"


V1_SHOT_SIZES = ("超远景", "远景", "全景", "中景", "近景", "特写", "极特写")
V1_CARRIER_MODES = ("完整人物", "人物局部", "物件", "空间", "建筑环境")
V1_COMPOSITIONS = (
    "纵向主体—物件关系",
    "边缘压力切入",
    "斜向纵深",
    "遮挡或框景",
    "多人层级",
    "局部焦点与留白",
)
V1_VIEWPOINTS = ("平视广角", "平视正面", "平视侧面", "侧前方平视", "侧面近距离", "俯拍")
V1_CAMERA_MOTIONS = ("固定机位", "轻微横移", "轻微推近", "轻微拉远", "跟拍")


def _direction_from_proposal(shot: Mapping[str, Any], proposal: Mapping[str, Any], media_type: str) -> dict[str, str]:
    """把画面创意层建议落成 v1 视觉字段；白名单外的值一律回退默认，模型不能改契约。"""

    text = str(shot["narration_text"]).strip()
    focus = _focus_text(text)
    return {
        "scene_context": f"围绕旁白“{focus}”对应的真实环境",
        "shot_size": proposal.get("shot_size") if proposal.get("shot_size") in V1_SHOT_SIZES else "中景",
        "carrier_mode": proposal.get("carrier_mode") if proposal.get("carrier_mode") in V1_CARRIER_MODES else "物件",
        "viewpoint": proposal.get("viewpoint") if proposal.get("viewpoint") in V1_VIEWPOINTS else "平视侧面",
        "composition": proposal.get("composition") if proposal.get("composition") in V1_COMPOSITIONS else "局部焦点与留白",
        "visual_focus": focus or "旁白核心信息",
        "camera_motion": proposal.get("camera_motion") if proposal.get("camera_motion") in V1_CAMERA_MOTIONS else "固定机位",
        "camera_fixed": "固定机位" in str(proposal.get("camera_motion") or "固定机位"),
    }


def _v1_frame_prompt(shot: Mapping[str, Any], direction: Mapping[str, Any], material: Mapping[str, Any]) -> str:
    record = {
        "source_text": shot["narration_text"],
        "story_beat": shot["visual_task"],
        "story_context": {},
    }
    return build_v1_frame_prompt(
        dict(direction),
        dict(material),
        False,
        "口播人物",
        str(record["source_text"]),
        str(record["story_beat"]),
        None,
        {},
    )


def _v1_video_prompt(shot: Mapping[str, Any], direction: Mapping[str, Any]) -> tuple[str, bool]:
    """复用 v1 113743 的阶段运镜、时长和 camera_fixed 组装。"""

    duration = max(1, int(math.ceil(shot["duration_us"] / 1_000_000)))
    midpoint = round(duration / 2, 1)
    action = f"围绕{direction['visual_focus']}完成连续状态变化"
    stages = [
        {"time_range": f"0-{midpoint}s", "action": f"建立{action}的起始状态", "camera_motion": direction["camera_motion"]},
        {"time_range": f"{midpoint}-{duration}s", "action": f"延续动作并落到{direction['visual_focus']}", "camera_motion": direction["camera_motion"]},
    ]
    params = {
        "plan_out_list": [{"plans": [{"shot_index": 0, "duration": duration, "stages": stages}], "visual_lock": "保持首帧主体、场景、构图、色彩和光影一致"}],
        "ref_image": [{"ref_image": [f"v2://{shot['shot_id']}/first-frame"]}],
        "clip_duration": [{"clip_duration": [duration]}],
    }
    result = run_v1_prompt_generation(params)
    prompts = result.get("video_prompt") if isinstance(result, Mapping) else []
    fixed = result.get("camera_fixed") if isinstance(result, Mapping) else []
    return (str(prompts[0]) if prompts else "", bool(fixed[0]) if fixed else direction["camera_fixed"])


def _content_material(
    shot: dict[str, Any],
    media_type: str,
    sequence_index: int,
    sequence_count: int,
) -> dict[str, Any]:
    text = shot["narration_text"].strip()
    direction = _direction_from_proposal(shot, shot.get("content_proposal") or {}, media_type)
    if media_type == "digital_human_video":
        # 数字人直接使用已确认的人物资产和对应 TTS，不需要首帧、生图或 AIGC
        # 视频生产材料。保留 v1 下游需要的视觉方向与机位字段，确保镜头坑位、
        # 时间线和数组契约不变。
        result = {
            "v1_visual_direction": {
                "scene_context": direction["scene_context"],
                "shot_size": direction["shot_size"],
                "carrier_mode": direction["carrier_mode"],
                "viewpoint": direction["viewpoint"],
                "composition": direction["composition"],
                "visual_focus": direction["visual_focus"],
                "camera_motion": direction["camera_motion"],
                "camera_fixed": direction["camera_fixed"],
            },
            "camera_motion": direction["camera_motion"],
            "camera_fixed": direction["camera_fixed"],
            "role": "数字人",
            "speaker": "口播人物",
            "appearance_constraints": [
                "沿用已确认的数字人形象，不改变身份锚点",
                "保持发型、服装、年龄、肤色和面部特征一致",
                "不生成新的首帧图片或 AIGC 视频主体",
            ],
            "speech_text": text,
            "speech_action": "按照旁白自然口播，完成口型同步、停顿和必要手势，不追加剧情动作。",
            "expression": "与当前观点一致的清晰表达",
            "framing": f"{direction['shot_size']}，{direction['viewpoint']}，人物面向镜头",
            "sync_requirements": [
                "使用对应 TTS",
                "口型与语音同步",
                "时间线严格覆盖当前镜头坑位",
            ],
        }
        if shot.get("story_context"):
            result["story_context"] = dict(shot["story_context"])
        return result

    record = {"source_text": text, "story_beat": shot["visual_task"], "story_context": {}}
    v1_material = build_v1_fallback_material(record, direction)
    first_frame_prompt = _v1_frame_prompt(shot, direction, v1_material)
    video_prompt, camera_fixed = _v1_video_prompt(shot, direction) if media_type == "aigc_video" else ("", direction["camera_fixed"])
    result = {
        "v1_visual_direction": {
            "scene_context": direction["scene_context"],
            "shot_size": direction["shot_size"],
            "carrier_mode": direction["carrier_mode"],
            "viewpoint": direction["viewpoint"],
            "composition": direction["composition"],
            "visual_focus": direction["visual_focus"],
            "camera_motion": direction["camera_motion"],
            "camera_fixed": camera_fixed,
        },
        "first_frame_material": v1_material,
        "first_frame_prompt": first_frame_prompt,
        "motion_seed": v1_material["motion_seed"],
        "camera_motion": direction["camera_motion"],
        "camera_fixed": camera_fixed,
        "negative": ["无可读文字", "无品牌水印", "无无关人物或剧情"],
    }
    template, props = ("", {})
    if media_type == "explanation_video":
        template, props = _explanation_spec(text, shot["visual_task"])
        result.update({
            "explanation_focus": _focus_text(text),
            "remotion": {"role": "fullscreen", "template_type": template, "props": props},
            "first_frame_prompt": f"说明动效首帧：{template} 模板，聚焦“{_focus_text(text)}”，无实拍人物。",
        })
    elif media_type == "overlay_explanation":
        template, props = _explanation_spec(text, shot["visual_task"])
        result.update({
            "speaker": "口播人物",
            "speech_text": text,
            "expression": "与当前观点一致的清晰表达",
            "framing": f"{direction['shot_size']}，{direction['viewpoint']}，人物面向镜头",
            "sync_requirements": ["使用对应 TTS", "口型与语音同步"],
            "explanation_focus": _focus_text(text),
            "remotion": {
                "role": "overlay",
                "base_media_type": "digital_human_video",
                "template_type": template,
                "props": props,
            },
            "overlay_anchor": "叠加层避开字幕区域，放置在画面安全区。",
        })
    elif media_type == "digital_human_video":
        result.update({
            "speaker": "口播人物",
            "speech_text": text,
            "expression": "与当前观点一致的清晰表达",
            "framing": f"{direction['shot_size']}，{direction['viewpoint']}，人物面向镜头",
            "sync_requirements": ["使用对应 TTS", "口型与语音同步"],
        })
    elif media_type == "aigc_video":
        result.update({
            "subject": "与口播观点对应的主体",
            "scene": direction["scene_context"],
            "start_state": "过程发生前的状态",
            "action_process": "按旁白表现连续变化，不添加旁白之外的新事件",
            "end_state": "旁白所说的结果状态",
            "video_prompt": video_prompt,
            "duration_seconds": round(shot["duration_us"] / 1_000_000, 3),
            "constraints": ["不得生成可读文字、UI、品牌或无关剧情", "保持主体和场景连续"],
        })
    else:
        result.update({
            "subject": "口播观点对应的对象、人物状态或结果",
            "scene": direction["scene_context"],
            "state": "呈现当前语义单元已经成立的状态",
            "image_prompt": first_frame_prompt,
            "style": "统一视频视觉风格，真实、清晰、适合竖屏",
        })
    if shot.get("story_context"):
        result["story_context"] = dict(shot["story_context"])
    return result


def _build_v1_adapter(groups: list[dict[str, Any]]) -> dict[str, Any]:
    code_list: list[dict[str, Any]] = []
    llm_list: list[dict[str, Any]] = []
    for group in groups:
        code_shots = []
        llm_shots = []
        durations: list[float] = []
        int_durations: list[int] = []
        timelines: list[dict[str, int]] = []
        for shot in group["shots"]:
            duration = shot["duration_us"] / 1_000_000
            base = {
                "source_text": shot["visual_description"],
                "narration_text": shot["narration_text"],
                "clip_role": shot["semantic_role"],
                "story_beat": shot["visual_task"],
            }
            # v2 五态媒介与说明契约以附加字段进入 v1 收口，不改变既有字段和数组结构。
            v2_fields = {
                "v2_media_type": shot["media_type"],
                "v2_media_reason": shot["media_reason"],
            }
            if shot["media_type"] in {"explanation_video", "overlay_explanation"}:
                remotion = shot["content_plan"].get("remotion", {})
                v2_fields["v2_remotion"] = {
                    "role": remotion.get("role"),
                    "base_media_type": remotion.get("base_media_type"),
                    "template_type": remotion.get("template_type"),
                    "props": remotion.get("props", {}),
                }
            llm_shots.append({**base, **v2_fields})
            code_shots.append({**base, "clip_duration": round(duration, 6), **v2_fields})
            durations.append(round(duration, 6))
            int_durations.append(int(math.ceil(duration)))
            timelines.append(dict(shot["timeline"]))
        code_list.append({"group_id": group["group_id"], "shots": code_shots, "clip_duration": durations, "int_duration": int_durations, "timelines": timelines})
        llm_list.append({"group_id": group["group_id"], "shots": llm_shots, "reasoning_content": "v2 视觉规划结果已收口；旁白和时间线保持冻结。"})
    return {"LLM_list": llm_list, "Code_list": code_list}


def validate_v1_adapter_output(adapter: Mapping[str, Any], *, expected_shots: int) -> list[str]:
    errors: list[str] = []
    code_list, llm_list = adapter.get("Code_list"), adapter.get("LLM_list")
    if not isinstance(code_list, list) or not isinstance(llm_list, list) or len(code_list) != len(llm_list):
        return ["Code_list 与 LLM_list 必须是等长数组"]
    count = 0
    previous_end: int | None = None
    for group_index, group in enumerate(code_list):
        if not isinstance(group, Mapping):
            errors.append(f"Code_list[{group_index}] 不是对象")
            continue
        fields = ("shots", "clip_duration", "int_duration", "timelines")
        if any(not isinstance(group.get(field), list) for field in fields):
            errors.append(f"Code_list[{group_index}] 缺少数组字段")
            continue
        lengths = {len(group[field]) for field in fields}
        if len(lengths) != 1:
            errors.append(f"Code_list[{group_index}] 内部数组未对齐")
        for timeline in group["timelines"]:
            if not isinstance(timeline, Mapping):
                errors.append("timeline 不是对象")
                continue
            start, end = timeline.get("start"), timeline.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or end <= start:
                errors.append("timeline 不是有效微秒区间")
            if previous_end is not None and start != previous_end:
                errors.append("timelines 存在空洞或重叠")
            previous_end = end
        count += len(group["shots"])
    if count != expected_shots:
        errors.append(f"收口镜头数量 {count} != v2 镜头数量 {expected_shots}")
    return errors


def build_v2_plan(
    *,
    text: str,
    segments: Sequence[str],
    timelines: Sequence[Mapping[str, Any]],
    digital_human_target_ratio: float = 0.25,
    aigc_target_ratio: float | None = None,
    semantic_model: Any = None,
    content_model: Any = None,
    segment_type_model: Any = None,
    story_transport: Any = None,
) -> dict[str, Any]:
    """模型理解 + 代码定稿：模型出语义/画面候选，代码锁时长、媒介与 v1 收口。"""

    source = _text(text, "text")
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)) or not segments:
        raise DirectorV2Error("segments 必须是非空数组")
    if len(segments) != len(timelines):
        raise DirectorV2Error("segments 与 timelines 必须一一对应")
    if not 0 <= digital_human_target_ratio <= 1:
        raise DirectorV2Error("digital_human_target_ratio 必须在 0 到 1 之间")
    if aigc_target_ratio is not None and not 0 <= aigc_target_ratio <= 1:
        raise DirectorV2Error("aigc_target_ratio 必须为空或在 0 到 1 之间")
    # 真实链路中历史导演输出可能对原始文案做过轻微改写；v2 的旁白、TTS
    # 和 v1 收口以 segments 为权威，原始 text 只作为来源记录保留。
    tts_text = "".join(str(item) for item in segments)
    semantic_model = semantic_model or RuleSemanticModel()
    semantic_plan, semantic_meta = run_semantic_model_with_retry(semantic_model, segments, timelines)
    group_visual_tasks = [_visual_task(str(item)) for item in segments]
    segment_type_model = segment_type_model or RuleSegmentTypeModel()
    segment_type_result = build_segment_types(
        segments,
        timelines,
        group_visual_tasks,
        model=segment_type_model,
    )
    segment_types = segment_type_result["segment_types"]
    segment_type_meta = segment_type_result["meta"]
    story_draft = build_story_draft(
        source,
        segments,
        timelines,
        segment_types,
        group_visual_tasks,
        transport=story_transport,
    )

    groups: list[dict[str, Any]] = []
    all_shots: list[dict[str, Any]] = []
    for group_index, (raw_segment, raw_timeline) in enumerate(zip(segments, timelines, strict=True)):
        segment = _text(raw_segment, f"segments[{group_index}]")
        timeline = _timeline(raw_timeline, f"timelines[{group_index}]")
        section = _section_name(group_index, len(segments), segment)
        group_segment_type = (
            "hook"
            if group_index == 0
            else "ending"
            if group_index == len(segments) - 1
            else segment_types.get(f"g{group_index + 1:02d}", "mixed")
        )
        semantic_parts = _semantic_units(segment)
        # 分段优先级：文案语义 > 标点与语气 > 画面连续性 > 约 3.5 秒软目标。
        # 3.5 秒不参与镜头数量计算，也不会单独触发切分。
        plan_units = semantic_plan["segments"][group_index].get("semantic_units", [])
        parts = [str(unit.get("text")) for unit in plan_units if isinstance(unit, Mapping) and unit.get("text")]
        if "".join(parts) != segment:
            parts = _third_level_shot_parts(segment, _duration_us(timeline))
        if group_index == 0 and len(parts) == 1:
            opening_parts = _opening_hook_parts(segment, _duration_us(timeline))
            if len(opening_parts) > 1:
                parts = opening_parts
        # 镜头时长收进 [1.5s, 5s]，仍是 TTS 按文本占比被动推导结果。
        parts = _fit_shot_duration_bounds(
            parts,
            timeline["start"],
            timeline["end"],
            min_shot_count=2 if group_index == 0 else 1,
        )
        part_timelines = _allocate_tts_durations(timeline["start"], timeline["end"], parts)
        group_shots: list[dict[str, Any]] = []
        for slot_index, part in enumerate(parts, start=1):
            part_timeline = part_timelines[slot_index - 1]
            cursor, end = part_timeline["start"], part_timeline["end"]
            is_first = group_index == 0 and slot_index == 1
            is_last = group_index == len(segments) - 1 and slot_index == len(parts)
            semantic_role = _classify_semantic_role(part, first=is_first, last=is_last)
            visual_task = _visual_task(part)
            needs_process = _need_process(part, visual_task)
            digital_human_eligible = _digital_human_eligible(
                part,
                visual_task,
                is_first=is_first,
                is_last=is_last,
            )
            digital_human_candidate_tier = (
                _digital_human_candidate_tier(part, is_last=is_last)
                if digital_human_eligible
                else None
            )
            aigc_eligible = _aigc_candidate(part, visual_task)
            explanation_candidate = _shot_explanation_candidate(group_segment_type, visual_task)
            explanation_mode = _shot_explanation_mode(
                explanation_candidate,
                digital_human_eligible=digital_human_eligible,
                tier=digital_human_candidate_tier,
                duration_us=end - cursor,
                index=len(all_shots),
            )
            shot = {
                "index": len(all_shots),
                "shot_id": f"g{group_index + 1:02d}_s{slot_index:02d}",
                "group_id": f"g{group_index + 1:02d}",
                "section": section,
                "segment_type": group_segment_type,
                "semantic_role": semantic_role,
                "narration_text": part,
                "timeline": {"start": cursor, "end": end},
                "duration_us": end - cursor,
                "visual_task": visual_task,
                "needs_process": needs_process,
                "digital_human_eligible": digital_human_eligible,
                "digital_human_candidate_tier": digital_human_candidate_tier,
                "aigc_eligible": aigc_eligible,
                "explanation_candidate": explanation_candidate,
                "explanation_mode": explanation_mode,
                "long_shot_image_score_adjustment": (end - cursor) > 2_000_000,
                "visual_description": f"围绕“{part.strip()}”完成{visual_task}，画面只服务于口播信息。",
            }
            shot["scores"] = _normalize_scores(
                _raw_media_scores(
                    part,
                    visual_task,
                    section,
                    duration_us=end - cursor,
                    is_first=is_first,
                    is_last=is_last,
                    digital_human_eligible=digital_human_eligible,
                    aigc_eligible=aigc_eligible,
                )
            )
            group_shots.append(shot)
            all_shots.append(shot)
        groups.append({
            "group_id": f"g{group_index + 1:02d}",
            "section": section,
            "segment_type": group_segment_type,
            "segment_text": segment,
            "timeline": {
                "start_us": timeline["start"],
                "end_us": timeline["end"],
                "duration_us": _duration_us(timeline),
            },
            "semantic_segments": [{"text": part, "role": _classify_semantic_role(part)} for part in semantic_parts],
            "shots": group_shots,
        })

    digital_positions = _select_digital_humans(all_shots, float(digital_human_target_ratio))
    aigc_positions = _select_aigc(all_shots, digital_positions, aigc_target_ratio)
    for shot in all_shots:
        media_type, reason = _media_type(shot, digital_positions, aigc_positions)
        shot["media_type"] = media_type
        shot["media_reason"] = reason
    if story_draft is not None:
        story_group_set = set(story_draft["story_group_ids"])
        for shot in all_shots:
            if shot["group_id"] in story_group_set:
                shot["story_context"] = resolve_story_context(shot["group_id"], story_draft)
    content_model = content_model or RuleContentModel()
    content_plan_map, content_meta = run_content_model_with_retry(content_model, all_shots)
    content_proposals = content_plan_map.get("proposals", {})
    for sequence_index, shot in enumerate(all_shots):
        shot["content_proposal"] = content_proposals.get(shot["shot_id"], {})
        shot["content_plan"] = _content_material(shot, shot["media_type"], sequence_index, len(all_shots))
        direction = shot["content_plan"]["v1_visual_direction"]
        shot["visual_plan"] = direction
        if shot["media_type"] == "explanation_video":
            template = shot["content_plan"].get("remotion", {}).get("template_type", "")
            shot["visual_description"] = f"整屏说明动效（{template}）：{_focus_text(shot['narration_text'])}。"
        elif shot["media_type"] == "overlay_explanation":
            template = shot["content_plan"].get("remotion", {}).get("template_type", "")
            shot["visual_description"] = f"数字人画面叠加说明层（{template}）：{_focus_text(shot['narration_text'])}。"
        else:
            shot["visual_description"] = (
                f"{direction['shot_size']}，{direction['viewpoint']}，{direction['composition']}；"
                f"{direction['scene_context']}；视觉重点为{direction['visual_focus']}。"
            )
            if shot.get("story_context"):
                shot["visual_description"] += (
                    f"；故事映射：{shot['story_context'].get('scene_id', '')} {shot['story_context'].get('silent_action', '')}"
                ).rstrip()
    adapter = _build_v1_adapter(groups)
    errors = validate_v1_adapter_output(adapter, expected_shots=len(all_shots))
    if errors:
        raise DirectorV2Error("v1 收口校验失败：" + "；".join(errors))
    user_script = [
        {
            "shot_id": shot["shot_id"],
            "time": {"start_us": shot["timeline"]["start"], "end_us": shot["timeline"]["end"]},
            "narration": shot["narration_text"],
            "media_type": MEDIA_TYPE_LABELS[shot["media_type"]],
            "visual_content": shot["visual_description"],
            "prompt_material": shot["content_plan"],
        }
        for shot in all_shots
    ]
    opening_shots = [shot for shot in all_shots if shot["section"] == "片头"]
    opening_static_hold_ok = (
        not opening_shots
        or (
            len(opening_shots) >= 2
            and all(shot["duration_us"] >= MIN_SHOT_DURATION_US for shot in opening_shots)
        )
    )
    short_route_ok = all(
        shot["media_type"] == "static_image"
        for shot in all_shots
        if shot["index"] != 0
        and shot["duration_us"] < 3_000_000
        and not shot["explanation_candidate"]
    )
    total_duration_us = sum(_duration_us(_timeline(item, f"timelines[{i}]")) for i, item in enumerate(timelines))
    digital_candidate_duration_us = sum(
        shot["duration_us"]
        for shot in all_shots
        if (shot["index"] == 0 or shot["duration_us"] >= 3_000_000)
        and shot["index"] != 0
        and (
            (
                shot["explanation_candidate"]
                and shot["digital_human_eligible"]
                and not shot["needs_process"]
                and shot["digital_human_candidate_tier"] in {"direct", "audience_address"}
            )
            or (
                not shot["explanation_candidate"]
                and shot["digital_human_eligible"]
                and not shot["needs_process"]
            )
        )
    )
    aigc_candidate_duration_us = sum(
        shot["duration_us"]
        for shot in all_shots
        if shot["index"] == 0
        or (
            shot["duration_us"] >= 3_000_000
            and shot["aigc_eligible"]
            and not shot["explanation_candidate"]
            and shot["index"] not in digital_positions
        )
    )
    required_visual_fields = {"shot_size", "carrier_mode", "viewpoint", "composition", "visual_focus"}
    visual_fields_complete = all(required_visual_fields.issubset(shot.get("visual_plan", {})) for shot in all_shots)
    v1_frame_prompt_complete = all(
        bool(shot["content_plan"].get("first_frame_prompt"))
        for shot in all_shots
        if shot["media_type"] != "digital_human_video"
    )
    v1_video_prompt_complete = all(
        bool(shot["content_plan"].get("video_prompt"))
        for shot in all_shots
        if shot["media_type"] == "aigc_video"
    )
    v1_camera_fields_complete = all(
        "camera_motion" in shot["content_plan"]
        and "camera_fixed" in shot["content_plan"]
        and (
            shot["media_type"] == "digital_human_video"
            or "motion_seed" in shot["content_plan"]
        )
        for shot in all_shots
    )
    digital_human_duration_ratio = round(
        sum(
            shot["duration_us"]
            for shot in all_shots
            if shot["media_type"] in {"digital_human_video", "overlay_explanation"}
        )
        / total_duration_us,
        6,
    )
    explanation_duration_ratio = round(
        sum(
            shot["duration_us"]
            for shot in all_shots
            if shot["media_type"] == "explanation_video"
        )
        / total_duration_us,
        6,
    )
    overlay_duration_ratio = round(
        sum(
            shot["duration_us"]
            for shot in all_shots
            if shot["media_type"] == "overlay_explanation"
        )
        / total_duration_us,
        6,
    )
    segment_type_counts = {
        segment_type: sum(
            1
            for group in groups
            if group["segment_type"] == segment_type
        )
        for segment_type in ("hook", "story", "explanation", "mixed", "ending")
    }
    secondary_segment_timelines = [
        {
            "secondary_segment_index": index,
            "shot_index": shot["index"],
            "shot_id": shot["shot_id"],
            "group_id": shot["group_id"],
            "segment_text": shot["narration_text"],
            "start_us": shot["timeline"]["start"],
            "end_us": shot["timeline"]["end"],
            "duration_us": shot["duration_us"],
        }
        for index, shot in enumerate(all_shots)
    ]
    transition_points = [
        {
            "transition_index": index,
            "at_us": current["end_us"],
            "from_shot_id": current["shot_id"],
            "to_shot_id": following["shot_id"],
            "from_group_id": current["group_id"],
            "to_group_id": following["group_id"],
            # 跨二级分段才默认加转场；同一二级分段内的三级镜头默认直切。
            "classification_level": (
                "level_3"
                if current["group_id"] == following["group_id"]
                else "level_2"
            ),
            "default_enabled": current["group_id"] != following["group_id"],
        }
        for index, (current, following) in enumerate(
            zip(secondary_segment_timelines, secondary_segment_timelines[1:], strict=False)
        )
    ]
    return {
        "package": "oral_broll_director_v2",
        "opening_title": (
            str(story_draft.get("story", {}).get("opening_title") or "").strip()
            if isinstance(story_draft, Mapping)
            else ""
        ) or fallback_opening_title(source),
        "policy": {
            "leveling": "dynamic_section_then_semantic_then_on_demand_visual_shots",
            "shot_segmentation_priority": ["copy_semantics", "punctuation_and_tone", "visual_task_and_continuity", "duration_soft_target"],
            "duration_soft_target_seconds": 3.5,
            "media_selection_mode": "explanation_route_then_serial_digital_then_aigc_then_image",
            "segment_type_rule": "rule_first_then_model_for_ambiguous_visual_tasks",
            "digital_human_target_ratio": digital_human_target_ratio,
            "aigc_target_ratio": aigc_target_ratio,
            "media_score_total": 100,
            "long_shot_image_score_rule": "duration_us > 3000000: image_raw_score *= 1/3; duration_us > 2000000: image_raw_score *= 0.5; released_points split equally to digital_human and aigc",
            "first_shot_forbid_digital_human": True,
            "first_shot_must_be_aigc": True,
            "segment_timeline_unit": "microseconds",
            "segment_timeline_source": "shots",
            "group_timeline_unit": "microseconds",
            "group_timeline_source": "input_timelines",
            "secondary_segment_timeline_unit": "microseconds",
            "secondary_segment_timeline_source": "shots",
            "transition_timeline_source": "secondary_segment_timelines",
            "transition_policy": {
                "unit": "microseconds",
                "level_2": {
                    "default_enabled": True,
                    "audio_companion": True,
                },
                "level_3": {
                    "default_enabled": False,
                    "audio_companion": False,
                    "override": "enabled=true 可显式启用",
                },
            },
            "shot_timeline_source": "tts_segment_char_proportion_passive",
            "digital_human_quota_fill": "audience_address_you",
            "explanation_media_types": ["explanation_video", "overlay_explanation"],
            "overlay_rule": "explanation shot with digital-human direct/audience tier and duration>=3s uses digital human base + overlay; others use fullscreen explanation",
            "remotion_templates": list(REMOTION_TEMPLATES),
            "min_shot_duration_us": MIN_SHOT_DURATION_US,
            "max_shot_duration_us": MAX_SHOT_DURATION_US,
            "opening_shot_preferred_max_us": OPENING_SHOT_MAX_US,
            "opening_multi_shot_required": True,
            "opening_split_priority": ["punctuation_semantics", "character_fallback"],
            "story_layer": {
                "enabled": True,
                "coverage": "story_segments_only",
                "origin": story_draft.get("origin") if story_draft is not None else None,
                "story_group_count": len(story_draft["story_group_ids"]) if story_draft is not None else 0,
            },
            "models": {
                "semantic_model": semantic_meta.get("model"),
                "semantic_retried": semantic_meta.get("retried"),
                "semantic_fallback": semantic_meta.get("fallback"),
                "content_model": content_meta.get("model"),
                "content_retried": content_meta.get("retried"),
                "content_fallback": content_meta.get("fallback"),
                "segment_type_model": segment_type_meta.get("model"),
                "segment_type_retried": segment_type_meta.get("retried"),
                "segment_type_fallback": segment_type_meta.get("fallback"),
                "story_model": story_draft.get("origin") if story_draft is not None else "none",
            },
        },
        "source": {"text": source, "tts_text": tts_text, "source_matches_tts_segments": source.replace("\n", "").strip() == tts_text.replace("\n", "").strip(), "group_count": len(segments), "total_duration_us": total_duration_us},
        "group_timelines": [
            {
                "segment_index": index,
                "segment_id": group["group_id"],
                "segment_text": group["segment_text"],
                "start_us": group["timeline"]["start_us"],
                "end_us": group["timeline"]["end_us"],
                "duration_us": group["timeline"]["duration_us"],
            }
            for index, group in enumerate(groups)
        ],
        # segment_timelines 是二级镜头节点；一级旁白/BGM范围使用 group_timelines。
        "segment_timelines": secondary_segment_timelines,
        "secondary_segment_timelines": secondary_segment_timelines,
        "transition_timeline_unit": "microseconds",
        "transition_timelines": secondary_segment_timelines,
        "transition_points": transition_points,
        "groups": groups,
        "shots": all_shots,
        "story_draft": story_draft,
        "v1_adapter_output": adapter,
        "user_video_script": user_script,
        "qa": {
            "v1_adapter_errors": [],
            "shot_count": len(all_shots),
            "digital_human_duration_ratio": digital_human_duration_ratio,
            "digital_human_candidate_duration_ratio": round(digital_candidate_duration_us / total_duration_us, 6),
            "aigc_duration_ratio": round(sum(shot["duration_us"] for shot in all_shots if shot["media_type"] == "aigc_video") / total_duration_us, 6),
            "aigc_candidate_duration_ratio": round(aigc_candidate_duration_us / total_duration_us, 6),
            "explanation_duration_ratio": explanation_duration_ratio,
            "overlay_duration_ratio": overlay_duration_ratio,
            "digital_human_selected_count": len(digital_positions),
            "aigc_selected_count": len(aigc_positions),
            "explanation_selected_count": sum(1 for shot in all_shots if shot["media_type"] == "explanation_video"),
            "overlay_selected_count": sum(1 for shot in all_shots if shot["media_type"] == "overlay_explanation"),
            "segment_type_counts": segment_type_counts,
            "story_draft_present": story_draft is not None,
            "long_shot_image_score_adjustment_count": sum(1 for shot in all_shots if shot["long_shot_image_score_adjustment"]),
            "shot_duration_unique_count": len({shot["duration_us"] for shot in all_shots}),
            "shot_duration_min_us": min(shot["duration_us"] for shot in all_shots),
            "shot_duration_max_us": max(shot["duration_us"] for shot in all_shots),
            "opening_static_hold_ok": opening_static_hold_ok,
            "v1_short_route_ok": short_route_ok,
            "v1_visual_fields_complete": visual_fields_complete,
            "v1_frame_prompt_complete": v1_frame_prompt_complete,
            "v1_video_prompt_complete": v1_video_prompt_complete,
            "v1_camera_fields_complete": v1_camera_fields_complete,
        },
    }


def render_user_video_script_markdown(plan: Mapping[str, Any]) -> str:
    def cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", "<br>")

    def prompt_cell(material: Mapping[str, Any]) -> str:
        direction = material.get("v1_visual_direction", {})
        first_frame = material.get("first_frame_material", {})
        state_or_process = material.get("action_process") or material.get("state") or material.get("speech_text") or ""
        remotion = material.get("remotion")
        if remotion:
            template = remotion.get("template_type", "")
            props = remotion.get("props", {})
            role = "叠加说明层" if remotion.get("role") == "overlay" else "整屏说明动效"
            items = [
                f"{role}：模板 {template}",
                f"props：{json.dumps(props, ensure_ascii=False)[:200]}",
                "完整 Remotion 契约：见 11_story_and_remotion.md",
            ]
            return "<br>".join(cell(item) for item in items)
        if material.get("role") == "数字人":
            direction = material.get("v1_visual_direction", {})
            sync = json.dumps(material.get("sync_requirements", []), ensure_ascii=False)
            items = [
                f"角色：{material.get('role', '')} / {material.get('speaker', '')}",
                f"口播文本：{material.get('speech_text', '')}",
                f"人物外观约束：{'；'.join(material.get('appearance_constraints', []))}",
                f"口播动作：{material.get('speech_action', '')}",
                f"表情：{material.get('expression', '')}",
                f"机位：{material.get('framing', '')}",
                f"运镜：{material.get('camera_motion', direction.get('camera_motion', ''))}",
                f"TTS同步：{sync}",
            ]
            return "<br>".join(cell(item) for item in items)
        items = [
            f"主体：{material.get('subject', '')}",
            f"场景：{material.get('scene', direction.get('scene_context', ''))}",
            f"动作/状态：{state_or_process}",
            f"首帧重点：{first_frame.get('subject_action', '')}",
            f"景别：{direction.get('shot_size', '')}；视角：{direction.get('viewpoint', '')}；构图：{direction.get('composition', '')}",
            f"运镜：{material.get('camera_motion', direction.get('camera_motion', ''))}",
            f"motion_seed：{material.get('motion_seed', '')}",
            "完整首帧/视频提示词：见 10_prompt_material.md",
        ]
        return "<br>".join(cell(item) for item in items if item.split("：", 1)[-1])

    internal_shots = {shot["shot_id"]: shot for shot in plan.get("shots", [])}
    lines = [
        "# 编导 v2 视频脚本",
        "",
        "> 用户审核版：说明段镜头先定稿（整屏说明或数字人叠加说明），其余镜头按数字人 20% → AIGC → 图片串行筛选。",
        "",
        "| 镜头编号 | 时间 | 对应旁白 | 视觉任务 | 画面内容 | 媒介类型 | 景别/视角/构图 | 提示词材料 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for shot in plan.get("user_video_script", []):
        start = shot["time"]["start_us"] / 1_000_000
        end = shot["time"]["end_us"] / 1_000_000
        duration = end - start
        direction = shot["prompt_material"].get("v1_visual_direction", {})
        framing = f"{direction.get('shot_size', '')}；{direction.get('viewpoint', '')}；{direction.get('composition', '')}"
        visual_task = internal_shots.get(shot["shot_id"], {}).get("visual_task", "")
        if shot["media_type"] in {"说明动效", "叠加说明"}:
            remotion = shot["prompt_material"].get("remotion", {})
            framing = f"说明模板：{remotion.get('template_type', '')}"
            if shot["media_type"] == "叠加说明":
                framing += f"；底媒介：{remotion.get('base_media_type', '')}"
        lines.append(
            "| "
            + " | ".join(
                [
                    cell(shot["shot_id"]),
                    cell(f"{start:.3f}s - {end:.3f}s（{duration:.2f}s）"),
                    cell(shot["narration"]),
                    cell(visual_task),
                    cell(shot["visual_content"]),
                    cell(shot["media_type"]),
                    cell(framing),
                    prompt_cell(shot["prompt_material"]),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def render_prompt_material_markdown(plan: Mapping[str, Any]) -> str:
    """输出便于逐镜头检查首帧、图片和视频提示词的 Markdown 表格。"""

    def cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", "<br>")

    lines = [
        "# 编导 v2 提示词材料表",
        "",
        "| 镜头编号 | 媒介类型 | 首帧提示词 | 图片提示词 | 视频提示词 | 运镜 | 固定机位 | 说明契约 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for shot in plan.get("user_video_script", []):
        material = shot["prompt_material"]
        remotion = material.get("remotion")
        remotion_cell = ""
        if remotion:
            props = remotion.get("props", {})
            remotion_cell = (
                f"{remotion.get('role', '')} / {remotion.get('template_type', '')}"
                f"<br>{json.dumps(props, ensure_ascii=False)[:180]}"
            )
        lines.append(
            "| "
            + " | ".join(
                [
                    cell(shot["shot_id"]),
                    cell(shot["media_type"]),
                    cell(material.get("first_frame_prompt", "")),
                    cell(material.get("image_prompt", "")),
                    cell(material.get("video_prompt", "")),
                    cell(material.get("camera_motion", "")),
                    cell(material.get("camera_fixed", "")),
                    cell(remotion_cell),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


__all__ = [
    "DirectorV2Error",
    "build_v2_plan",
    "render_user_video_script_markdown",
    "render_prompt_material_markdown",
    "validate_v1_adapter_output",
]
