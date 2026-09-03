"""“首尾帧顺延”代码节点（170263）的 Python 实现。

实现保持原始 Coze 代码的参数解析、按组切片、首帧/尾帧顺延、字段顺序
和错误文本，不引入外部服务或模型调用。
"""

from __future__ import annotations

import json
from typing import Any

from .first_frame_grid import is_digital_human_shot
from .material_models import normalize_frame_continuity


def _parse_value(value: Any, max_depth: int = 5) -> Any:
    current = value
    for _ in range(max_depth):
        if not isinstance(current, str):
            break
        text = current.strip()
        if not text:
            break
        try:
            parsed = json.loads(text)
        except Exception:
            break
        if parsed == current:
            break
        current = parsed
    return current


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


def _to_duration(value: Any) -> int | float:
    try:
        number = float(value)
    except Exception:
        return 0
    if number <= 0:
        return 0
    if number.is_integer():
        return int(number)
    return round(number, 1)


def _get_seed_text(value: Any) -> str:
    value = _parse_value(value)
    if isinstance(value, dict):
        return _clean_text(
            value.get("motion_seed")
            or value.get("seed")
            or value.get("text")
            or value.get("content")
        )
    return _clean_text(value)


def _get_image_url(value: Any) -> str:
    value = _parse_value(value)
    if isinstance(value, dict):
        return _clean_text(
            value.get("image_url")
            or value.get("url")
            or value.get("link")
            or value.get("image")
            or value.get("src")
        )
    return _clean_text(value)


def _get_llm_shot_count(value: Any) -> int:
    group = _as_dict(value)
    shots = _as_list(group.get("shots"))
    if shots:
        return len(shots)
    return len(_as_list(group.get("items")))


def _get_group_durations(value: Any) -> list[int | float]:
    value = _parse_value(value)
    if isinstance(value, list):
        return [_to_duration(item) for item in value]
    group = _as_dict(value)
    for key in ["clip_duration", "clip_durations", "duration_list", "durations"]:
        durations = _as_list(group.get(key))
        if durations:
            return [_to_duration(item) for item in durations]
    return []


def _resolve_params(args_or_params: Any) -> dict[str, Any]:
    params = getattr(args_or_params, "params", None)
    if not isinstance(params, dict):
        if isinstance(args_or_params, dict):
            params = args_or_params.get("params", args_or_params)
        else:
            params = {}
    if isinstance(params.get("_input"), dict):
        params = params.get("_input")
    return params


def _route_metadata_present(params: dict[str, Any]) -> bool:
    return any(
        key in params
        for key in ("digital_human_shot_indices", "shot_metadata")
    )


def _explicit_digital_indices(value: Any) -> set[int]:
    result: set[int] = set()
    for item in _as_list(value):
        try:
            index = int(item)
        except (TypeError, ValueError):
            continue
        if index >= 0:
            result.add(index)
    return result


def _metadata_at(metadata: Any, index: int) -> Any:
    if isinstance(metadata, list):
        return metadata[index] if 0 <= index < len(metadata) else {}
    if isinstance(metadata, dict):
        return metadata.get(index, metadata.get(str(index), {}))
    return {}


def _shot_records(group: Any) -> list[Any]:
    group = _as_dict(group)
    for key in ("shots", "items", "plans"):
        records = _as_list(group.get(key))
        if records:
            return records
    return []


def _group_digital_flags(
    llm_group: Any,
    code_group: Any,
    shot_count: int,
    global_cursor: int,
    explicit_indices: set[int],
    metadata: Any,
) -> list[bool]:
    llm_shots = _shot_records(llm_group)
    code_shots = _shot_records(code_group)
    flags: list[bool] = []
    for local_index in range(shot_count):
        global_index = global_cursor + local_index
        candidates = []
        if local_index < len(llm_shots):
            candidates.append(llm_shots[local_index])
        if local_index < len(code_shots):
            candidates.append(code_shots[local_index])
        candidates.append(_metadata_at(metadata, global_index))
        flags.append(
            global_index in explicit_indices
            or any(is_digital_human_shot(candidate) for candidate in candidates)
        )
    return flags


