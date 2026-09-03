# 8364 节点 192311 BGM 任务组装；代码逐字来源于工作流导出。
# 未对业务代码做重构；仅增加可测试的 Python 调用入口。
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import json
import math


# =========================================================
# BGM 任务组装
#
# 顶级入参：
#   music_cues : 全文视觉意图导演输出
#   timelines  : 粗段落时间线，单位微秒
#
# 顶级出参：
#   bgm_tasks          : 直接给批处理内 gen_bgm 使用
#   bgm_timelines      : 直接传给 merge_bgm_timeline.timelines
#   transition_schemes : 直接传给 merge_bgm_timeline.transition_schemes
# =========================================================


MIN_BGM_DURATION = 30
MAX_BGM_DURATION = 120

# 单首音乐优先上限。后续预留 crossfade 后仍小于 120 秒。
TARGET_CHAPTER_SECONDS = 108

# crossfade 过渡时长；最终会受相邻篇章实际长度保护。
CROSSFADE_SECONDS = 4.0

# fade_gap 时，在章节边界主动留出的无 BGM 空白。
FADE_GAP_SECONDS = 1.2

# 控制 gen_bgm Text 长度。
MAX_PROMPT_LENGTH = 480

# 官方 gen_bgm 的标签字段均为字符串数组。
DEFAULT_GENRE = [
    "documentary"
]

DEFAULT_INSTRUMENT = [
    "piano",
    "synth",
    "percussion"
]

ALLOWED_TRANSITIONS = {
    "crossfade",
    "cut",
    "fade_gap",
    "end_fade"
}


