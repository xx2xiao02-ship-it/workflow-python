# 8364 节点 156199 画面类型识别；代码逐字来源于工作流导出。
# 未对业务代码做重构；仅增加可测试的 Python 调用入口。
import asyncio
from types import SimpleNamespace

import json


HOST_RATIO = 0.20
US_PER_SEC = 1000000

GROUP_EDGE_TOLERANCE_US = 120000
MAX_SPEECH_GAP_US = 900000
MIN_HOST_CANDIDATE_SEC = 1.4
MAX_HOST_CANDIDATE_SEC = 10.5
MAX_CANDIDATES_PER_GROUP = 12


async def main(args):
    params = getattr(args, "params", None)

    if not isinstance(params, dict):
        if isinstance(args, dict):
            params = args.get("params", args)
        else:
            params = {}

    if isinstance(params.get("_input"), dict):
        params = params.get("_input")

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

    def parse_value(value):
        value = to_plain(value)

        if isinstance(value, str):
            text = value.strip()

            if text:
                try:
                    return json.loads(text)
                except Exception:
                    return value

        return value

    def as_dict(value):
        value = parse_value(value)
        return value if isinstance(value, dict) else {}

    def as_list(value):
        value = parse_value(value)
        return value if isinstance(value, list) else []

    def clean_text(value):
        if value is None:
            return ""

        return str(value).strip()

    def to_int(value, default=0):
        try:
            return int(float(value))
        except Exception:
            return default

    def get_param(key, default=None):
        source = to_plain(params)

        if not isinstance(source, dict):
            return default

        if key in source:
            return source.get(key)

        for wrap_key in [
            "input",
            "inputs",
            "params",
            "body",
            "data",
            "output",
            "result"
        ]:
            wrapped = source.get(wrap_key)

            if isinstance(wrapped, dict) and key in wrapped:
                return wrapped.get(key)

        return default

    def norm_time(value):
        item = as_dict(value)

        start = to_int(item.get("start"), 0)
        end = to_int(item.get("end"), 0)

        if end < start:
            end = start

        return {
            "start": start,
            "end": end
        }

    def sec_between(start, end):
        return round((end - start) / US_PER_SEC, 3)

    def overlap_us(left, right):
        start = max(left["start"], right["start"])
        end = min(left["end"], right["end"])
        return max(0, end - start)

    def clean_plan(value):
        raw = as_dict(value)
        plan = {}

        core = clean_text(raw.get("core"))
        spine = clean_text(raw.get("spine"))
        open_text = clean_text(raw.get("open"))

        arc = [
            clean_text(item)
            for item in as_list(raw.get("arc"))
            if clean_text(item)
        ]

        if core:
            plan["core"] = core

        if spine:
            plan["spine"] = spine

        if open_text:
            plan["open"] = open_text

        if arc:
            plan["arc"] = arc

        return plan

    def build_visual_groups(code_list, errors):
        groups = []

        for group_idx, raw_group in enumerate(code_list):
            group = as_dict(raw_group)

            shots = as_list(group.get("shots"))
            shot_times = as_list(group.get("timelines"))

            if len(shots) != len(shot_times):
                errors.append(
                    "Code_list["
                    + str(group_idx)
                    + "] shots 与 timelines 长度不一致"
                )

            count = min(len(shots), len(shot_times))
            group_shots = []

            for shot_idx in range(count):
                shot = as_dict(shots[shot_idx])
                timeline = norm_time(shot_times[shot_idx])

                if timeline["end"] <= timeline["start"]:
                    errors.append(
                        "跳过无效镜头时间：Code_list["
                        + str(group_idx)
                        + "].shots["
                        + str(shot_idx)
                        + "]"
                    )
                    continue

                group_shots.append({
                    "group_idx": group_idx,
                    "shot_idx": shot_idx,
                    "source_text": clean_text(shot.get("source_text")),
                    "clip_role": clean_text(shot.get("clip_role")),
                    "timeline": timeline
                })

            if not group_shots:
                errors.append(
                    "跳过空镜头组：Code_list[" + str(group_idx) + "]"
                )
                continue

            start = min(
                item["timeline"]["start"]
                for item in group_shots
            )
            end = max(
                item["timeline"]["end"]
                for item in group_shots
            )

            groups.append({
                "group_idx": group_idx,
                "timeline": {
                    "start": start,
                    "end": end
                },
                "duration_sec": sec_between(start, end),
                "shots": group_shots,
                "speech_units": []
            })

        groups.sort(
            key=lambda item: (
                item["timeline"]["start"],
                item["timeline"]["end"],
                item["group_idx"]
            )
        )

        return groups

    def build_audio_units(link_list, timelines, errors):
        result = []

        if len(link_list) != len(timelines):
            errors.append(
                "link_list 与 timelines 长度不一致："
                + str(len(link_list))
                + " / "
                + str(len(timelines))
            )

        count = min(len(link_list), len(timelines))

        for idx in range(count):
            audio_url = clean_text(link_list[idx])
            timeline = norm_time(timelines[idx])

            if not audio_url:
                errors.append(
                    "跳过空 link_list[" + str(idx) + "]"
                )
                continue

            if timeline["end"] <= timeline["start"]:
                errors.append(
                    "跳过无效 timelines[" + str(idx) + "]"
                )
                continue

            result.append({
                "audio_idx": idx,
                "audio_url": audio_url,
                "timeline": timeline
            })

        result.sort(
            key=lambda item: (
                item["timeline"]["start"],
                item["timeline"]["end"],
                item["audio_idx"]
            )
        )

        return result

    def build_speech_units(raw_segments, raw_times, errors):
        result = []

        if len(raw_segments) != len(raw_times):
            errors.append(
                "new_segments 与 new_timelines 长度不一致："
                + str(len(raw_segments))
                + " / "
                + str(len(raw_times))
            )

        count = min(len(raw_segments), len(raw_times))

        for raw_idx in range(count):
            text = clean_text(raw_segments[raw_idx])
            timeline = norm_time(raw_times[raw_idx])

            if not text:
                errors.append(
                    "跳过空 new_segments[" + str(raw_idx) + "]"
                )
                continue

            if timeline["end"] <= timeline["start"]:
                errors.append(
                    "跳过无效 new_timelines[" + str(raw_idx) + "]"
                )
                continue

            result.append({
                "raw_idx": raw_idx,
                "text": text,
                "timeline": timeline,
                "duration_sec": sec_between(
                    timeline["start"],
                    timeline["end"]
                )
            })

        result.sort(
            key=lambda item: (
                item["timeline"]["start"],
                item["timeline"]["end"],
                item["raw_idx"]
            )
        )

        return result

    def get_audio_parts(target_time, audio_units):
        parts = []
        cursor = target_time["start"]

        for audio in audio_units:
            audio_time = audio["timeline"]

            if audio_time["end"] <= target_time["start"]:
                continue

            if audio_time["start"] >= target_time["end"]:
                break

            start = max(cursor, audio_time["start"], target_time["start"])
            end = min(target_time["end"], audio_time["end"])

            if end <= start:
                continue

            if start > cursor + GROUP_EDGE_TOLERANCE_US:
                return []

            parts.append({
                "audio_url": audio["audio_url"],
                "audio_time": {
                    "start": start - audio_time["start"],
                    "end": end - audio_time["start"]
                },
                "host_time": {
                    "start": start - target_time["start"],
                    "end": end - target_time["start"]
                }
            })

            cursor = max(cursor, end)

            if cursor >= target_time["end"]:
                break

        if cursor < target_time["end"] - GROUP_EDGE_TOLERANCE_US:
            return []

        return parts

    def choose_group_for_speech(speech, groups):
        matched = []

        for group in groups:
            group_time = group["timeline"]
            overlap = overlap_us(speech["timeline"], group_time)

            if overlap <= 0:
                continue

            fully_inside = (
                speech["timeline"]["start"]
                >= group_time["start"] - GROUP_EDGE_TOLERANCE_US
                and speech["timeline"]["end"]
                <= group_time["end"] + GROUP_EDGE_TOLERANCE_US
            )

            if fully_inside:
                matched.append((overlap, group))

        if not matched:
            return None

        matched.sort(
            key=lambda item: (
                -item[0],
                item[1]["timeline"]["start"],
                item[1]["group_idx"]
            )
        )

        return matched[0][1]

    def strip_tail_marks(text):
        return text.rstrip(" \t\r\n”’\"'）】》〉」』")

    def ends_hard_sentence(text):
        text = strip_tail_marks(text)
        return bool(text) and text[-1] in "。！？!?；;"

    def text_is_likely_continuation(text):
        value = clean_text(text)
        continuation_words = [
            "但",
            "但是",
            "而",
            "而且",
            "所以",
            "因此",
            "于是",
            "然后",
            "同时",
            "因为",
            "如果",
            "当",
            "这时",
            "可",
            "却"
        ]

        return any(
            value.startswith(word)
            for word in continuation_words
        )

    def can_start_candidate(unit, previous_global_unit):
        if previous_global_unit is None:
            return not text_is_likely_continuation(unit["text"])

        if not ends_hard_sentence(previous_global_unit["text"]):
            return False

        return not text_is_likely_continuation(unit["text"])

    def is_continuous(previous_unit, current_unit):
        if current_unit["raw_idx"] != previous_unit["raw_idx"] + 1:
            return False

        gap = (
            current_unit["timeline"]["start"]
            - previous_unit["timeline"]["end"]
        )

        return gap <= MAX_SPEECH_GAP_US

    def join_speech_text(units):
        return "".join(
            clean_text(item["text"])
            for item in units
        )

    def get_cuts(target_time, group_shots):
        cuts = []
        covered_shots = []

        for visual in group_shots:
            shot_time = visual["timeline"]
            start = max(target_time["start"], shot_time["start"])
            end = min(target_time["end"], shot_time["end"])

            if end <= start:
                continue

            cuts.append({
                "group_idx": visual["group_idx"],
                "shot_idx": visual["shot_idx"],
                "overlap_time": {
                    "start": start,
                    "end": end
                }
            })

            covered_shots.append({
                "source_text": visual["source_text"],
                "clip_role": visual["clip_role"]
            })

        return cuts, covered_shots

    def candidate_rank(item):
        duration = item["duration_sec"]
        preferred_distance = abs(duration - 5.5)
        short_penalty = 3 if duration < 3.5 else 0
        return (
            short_penalty,
            preferred_distance,
            -duration,
            item["segment_range"]["start_idx"],
            item["segment_range"]["end_idx"]
        )

    def build_group_candidates(groups, speech_units, audio_units, errors):
        global_by_raw_idx = {
            item["raw_idx"]: item
            for item in speech_units
        }

        for speech in speech_units:
            group = choose_group_for_speech(speech, groups)

            if group is None:
                errors.append(
                    "未纳入镜头组，跳过 Host 候选：new_segments["
                    + str(speech["raw_idx"])
                    + "]"
                )
                continue

            group["speech_units"].append(speech)

        all_candidates = []
        all_maps = []
        candidate_idx = 0

        for group in groups:
            units = sorted(
                group["speech_units"],
                key=lambda item: (
                    item["timeline"]["start"],
                    item["timeline"]["end"],
                    item["raw_idx"]
                )
            )

            group_candidates = []

            for start_pos, start_unit in enumerate(units):
                previous_global = global_by_raw_idx.get(
                    start_unit["raw_idx"] - 1
                )

                if not can_start_candidate(
                    start_unit,
                    previous_global
                ):
                    continue

                selected_units = []

                for end_pos in range(start_pos, len(units)):
                    current_unit = units[end_pos]

                    if selected_units and not is_continuous(
                        selected_units[-1],
                        current_unit
                    ):
                        break

                    selected_units.append(current_unit)

                    candidate_time = {
                        "start": selected_units[0]["timeline"]["start"],
                        "end": selected_units[-1]["timeline"]["end"]
                    }
                    duration_sec = sec_between(
                        candidate_time["start"],
                        candidate_time["end"]
                    )

                    if duration_sec > MAX_HOST_CANDIDATE_SEC:
                        break

                    if not ends_hard_sentence(current_unit["text"]):
                        continue

                    if duration_sec < MIN_HOST_CANDIDATE_SEC:
                        continue

                    audio_parts = get_audio_parts(
                        candidate_time,
                        audio_units
                    )

                    if not audio_parts:
                        errors.append(
                            "候选缺少完整音频，跳过：group "
                            + str(group["group_idx"])
                            + " / segments "
                            + str(selected_units[0]["raw_idx"])
                            + "-"
                            + str(selected_units[-1]["raw_idx"])
                        )
                        continue

                    cuts, covered_shots = get_cuts(
                        candidate_time,
                        group["shots"]
                    )

                    if not covered_shots:
                        errors.append(
                            "候选未覆盖当前镜头组画面，跳过：group "
                            + str(group["group_idx"])
                            + " / segments "
                            + str(selected_units[0]["raw_idx"])
                            + "-"
                            + str(selected_units[-1]["raw_idx"])
                        )
                        continue

                    group_candidates.append({
                        "group_idx": group["group_idx"],
                        "group_duration_sec": group["duration_sec"],
                        "segment_range": {
                            "start_idx": selected_units[0]["raw_idx"],
                            "end_idx": selected_units[-1]["raw_idx"]
                        },
                        "new_segments": join_speech_text(selected_units),
                        "duration_sec": duration_sec,
                        "timeline": candidate_time,
                        "audio_parts": audio_parts,
                        "cuts": cuts,
                        "covered_shots": covered_shots
                    })

            unique = {}
            for item in group_candidates:
                key = (
                    item["segment_range"]["start_idx"],
                    item["segment_range"]["end_idx"]
                )
                unique[key] = item

            group_candidates = list(unique.values())
            group_candidates.sort(key=candidate_rank)
            group_candidates = group_candidates[
                :MAX_CANDIDATES_PER_GROUP
            ]

            group_candidates.sort(
                key=lambda item: (
                    item["segment_range"]["start_idx"],
                    item["segment_range"]["end_idx"]
                )
            )

            for item in group_candidates:
                legacy_audio_url = ""
                legacy_audio_time = {}

                if len(item["audio_parts"]) == 1:
                    legacy_audio_url = item["audio_parts"][0]["audio_url"]
                    legacy_audio_time = item["audio_parts"][0]["audio_time"]

                llm_candidate = {
                    "idx": candidate_idx,
                    "group_idx": item["group_idx"],
                    "group_duration_sec": item["group_duration_sec"],
                    "segment_range": item["segment_range"],
                    "new_segments": item["new_segments"],
                    "duration_sec": item["duration_sec"],
                    "covered_shots": item["covered_shots"]
                }

                map_unit = {
                    "idx": candidate_idx,
                    "group_idx": item["group_idx"],
                    "segment_range": item["segment_range"],
                    "new_segments": item["new_segments"],
                    "duration_sec": item["duration_sec"],
                    "new_timelines": item["timeline"],
                    "audio_url": legacy_audio_url,
                    "audio_time": legacy_audio_time,
                    "audio_parts": item["audio_parts"],
                    "cuts": item["cuts"]
                }

                all_candidates.append(llm_candidate)
                all_maps.append(map_unit)
                candidate_idx += 1

            if units and not group_candidates:
                errors.append(
                    "镜头组 "
                    + str(group["group_idx"])
                    + " 未生成完整 Host 候选，请检查 new_segments 是否保留句末标点且未跨镜头组切断"
                )

        return all_candidates, all_maps

    director_plan = clean_plan(
        get_param("director_plan", {})
    )

    raw_segments = as_list(
        get_param("new_segments", [])
    )

    raw_times = as_list(
        get_param("new_timelines", [])
    )

    code_list = as_list(
        get_param("Code_list", [])
    )

    link_list = as_list(
        get_param("link_list", [])
    )

    timelines = as_list(
        get_param("timelines", [])
    )

    errors = []

    visual_groups = build_visual_groups(
        code_list,
        errors
    )

    audio_units = build_audio_units(
        link_list,
        timelines,
        errors
    )

    speech_units = build_speech_units(
        raw_segments,
        raw_times,
        errors
    )

    total_sec = round(
        sum(
            item["duration_sec"]
            for item in speech_units
        ),
        3
    )

    llm_candidates, map_units = build_group_candidates(
        visual_groups,
        speech_units,
        audio_units,
        errors
    )

    target_sec = round(
        total_sec * HOST_RATIO,
        3
    )

    host_llm_input = {
        "director_plan": director_plan,
        "target_sec": target_sec,
        "candidates": llm_candidates
    }

    host_map = {
        "units": map_units
    }

    return {
        "host_llm_input": host_llm_input,
        "host_map": host_map,
        "error": "；".join(errors)
    }

async def run_scene_type_recognition_async(params):
    return await main(SimpleNamespace(params=params))

def run_scene_type_recognition(params):
    return asyncio.run(run_scene_type_recognition_async(params))

__all__ = ["run_scene_type_recognition", "run_scene_type_recognition_async"]