def run_end_frame_extension(args_or_params: Any) -> dict[str, Any]:
    params = _resolve_params(args_or_params)
    source_items = _as_list(params.get("items"))
    llm_list = _as_list(params.get("LLM_list"))
    code_list = _as_list(params.get("Code_list"))
    flat_motion_seeds = _as_list(params.get("motion_seed"))
    flat_image_urls = _as_list(params.get("image_url_list"))
    flat_story_context = _as_list(params.get("story_context"))
    route_mode = _route_metadata_present(params)
    explicit_digital_indices = _explicit_digital_indices(
        params.get("digital_human_shot_indices")
    )
    shot_metadata = params.get("shot_metadata", [])
    try:
        frame_continuity_enabled = normalize_frame_continuity(
            params.get("use_frame_continuity")
            if "use_frame_continuity" in params
            else None,
            default=True,
        )
    except ValueError as exc:
        return {
            "items": source_items,
            "motion_seed": [],
            "clip_duration": [],
            "ref_image": [],
            "ref_image_f": [],
            "ref_image_e": [],
            "error": str(exc),
        }

    errors: list[str] = []
    if not source_items:
        errors.append("items 为空或格式不是数组。")
    if not code_list:
        errors.append("Code_list 为空或格式不是数组。")
    if not flat_motion_seeds:
        errors.append("motion_seed 为空或格式不是数组。")
    if not flat_image_urls and not route_mode:
        errors.append("image_url_list 为空或格式不是数组。")
    if source_items and llm_list and len(source_items) != len(llm_list):
        errors.append(f"items 与 LLM_list 组数不一致：{len(source_items)} / {len(llm_list)}。")
    if source_items and code_list and len(source_items) != len(code_list):
        errors.append(f"items 与 Code_list 组数不一致：{len(source_items)} / {len(code_list)}。")

    grouped_motion_seed: list[dict[str, Any]] = []
    grouped_clip_duration: list[dict[str, Any]] = []
    grouped_ref_image: list[dict[str, Any]] = []
    grouped_ref_image_f: list[dict[str, Any]] = []
    grouped_ref_image_e: list[dict[str, Any]] = []
    seed_cursor = 0
    image_cursor = 0
    story_cursor = 0
    global_shot_cursor = 0
    skipped_digital_human_shot_indices: list[int] = []

    # 先确定每组的镜头数和数字人位置，才能区分“全部对齐数组”和“仅普通镜头紧凑数组”。
    group_specs: list[tuple[int, list[bool]]] = []
    total_shots = 0
    total_active_shots = 0
    for group_index, _ in enumerate(source_items):
        llm_group = llm_list[group_index] if group_index < len(llm_list) else {}
        code_group = code_list[group_index] if group_index < len(code_list) else {}
        llm_shot_count = _get_llm_shot_count(llm_group)
        code_durations = _get_group_durations(code_group)
        shot_count = len(code_durations) or llm_shot_count
        flags = _group_digital_flags(
            llm_group,
            code_group,
            shot_count,
            total_shots,
            explicit_digital_indices,
            shot_metadata,
        )
        group_specs.append((shot_count, flags))
        total_shots += shot_count
        total_active_shots += sum(not flag for flag in flags)

    compact_image_mode = (
        route_mode
        and len(flat_image_urls) == total_active_shots
        and len(flat_image_urls) != total_shots
    )

    for group_index, _ in enumerate(source_items):
        llm_group = llm_list[group_index] if group_index < len(llm_list) else {}
        code_group = code_list[group_index] if group_index < len(code_list) else {}
        llm_shot_count = _get_llm_shot_count(llm_group)
        clip_duration = _get_group_durations(code_group)
        duration_shot_count = len(clip_duration)

        if llm_shot_count and duration_shot_count and llm_shot_count != duration_shot_count:
            errors.append(
                f"第 {group_index + 1} 组 shots 数量与 clip_duration 数量不一致："
                f"{llm_shot_count} / {duration_shot_count}。"
            )

        shot_count = duration_shot_count or llm_shot_count
        if shot_count <= 0:
            errors.append(f"第 {group_index + 1} 组未识别到有效 shots 或 clip_duration。")
            grouped_motion_seed.append({"motion_seed": []})
            grouped_clip_duration.append({"clip_duration": []})
            grouped_ref_image.append({"ref_image": []})
            grouped_ref_image_f.append({"ref_image_f": []})
            grouped_ref_image_e.append({"ref_image_e": []})
            global_shot_cursor += shot_count
            continue

        _, digital_flags = group_specs[group_index]

        if flat_story_context:
            group_story_context = flat_story_context[story_cursor:story_cursor + shot_count]
            if len(group_story_context) != shot_count:
                errors.append(
                    f"第 {group_index + 1} 组 story_context 数量不一致："
                    f"{len(group_story_context)} / {shot_count}"
                )
            if not all(isinstance(item, dict) for item in group_story_context):
                errors.append(f"第 {group_index + 1} 组 story_context 必须是对象数组。")
            story_cursor += shot_count

        group_seeds = flat_motion_seeds[seed_cursor:seed_cursor + shot_count]
        seed_cursor += shot_count
        if compact_image_mode:
            active_count = sum(not flag for flag in digital_flags)
            compact_images = flat_image_urls[image_cursor:image_cursor + active_count]
            image_cursor += active_count
            group_images = []
            compact_cursor = 0
            for is_digital in digital_flags:
                if is_digital:
                    group_images.append("")
                else:
                    group_images.append(
                        compact_images[compact_cursor]
                        if compact_cursor < len(compact_images) else ""
                    )
                    compact_cursor += 1
        else:
            group_images = flat_image_urls[image_cursor:image_cursor + shot_count]
            image_cursor += shot_count

        if len(group_seeds) != shot_count:
            errors.append(
                f"第 {group_index + 1} 组需要 {shot_count} 条 motion_seed，"
                f"实际取得 {len(group_seeds)} 条。"
            )
        if len(group_images) != shot_count:
            errors.append(
                f"第 {group_index + 1} 组需要 {shot_count} 张参考图，"
                f"实际取得 {len(group_images)} 张。"
            )

        ref_image = [
            _get_image_url(group_images[shot_index])
            if shot_index < len(group_images) else ""
            for shot_index in range(shot_count)
        ]
        ref_image_f = list(ref_image)
        ref_image_e = []

        # 尾帧数组仍按相邻镜头输出一个位置；数字人相邻处用空字符串断开普通视频连续性。
        if shot_count > 1:
            ref_image_e = [
                ref_image[shot_index + 1]
                if frame_continuity_enabled
                and not digital_flags[shot_index]
                and not digital_flags[shot_index + 1]
                else ""
                for shot_index in range(shot_count - 1)
            ]

        for shot_index, image_url in enumerate(ref_image):
            if not image_url and not digital_flags[shot_index]:
                errors.append(f"第 {group_index + 1} 组第 {shot_index + 1} 条缺少参考图。")
        if len(ref_image_e) != max(shot_count - 1, 0):
            errors.append(
                f"第 {group_index + 1} 组应有 {max(shot_count - 1, 0)} 条尾帧，"
                f"实际取得 {len(ref_image_e)} 条。"
            )

        grouped_motion_seed.append({
            "motion_seed": [
                _get_seed_text(group_seeds[shot_index])
                if shot_index < len(group_seeds) else ""
                for shot_index in range(shot_count)
            ]
        })
        grouped_clip_duration.append({"clip_duration": clip_duration})
        grouped_ref_image.append({"ref_image": ref_image})
        grouped_ref_image_f.append({"ref_image_f": ref_image_f})
        grouped_ref_image_e.append({"ref_image_e": ref_image_e})
        skipped_digital_human_shot_indices.extend(
            global_shot_cursor + shot_index
            for shot_index, is_digital in enumerate(digital_flags)
            if is_digital
        )
        global_shot_cursor += shot_count

    if seed_cursor < len(flat_motion_seeds):
        errors.append(f"存在 {len(flat_motion_seeds) - seed_cursor} 条未被使用的 motion_seed。")
    if image_cursor < len(flat_image_urls):
        errors.append(f"存在 {len(flat_image_urls) - image_cursor} 张未被使用的 image_url。")
    if flat_story_context and story_cursor != len(flat_story_context):
        errors.append(
            f"存在 {len(flat_story_context) - story_cursor} 条未被使用的 story_context。"
        )

    output = {
        "items": source_items,
        "motion_seed": grouped_motion_seed,
        "clip_duration": grouped_clip_duration,
        "ref_image": grouped_ref_image,
        "ref_image_f": grouped_ref_image_f,
        "ref_image_e": grouped_ref_image_e,
        "error": "；".join(errors),
    }
    if route_mode:
        output["skipped_digital_human_shot_indices"] = skipped_digital_human_shot_indices
    if flat_story_context:
        output["story_context"] = [dict(item) for item in flat_story_context if isinstance(item, dict)]
    return output


async def main(args: Any) -> dict[str, Any]:
    return run_end_frame_extension(args)