async def main(args: Args) -> Output:
    params = getattr(args, "params", None)

    if not isinstance(params, dict):
        if isinstance(args, dict):
            params = args.get("params", args)
        else:
            params = {}

    # =====================================================
    # 基础工具
    # =====================================================

    def to_plain(value):
        if value is None:
            return None

        if isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, dict):
            return {
                str(key): to_plain(item)
                for key, item in value.items()
            }

        if isinstance(value, (list, tuple, set)):
            return [to_plain(item) for item in value]

        if hasattr(value, "__dict__"):
            return {
                str(key): to_plain(item)
                for key, item in vars(value).items()
            }

        return str(value)

    def parse_json_maybe(value):
        value = to_plain(value)

        if isinstance(value, str):
            text = value.strip()

            if not text:
                return None

            try:
                return json.loads(text)
            except Exception:
                return value

        return value

    def get_param(key, default=None):
        root = to_plain(params)

        if not isinstance(root, dict):
            return default

        if key in root:
            return root.get(key)

        for box_key in [
            "_input",
            "input",
            "inputs",
            "params",
            "body",
            "data",
            "output",
            "result"
        ]:
            box = parse_json_maybe(root.get(box_key))

            if isinstance(box, dict) and key in box:
                return box.get(key)

        return default

    def as_list(value):
        value = parse_json_maybe(value)
        return value if isinstance(value, list) else []

    def as_dict(value):
        value = parse_json_maybe(value)
        return value if isinstance(value, dict) else {}

    def clean_text(value, default=""):
        if value is None:
            return default

        text = str(value).strip()
        return text if text else default

    def safe_int(value, default=0):
        try:
            return int(float(value))
        except Exception:
            return default

    def clamp(value, lower, upper):
        return max(lower, min(upper, value))

    def us_to_seconds(value):
        return round(float(value) / 1000000, 3)

    def seconds_to_us(value):
        return int(round(float(value) * 1000000))

    def second_label(value):
        value = round(float(value), 3)

        if abs(value - round(value)) < 0.001:
            return str(int(round(value)))

        return ("%.3f" % value).rstrip("0").rstrip(".")

    def trim_text(text, limit):
        text = clean_text(text)

        if len(text) <= limit:
            return text

        return text[:limit].rstrip("，。；;、 ") + "。"

    def overlap(start_a, end_a, start_b, end_b):
        return max(start_a, start_b) < min(end_a, end_b)

    # =====================================================
    # 输入标准化
    # =====================================================

    def normalize_timeline(raw):
        item = as_dict(raw)

        start = safe_int(item.get("start"), 0)
        end = safe_int(item.get("end"), 0)

        if end < start:
            end = start

        return {
            "start": start,
            "end": end
        }

    def default_direction(energy):
        if energy <= 1:
            return "保持留白和低能量进入，为后续推进预留空间。"

        if energy == 2:
            return "维持克制底色，缓慢增加环境层次。"

        if energy == 3:
            return "保持统一主音色，稳定增加轻微脉冲与节拍。"

        if energy == 4:
            return "增强低频和节奏密度，持续抬升张力但不要突然爆发。"

        return "强化节拍与低频推动感，在峰值后为下一段预留过渡。"

    def normalize_transition(value):
        transition = clean_text(value).lower()

        if transition in ALLOWED_TRANSITIONS:
            return transition

        # 兼容旧版全文视觉意图导演尚未输出 transition_to_next 的情况。
        return "crossfade"

    def normalize_cue(raw):
        item = as_dict(raw)

        energy = clamp(
            safe_int(item.get("energy"), 3),
            1,
            5
        )

        return {
            "music_role": clean_text(
                item.get("music_role"),
                "推进"
            ),
            "emotion": clean_text(
                item.get("emotion"),
                "克制、稳定、持续推进"
            ),
            "energy": energy,
            "music_direction": clean_text(
                item.get("music_direction"),
                default_direction(energy)
            ),
            "transition_to_next": normalize_transition(
                item.get("transition_to_next")
            )
        }

    # =====================================================
    # 音乐任务参数
    # =====================================================

    def choose_mood(cues):
        if not cues:
            return ["reflective"]

        max_energy = max(item["energy"] for item in cues)
        avg_energy = sum(
            item["energy"]
            for item in cues
        ) / len(cues)

        last_role = clean_text(
            cues[-1].get("music_role"),
            ""
        )

        if max_energy >= 5:
            return ["dramatic", "intense"]

        if max_energy >= 4:
            return ["intense"]

        if last_role in ["合", "收束"]:
            return ["reflective", "contemplative"]

        if avg_energy <= 2:
            return ["calm"]

        return ["contemplative"]

    def get_transition_text_in(scheme, seconds):
        if scheme == "crossfade":
            return (
                f"开头前{second_label(seconds)}秒以环境长音、弱低频和极轻脉冲柔和进入，"
                "承接上一篇章，不要突然起拍。"
            )

        if scheme == "fade_gap":
            return (
                "在前一篇章留白后，从安静的环境底色和稀疏长音重新进入，"
                "保持克制，不要抢占口播。"
            )

        if scheme == "cut":
            return (
                "开头直接进入当前段的核心节奏与情绪，不保留上一篇章的延音。"
            )

        return ""

    def get_transition_text_out(scheme, seconds):
        if scheme == "crossfade":
            return (
                f"最后{second_label(seconds)}秒逐步抽离强节拍和主旋律，"
                "保留延音，为下一篇章自然交叉过渡。"
            )

        if scheme == "fade_gap":
            return (
                f"最后{second_label(seconds)}秒主动渐弱并抽离鼓点，"
                "在章节边界留下短暂安静空间。"
            )

        if scheme == "cut":
            return (
                "结尾干净结束，不保留拖尾和渐弱，为下一篇章直接切入留出明确边界。"
            )

        return (
            "最后3秒克制淡出收束，保留反思余韵，不要胜利式高潮。"
        )

    def build_prompt(
        task_cues,
        task_start,
        task_end,
        task_index,
        task_count,
        transition_in,
        transition_out,
        transition_in_seconds,
        transition_out_seconds
    ):
        if task_count == 1:
            header = (
                "为中文观点口播视频创作无歌词背景纯音乐。"
                "整体保持现代纪录片氛围，以克制钢琴、环境合成器和轻打击乐为主；"
                "不要人声，旋律简洁，不抢口播，不要突然换曲或突兀停顿。"
            )
        else:
            header = (
                f"为同一条中文观点口播视频创作第{task_index}个连续背景音乐篇章。"
                "与前后篇章保持统一的现代纪录片气质，以克制钢琴、环境合成器和轻打击乐为主；"
                "不要人声，旋律简洁，不抢口播，不要突然换曲或突兀停顿。"
            )

        parts = [header]

        intro_text = get_transition_text_in(
            transition_in,
            transition_in_seconds
        )

        if intro_text:
            parts.append(intro_text)

        stage_entries = []

        for cue in task_cues:
            local_start = us_to_seconds(
                max(cue["start"], task_start) - task_start
            )

            local_end = us_to_seconds(
                min(cue["end"], task_end) - task_start
            )

            if local_end <= local_start:
                continue

            stage_entries.append({
                "energy": cue["energy"],
                "text": (
                    f"{second_label(local_start)}至{second_label(local_end)}秒："
                    f"{cue['music_role']}，"
                    f"{trim_text(cue['emotion'], 24)}；"
                    f"{trim_text(cue['music_direction'], 48)}"
                )
            })

        # 提示词过长时，保留开头、能量最高段、结尾。
        if len(stage_entries) > 4:
            strongest_index = max(
                range(len(stage_entries)),
                key=lambda index: stage_entries[index]["energy"]
            )

            keep_indexes = sorted(set([
                0,
                strongest_index,
                len(stage_entries) - 1
            ]))

            stage_entries = [
                stage_entries[index]
                for index in keep_indexes
            ]

        parts.extend(
            item["text"]
            for item in stage_entries
        )

        parts.append(
            get_transition_text_out(
                transition_out,
                transition_out_seconds
            )
        )

        return trim_text(
            "".join(parts),
            MAX_PROMPT_LENGTH
        )

    # =====================================================
    # 读取两个顶级入参
    # =====================================================

    raw_music_cues = as_list(
        get_param("music_cues", [])
    )

    raw_timelines = as_list(
        get_param("timelines", [])
    )

    pair_count = min(
        len(raw_music_cues),
        len(raw_timelines)
    )

    segments = []

    for index in range(pair_count):
        timeline = normalize_timeline(
            raw_timelines[index]
        )

        if timeline["end"] <= timeline["start"]:
            continue

        cue = normalize_cue(
            raw_music_cues[index]
        )

        segments.append({
            "segment_index": index,
            "start": timeline["start"],
            "end": timeline["end"],
            **cue
        })

    if not segments:
        return {
            "bgm_tasks": [],
            "bgm_timelines": [],
            "transition_schemes": []
        }

    segments.sort(
        key=lambda item: (
            item["start"],
            item["end"]
        )
    )

    # =====================================================
    # 按粗段落边界划分音乐篇章
    # =====================================================

    video_start = min(
        item["start"]
        for item in segments
    )

    video_end = max(
        item["end"]
        for item in segments
    )

    target_chapter_us = seconds_to_us(
        TARGET_CHAPTER_SECONDS
    )

    end_boundaries = sorted(set(
        item["end"]
        for item in segments
        if video_start < item["end"] < video_end
    ))

    chapters = []
    current_start = video_start

    while current_start < video_end:
        target_end = min(
            current_start + target_chapter_us,
            video_end
        )

        candidates = [
            boundary
            for boundary in end_boundaries
            if current_start < boundary <= target_end
        ]

        if candidates:
            current_end = max(candidates)
            hard_split = False
        else:
            current_end = target_end
            hard_split = current_end < video_end

        if current_end <= current_start:
            current_end = min(
                current_start + target_chapter_us,
                video_end
            )
            hard_split = current_end < video_end

        boundary_source = None

        if current_end < video_end and not hard_split:
            for segment in segments:
                if segment["end"] == current_end:
                    boundary_source = segment
                    break

        chapters.append({
            "core_start": current_start,
            "core_end": current_end,
            "boundary_source": boundary_source,
            "hard_split": hard_split
        })

        current_start = current_end

    # =====================================================
    # 由 LLM 的 transition_to_next 生成实际融合方案
    # =====================================================

    crossfade_us_default = seconds_to_us(
        CROSSFADE_SECONDS
    )

    fade_gap_us_default = seconds_to_us(
        FADE_GAP_SECONDS
    )

    task_ranges = [
        {
            "start": item["core_start"],
            "end": item["core_end"]
        }
        for item in chapters
    ]

    transition_schemes = []
    transition_seconds = []

    for index in range(len(chapters) - 1):
        previous_chapter = chapters[index]
        next_chapter = chapters[index + 1]

        source = previous_chapter["boundary_source"]

        # 一个粗段落本身超过 108 秒被硬切时，
        # 没有语义边界，默认采用平滑 crossfade。
        if source is None:
            scheme = "crossfade"
        else:
            scheme = source["transition_to_next"]

        if scheme == "end_fade":
            # 非全片结尾不允许 end_fade，自动回退平滑衔接。
            scheme = "crossfade"

        previous_core_duration = (
            previous_chapter["core_end"]
            - previous_chapter["core_start"]
        )

        next_core_duration = (
            next_chapter["core_end"]
            - next_chapter["core_start"]
        )

        if scheme == "crossfade":
            overlap_us = min(
                crossfade_us_default,
                max(0, previous_core_duration // 4),
                max(0, next_core_duration // 4)
            )

            # 极短段落无法承载 crossfade 时，退化为 cut。
            if overlap_us <= 0:
                scheme = "cut"
                transition_seconds.append(0.0)
            else:
                task_ranges[index + 1]["start"] = (
                    next_chapter["core_start"] - overlap_us
                )

                transition_seconds.append(
                    us_to_seconds(overlap_us)
                )

        elif scheme == "fade_gap":
            gap_us = min(
                fade_gap_us_default,
                max(0, previous_core_duration // 4),
                max(0, next_core_duration // 4)
            )

            # 极短段落无法承载留白时，退化为 cut。
            if gap_us <= 0:
                scheme = "cut"
                transition_seconds.append(0.0)
            else:
                task_ranges[index]["end"] = (
                    previous_chapter["core_end"] - gap_us
                )

                transition_seconds.append(
                    us_to_seconds(gap_us)
                )

        else:
            # cut
            scheme = "cut"
            transition_seconds.append(0.0)

        transition_schemes.append(scheme)

    # =====================================================
    # 输出官方 gen_bgm 任务 + 融合插件时间线
    # =====================================================

    bgm_tasks = []
    bgm_timelines = []
    task_count = len(task_ranges)

    for index, task_range in enumerate(task_ranges):
        task_start = task_range["start"]
        task_end = task_range["end"]

        if task_end <= task_start:
            raise RuntimeError(
                f"第 {index + 1} 个 BGM 任务时间范围无效。"
            )

        actual_seconds = us_to_seconds(
            task_end - task_start
        )

        # gen_bgm 必须是 30~120 秒整数。
        # 音乐若长于实际时间线，融合插件会按 bgm_timelines 自动裁剪。
        generation_duration = clamp(
            int(math.ceil(actual_seconds)),
            MIN_BGM_DURATION,
            MAX_BGM_DURATION
        )

        if index == 0:
            transition_in = "none"
            transition_in_seconds = 0.0
        else:
            transition_in = transition_schemes[index - 1]
            transition_in_seconds = transition_seconds[index - 1]

        if index < task_count - 1:
            transition_out = transition_schemes[index]
            transition_out_seconds = transition_seconds[index]
        else:
            transition_out = "end_fade"
            transition_out_seconds = 3.0

        task_cues = [
            item
            for item in segments
            if overlap(
                item["start"],
                item["end"],
                task_start,
                task_end
            )
        ]

        bgm_tasks.append({
            "Duration": generation_duration,
            "Text": build_prompt(
                task_cues=task_cues,
                task_start=task_start,
                task_end=task_end,
                task_index=index + 1,
                task_count=task_count,
                transition_in=transition_in,
                transition_out=transition_out,
                transition_in_seconds=transition_in_seconds,
                transition_out_seconds=transition_out_seconds
            ),
            "Genre": list(DEFAULT_GENRE),
            "Instrument": list(DEFAULT_INSTRUMENT),
            "Mood": choose_mood(task_cues)
        })

        bgm_timelines.append({
            "start": int(task_start),
            "end": int(task_end)
        })

    return {
        "bgm_tasks": bgm_tasks,
        "bgm_timelines": bgm_timelines,
        "transition_schemes": transition_schemes
    }

async def run_bgm_task_assembly_async(params):
    return await main(SimpleNamespace(params=params))

def run_bgm_task_assembly(params):
    return asyncio.run(run_bgm_task_assembly_async(params))

__all__ = ["run_bgm_task_assembly", "run_bgm_task_assembly_async"]
