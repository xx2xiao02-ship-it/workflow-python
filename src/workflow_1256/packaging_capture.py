"""隔离测试区的 A/B 视频包装参数抓取。

本模块只负责读取两份同源剪映草稿并生成候选抓取结果，不修改原始草稿、
现有包装包或生产运行时。它同时保留两种证据：

* ``raw_diff``：对草稿 JSON 做尽可能完整的递归差异，防止摘要层静默漏项；
* ``semantic_diff``：复用当前包装参数分析器，提取可以进入包装包治理层的差异。

A 是无包装基线，B 是用户手工包装后的草稿。两份草稿应来自同一视频脚本，
镜头顺序和分类应保持一致；内容改动会被列为兼容性警告，不会被伪装成包装参数。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from workflow_1256.video_packaging_bundle import (
    CATEGORY_LABELS,
    VideoPackagingBundleError,
    analyze_full_draft_style,
    diff_full_draft_style,
    load_draft_content,
    normalize_template_timing,
)


PACKAGING_CAPTURE_SCHEMA_VERSION = "packaging-capture-v1"
RAW_DIFF_SCHEMA_VERSION = "packaging-raw-diff-v1"
PARAMETER_CATALOG_SCHEMA_VERSION = "packaging-parameter-catalog-v1"
TIMELINE_UNIT = "microseconds"
DEFAULT_ALIGNMENT_TOLERANCE_US = 250_000
IMAGE_ADJACENCY_TOLERANCE_US = 300_000
DEFAULT_RAW_DIFF_LIMIT = 20_000
DEFAULT_FLOAT_PRECISION = 9
BOUNDARY_PACKAGING_WINDOW_US = 5_000_000
BOUNDARY_PACKAGING_MAX_DURATION_RATIO = 0.4

_PARAMETER_CATEGORY_ORDER = (
    "opening",
    "image",
    "image_sequence",
    "digital_human",
    "aigc",
    "explanation",
    "mixed_explanation",
    "ending",
    "variable",
    "global_audio",
    "narration_audio",
    "global_subtitles",
    "unclassified",
)
_PARAMETER_CATEGORY_LABELS = {
    **CATEGORY_LABELS,
    "image": "图片·单图/少图包装",
    "image_sequence": "图片·三图以上连续共用包装",
    "variable": "变量轨道（文案/音频）",
    "global_audio": "全局音频",
    "narration_audio": "解说音轨",
    "global_subtitles": "全片字幕",
    "unclassified": "未分类（需确认）",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".ts"}
_CONTENT_IMAGE_TYPES = {"photo", "image", "picture", "still", "static_image", "static-image"}
_CONTENT_VIDEO_TYPES = {"video", "movie", "moving_image", "moving-image"}
_PARAMETER_MATERIAL_COLLECTIONS = {
    "video_effects": "effects",
    "effects": "effects",
    "plugin_effects": "effects",
    "filters": "filters",
    "transitions": "transitions",
    "audio_effects": "audio_effects",
    "audio_fades": "audio_effects",
    "text_templates": "text_templates",
    "material_animations": "animations",
    "audios": "audio_materials",
    "videos": "content_materials",
    "images": "content_materials",
    "texts": "text_materials",
}
_TRACK_BINDING_CATEGORIES = set(_PARAMETER_CATEGORY_ORDER)
_TRACK_BINDING_ROLES = {
    "auto",
    "global_effect",
    "category_effect",
    "text_template_variable",
    "audio_variable",
    "subtitle",
    "sticker",
    "audio",
    "ignore",
}
_TEXT_VARIABLE_ROLES = {
    "auto",
    "main_title",
    "subtitle",
}
# 剪映的轨道编号不是 draft_content.json 的数组下标：音频使用负数命名空间，
# 画面、图片、特效、滤镜、贴纸和文字使用非负整数。render_index 越大越靠近
# 前景；它只用于确定界面显示层级，不替代 raw_track_index 的参数回写索引。
_JIANYING_TRACK_RENDER_BASES = {
    "video": 0,
    "image": 0,
    "audio": 0,
    "effect": 10_000,
    "filter": 11_000,
    "sticker": 14_000,
    "text": 15_000,
    "subtitle": 15_000,
}

# 这些字段通常由剪映在复制/保存时重新生成，不能作为包装风格变化。
# 资源身份字段（effect_id/filter_id/transition_id/resource_id）不在这里，
# 否则新包装包会失去可复用资源映射依据。
_VOLATILE_EXACT_KEYS = {
    "id",
    "draft_id",
    "segment_id",
    "material_id",
    "local_material_id",
    "local_id",
    "request_id",
    "create_time",
    "update_time",
    "last_modified_time",
    "created_at",
    "updated_at",
}
_VOLATILE_PATH_TOKENS = ("path", "url", "uri", "token", "secret", "password")
_TRACK_ROLE_VALUES = {
    "bgm",
    "narration",
    "ambience",
    "sfx",
    "transition_sfx",
    "visual_enhancement_sfx",
    "original_audio",
    "unknown",
}


class PackagingCaptureError(ValueError):
    """A/B 抓取输入或输出契约错误。"""


def _text(value: object) -> str:
    return str(value or "").strip()


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _track_render_index(raw_track: Mapping[str, Any]) -> int:
    """读取轨道的实际渲染层级，空轨道按剪映类型默认层级处理。"""

    values: list[int] = []
    for raw_segment in _list(raw_track.get("segments")):
        value = _dict(raw_segment).get("render_index")
        try:
            values.append(int(value))
        except (TypeError, ValueError):
            continue
    if values:
        return max(values)
    track_type = _text(raw_track.get("type") or raw_track.get("track_type")) or "unknown"
    return int(_JIANYING_TRACK_RENDER_BASES.get(track_type, 0))


def _jianying_track_order(raw_tracks: Sequence[Any]) -> dict[int, dict[str, Any]]:
    """为每条原始轨道计算剪映显示编号和上下顺序。

    ``raw_track_index`` 永远对应 JSON 数组位置，供差异和回写使用；
    ``jianying_track_index`` 才是 UI 上展示的轨道编号。正数空间按
    底层到前景分配 0..N，音频空间按原始音频顺序分配 -1,-2…；
    ``ui_order`` 是从剪映界面顶部到底部的显示顺序。
    """

    visual: list[dict[str, Any]] = []
    audio: list[dict[str, Any]] = []
    for raw_index, raw_track in enumerate(raw_tracks):
        track = _dict(raw_track)
        track_type = _text(track.get("type") or track.get("track_type")) or "unknown"
        row = {
            "raw_track_index": raw_index,
            "track_type": track_type,
            "render_index": _track_render_index(track),
        }
        (audio if track_type == "audio" else visual).append(row)

    # 剪映轨道层级以 draft_content.json 的 tracks 原始顺序为准。
    # render_index 是片段/资源的渲染参数，不能拿来重新排列轨道；否则
    # 字幕等原本位于第 5 条视觉轨的内容会被错误推到更高编号。
    visual_bottom_to_top = visual
    order: dict[int, dict[str, Any]] = {}
    visual_count = len(visual_bottom_to_top)
    for position, item in enumerate(visual_bottom_to_top):
        raw_index = int(item["raw_track_index"])
        order[raw_index] = {
            **item,
            "jianying_track_index": position,
            "ui_order": visual_count - position - 1,
        }
    for audio_position, item in enumerate(audio):
        raw_index = int(item["raw_track_index"])
        order[raw_index] = {
            **item,
            "jianying_track_index": -(audio_position + 1),
            "ui_order": visual_count + audio_position,
        }
    return order


def _number_equal(before: object, after: object, precision: int = DEFAULT_FLOAT_PRECISION) -> bool:
    if isinstance(before, bool) or isinstance(after, bool):
        return before == after
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        if not (math.isfinite(float(before)) and math.isfinite(float(after))):
            return before == after
        return round(float(before), precision) == round(float(after), precision)
    return False


def _ignore_raw_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _VOLATILE_EXACT_KEYS or any(token in lowered for token in _VOLATILE_PATH_TOKENS)


def _raw_projection(value: Any, *, key: str = "") -> Any:
    """生成用于全量差异的投影，保留资源参数，排除路径和复制噪声。"""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            child_key = str(raw_key)
            if _ignore_raw_key(child_key):
                continue
            result[child_key] = _raw_projection(raw_value, key=child_key)
        return result
    if isinstance(value, list):
        return [_raw_projection(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_raw_projection(item, key=key) for item in value]
    if isinstance(value, float) and math.isfinite(value):
        return round(value, DEFAULT_FLOAT_PRECISION)
    return copy.deepcopy(value)


def _raw_diff(
    before: Any,
    after: Any,
    *,
    path: str = "",
    changes: list[dict[str, Any]] | None = None,
    limit: int = DEFAULT_RAW_DIFF_LIMIT,
) -> tuple[list[dict[str, Any]], bool]:
    """递归比较投影后的 JSON，返回差异和是否截断。"""

    result = changes if changes is not None else []
    if len(result) >= limit:
        return result, True
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}.{key}" if path else str(key)
            if key not in before:
                result.append({"path": child_path, "kind": "added", "before": None, "after": copy.deepcopy(after[key])})
            elif key not in after:
                result.append({"path": child_path, "kind": "removed", "before": copy.deepcopy(before[key]), "after": None})
            else:
                _raw_diff(before[key], after[key], path=child_path, changes=result, limit=limit)
            if len(result) >= limit:
                return result, True
        return result, False
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child_path = f"{path}[{index}]"
            if index >= len(before):
                result.append({"path": child_path, "kind": "added", "before": None, "after": copy.deepcopy(after[index])})
            elif index >= len(after):
                result.append({"path": child_path, "kind": "removed", "before": copy.deepcopy(before[index]), "after": None})
            else:
                _raw_diff(before[index], after[index], path=child_path, changes=result, limit=limit)
            if len(result) >= limit:
                return result, True
        return result, False
    if before == after or _number_equal(before, after):
        return result, False
    result.append({"path": path, "kind": "changed", "before": copy.deepcopy(before), "after": copy.deepcopy(after)})
    return result, len(result) >= limit


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _draft_content_path(source: str | Path) -> Path:
    path = Path(source).expanduser().resolve()
    if path.is_file():
        if path.name not in {"draft_content.json", "draft_info.json", "draft_meta_info.json", "template-2.tmp"}:
            raise PackagingCaptureError(f"不是可识别的剪映草稿 JSON：{path}")
        return path
    if not path.is_dir():
        raise PackagingCaptureError(f"草稿不存在：{path}")
    for name in ("draft_content.json", "draft_info.json", "draft_meta_info.json", "template-2.tmp"):
        candidate = path / name
        if candidate.is_file():
            return candidate
    raise PackagingCaptureError(f"草稿目录缺少可识别 JSON：{path}")


def _source_manifest(source: str | Path, draft_path: Path, draft: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_path": str(Path(source).expanduser().resolve()),
        "draft_content_path": str(draft_path),
        "draft_content_sha256": _sha256_file(draft_path),
        "draft_name": _text(draft.get("name")) or draft_path.parent.name,
        "duration_us": int(draft.get("duration") or 0),
        "canvas": copy.deepcopy(_dict(draft.get("canvas_config"))),
        "platform": copy.deepcopy(_dict(draft.get("platform"))),
    }


def _count_keyframes(value: Any) -> int:
    if isinstance(value, Mapping):
        return sum(len(_list(child)) if key == "keyframe_list" else _count_keyframes(child) for key, child in value.items())
    if isinstance(value, list):
        return sum(_count_keyframes(child) for child in value)
    return 0


def _catalog_descriptor(value: Any, *, key: str = "") -> Any:
    """为分类清单保留可读参数，排除路径、文案和剪映随机 ID。"""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            child_key = str(raw_key)
            lowered = child_key.lower()
            if _ignore_raw_key(child_key) or lowered in {"content", "text", "words"}:
                continue
            child = _catalog_descriptor(raw_value, key=child_key)
            if child not in (None, "", [], {}):
                result[child_key] = child
        return result
    if isinstance(value, list):
        return [_catalog_descriptor(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_catalog_descriptor(item, key=key) for item in value]
    return copy.deepcopy(value)


def _material_aliases_for_catalog(item: Mapping[str, Any]) -> list[str]:
    return [
        _text(item.get(key))
        for key in ("id", "material_id", "local_material_id", "local_id", "effect_id", "filter_id", "transition_id", "resource_id")
        if _text(item.get(key))
    ]


def _catalog_material_index(materials: Mapping[str, Any]) -> dict[str, tuple[str, Mapping[str, Any]]]:
    index: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for collection, raw_items in materials.items():
        for raw_item in _list(raw_items):
            item = _dict(raw_item)
            for alias in _material_aliases_for_catalog(item):
                index.setdefault(alias, (str(collection), item))
    return index


def _infer_parameter_category(value: object) -> str | None:
    """从剪映轨道名/素材名推断镜头分类；无法确定时返回 None。"""

    text = _text(value)
    if not text:
        return None
    lowered = text.lower().replace("–", "-").replace("—", "-")
    if any(token in text for token in ("片头", "开场")) or any(token in lowered for token in ("opening", "intro", "title-card", "title_card")):
        return "opening"
    if any(token in text for token in ("片尾", "结尾")) or any(token in lowered for token in ("ending", "outro", "end-card", "end_card")):
        return "ending"
    if "数字人" in text or any(token in lowered for token in ("digital-human", "digital_human", "digital human", "digitalhuman")):
        return "digital_human"
    if "混合说明" in text or any(token in lowered for token in ("mixed-explanation", "mixed_explanation", "overlay-explanation", "overlay_explanation")):
        return "mixed_explanation"
    if "说明镜头" in text or "说明" in text or "explanation" in lowered:
        return "explanation"
    if "AIGC" in text.upper() or any(token in lowered for token in ("aigc-video", "aigc_video", "aigc")):
        return "aigc"
    if any(token in text for token in ("图片", "静态图", "首帧", "图像")) or any(token in lowered for token in ("static-image", "static_image", "photo", "picture", "image")):
        suffix = Path(text.split("?", 1)[0]).suffix.lower()
        if suffix in _IMAGE_SUFFIXES or suffix == "" or any(token in text for token in ("图片", "静态图", "首帧", "图像")):
            return "image"
    suffix = Path(text.split("?", 1)[0]).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    return None


def _infer_content_media_type(
    track_type: object,
    material_type: object,
    material_name: object,
) -> str | None:
    """识别内容片段的实际媒体形态，不用轨道名称替代素材类型。"""

    normalized_track_type = _text(track_type).lower()
    if normalized_track_type not in {"video", "image"}:
        return None
    normalized_material_type = _text(material_type).lower().replace("–", "-").replace("—", "-")
    if normalized_material_type in _CONTENT_IMAGE_TYPES:
        return "image"
    if normalized_material_type in _CONTENT_VIDEO_TYPES:
        return "video"
    suffix = Path(_text(material_name).split("?", 1)[0]).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    # image 类型轨道没有素材类型时仍按图片处理；普通 video 轨道只能保守按视频。
    return "image" if normalized_track_type == "image" else "video"


def _infer_content_category(
    track_name: object,
    material_name: object,
    *,
    track_type: object = "",
    material_type: object = "",
) -> str | None:
    """按片段实际媒体形态推断内容分类，再使用轨道语义补充来源分类。

    混合内容轨道可能同时包含独立图片和 AIGC 视频。此时 PNG/Photo 片段归
    图片，Video 片段归 AIGC 视频；不能再用整条轨道名称覆盖片段类型。若剪映
    把视频首帧文件标记为 video，则仍按视频处理，不会被文件后缀误判成图片。
    """

    track_category = _infer_parameter_category(track_name)
    material_category = _infer_parameter_category(material_name)
    media_type = _infer_content_media_type(track_type, material_type, material_name)
    # “AIGC动画”等集合素材轨可同时放 AIGC 视频、数字人视频和独立图片。
    # 数字人是片段级的明确来源标识，必须优先于集合轨的泛化名称；否则
    # digital-human_*.mp4 会被整条 AIGC 轨错误吞并，连同时间重叠的包装
    # 特效也无法归入数字人分类。
    if material_category == "digital_human":
        return "digital_human"
    if track_category == "aigc":
        if media_type == "image":
            return "image"
        return "aigc"
    if track_category == "digital_human":
        return track_category
    return material_category or track_category


def _boundary_packaging_category(
    row: Mapping[str, Any],
    duration_us: int,
) -> str | None:
    """仅为完整落在前后 5 秒、且覆盖不足全片 40% 的元素标记首尾边界。"""

    if duration_us <= 0:
        return None
    start_us = max(0, int(row.get("start_us") or 0))
    end_us = max(start_us, int(row.get("end_us") or start_us))
    if end_us <= start_us:
        return None
    # 近 40% 或更多的长素材是贯穿/内容候选，不能因为触及边界而变成片头或片尾。
    if end_us - start_us >= int(duration_us * BOUNDARY_PACKAGING_MAX_DURATION_RATIO):
        return None
    window_size = min(BOUNDARY_PACKAGING_WINDOW_US, duration_us)
    if start_us >= 0 and end_us <= window_size:
        return "opening"
    if start_us >= max(0, duration_us - window_size) and end_us <= duration_us:
        return "ending"
    return None


def _adjacent_image_sequences(
    raw_tracks: Sequence[Any],
    material_index: Mapping[str, tuple[str, Mapping[str, Any]]],
    *,
    draft_duration_us: int,
) -> list[dict[str, Any]]:
    """识别“连续三图共用同一特效/视觉包装”的图片组合。

    单纯三张图片相邻不够：至少要有一条 effect/filter/sticker 片段同时覆盖
    三张及以上连续图片，才能进入 ``image_sequence``。覆盖全片 80% 以上的
    全局特效（如统一暗角）不作为共用包装依据。
    """

    jianying_order = _jianying_track_order(_list(raw_tracks))
    visual_elements: list[dict[str, Any]] = []
    for visual_track_index, raw_visual_track in enumerate(raw_tracks):
        visual_track = _dict(raw_visual_track)
        visual_track_type = _text(visual_track.get("type") or visual_track.get("track_type"))
        if visual_track_type not in {"effect", "filter", "sticker"}:
            continue
        for visual_segment_index, raw_visual_segment in enumerate(_list(visual_track.get("segments"))):
            visual_row = _segment_parameter_row(
                _dict(raw_visual_segment),
                segment_index=visual_segment_index,
                material_index=material_index,
            )
            visual_start = int(visual_row.get("start_us") or 0)
            visual_end = int(visual_row.get("end_us") or visual_start)
            if visual_end <= visual_start:
                continue
            if draft_duration_us and visual_end - visual_start >= int(draft_duration_us * 0.8):
                continue
            ref_names = [
                _text(_dict(ref).get("name"))
                for ref in _list(visual_row.get("parameter_refs"))
                if _text(_dict(ref).get("name"))
            ]
            visual_elements.append({
                "raw_track_index": visual_track_index,
                "segment_index": visual_segment_index,
                "jianying_track_index": int(jianying_order.get(visual_track_index, {}).get("jianying_track_index", visual_track_index)),
                "track_type": visual_track_type,
                "material_name": _text(visual_row.get("material_name")) or (ref_names[0] if ref_names else "未命名视觉包装"),
                "start_us": visual_start,
                "end_us": visual_end,
            })

    def overlaps_all_images(visual: Mapping[str, Any], images: Sequence[Mapping[str, Any]]) -> bool:
        visual_start = int(visual.get("start_us") or 0)
        visual_end = int(visual.get("end_us") or visual_start)
        return all(
            min(visual_end, int(image.get("end_us") or 0)) + IMAGE_ADJACENCY_TOLERANCE_US
            > max(visual_start, int(image.get("start_us") or 0)) - IMAGE_ADJACENCY_TOLERANCE_US
            for image in images
        )

    sequences: list[dict[str, Any]] = []
    for track_index, raw_track in enumerate(raw_tracks):
        track = _dict(raw_track)
        track_type = _text(track.get("type") or track.get("track_type"))
        if track_type not in {"video", "image"}:
            continue
        fallback_category = _infer_parameter_category(track.get("name"))
        image_rows: list[dict[str, Any]] = []
        for segment_index, raw_segment in enumerate(_list(track.get("segments"))):
            row = _segment_parameter_row(
                _dict(raw_segment),
                segment_index=segment_index,
                material_index=material_index,
            )
            category = _infer_content_category(
                track.get("name"),
                row.get("material_name"),
                track_type=track_type,
                material_type=row.get("material_type"),
            )
            start_us = int(row.get("start_us") or 0)
            end_us = int(row.get("end_us") or start_us)
            if category == "image" and end_us > start_us:
                image_rows.append({
                    "raw_track_index": track_index,
                    "segment_index": segment_index,
                    "jianying_track_index": int(jianying_order.get(track_index, {}).get("jianying_track_index", track_index)),
                    "material_name": _text(row.get("material_name")),
                    "start_us": start_us,
                    "end_us": end_us,
                })
        if len(image_rows) < 3:
            continue
        image_rows.sort(key=lambda item: (int(item["start_us"]), int(item["segment_index"])))
        runs: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for item in image_rows:
            if not current:
                current.append(item)
                continue
            previous = current[-1]
            gap_us = int(item["start_us"]) - int(previous["end_us"])
            if -IMAGE_ADJACENCY_TOLERANCE_US <= gap_us <= IMAGE_ADJACENCY_TOLERANCE_US:
                current.append(item)
            else:
                runs.append(current)
                current = [item]
        if current:
            runs.append(current)
        for run in runs:
            if len(run) < 3:
                continue
            grouped_by_members: dict[str, dict[str, Any]] = {}
            for visual in visual_elements:
                members = [item for item in run if overlaps_all_images(visual, [item])]
                if len(members) < 3:
                    continue
                key = "|".join(str(item["segment_index"]) for item in members)
                group = grouped_by_members.setdefault(key, {"members": members, "shared_visual_elements": []})
                group["shared_visual_elements"].append(visual)
            for group in grouped_by_members.values():
                members = list(group["members"])
                sequences.append({
                    "sequence_id": f"image_sequence_{len(sequences) + 1}",
                    "mode": "image_sequence",
                    "label": "图片·三图以上连续共用包装",
                    "raw_track_index": track_index,
                    "jianying_track_index": int(members[0]["jianying_track_index"]),
                    "time_range_us": {
                        "start": int(members[0]["start_us"]),
                        "end": int(members[-1]["end_us"]),
                    },
                    "member_count": len(members),
                    "members": members,
                    "shared_visual_elements": list(group["shared_visual_elements"]),
                })
    return sequences


def _catalog_material_parameters(collection: str, material: Mapping[str, Any]) -> dict[str, Any]:
    """返回一条不含本地路径/文案的素材参数描述。"""

    if collection in {"videos", "images"}:
        fields = (
            "material_name", "name", "type", "category", "duration", "width", "height",
            "has_audio", "fps", "rotation", "speed", "crop", "transform", "source_timerange",
        )
    elif collection == "texts":
        fields = (
            "name", "type", "font_name", "font_title", "font_category_name", "font_size", "text_size",
            "alignment", "typesetting", "line_spacing", "letter_spacing", "bold_width", "italic_degree",
            "underline", "border_alpha", "border_color", "border_width", "background_alpha",
            "background_color", "background_height", "background_width", "has_shadow", "shadow_alpha",
            "shadow_color", "shadow_distance", "shadow_smoothing", "style_name", "preset_name",
            "preset_category", "text_preset_resource_id",
        )
    else:
        fields = (
            "name", "effect_name", "effect_id", "filter_id", "transition_id", "resource_id",
            "category_id", "category_name", "type", "value", "intensity", "duration",
            "apply_target_type", "render_index", "track_render_index", "adjust_params",
            "apply_time_range", "time_range", "fade_in_duration", "fade_out_duration", "volume",
        )
    selected = {key: copy.deepcopy(material[key]) for key in fields if key in material}
    return _catalog_descriptor(selected)


def _catalog_parameter_ref(
    alias: object,
    material_index: Mapping[str, tuple[str, Mapping[str, Any]]],
) -> dict[str, Any] | None:
    key = _text(alias)
    if not key or key not in material_index:
        return None
    collection, material = material_index[key]
    descriptor = _catalog_material_parameters(collection, material)
    if collection == "text_templates":
        actual_texts: list[str] = []
        for raw_info in _list(material.get("text_info_resources")):
            info = _dict(raw_info)
            text_material_id = _text(info.get("text_material_id"))
            if not text_material_id:
                continue
            text_entry = material_index.get(text_material_id)
            if not text_entry or text_entry[0] != "texts":
                continue
            content = _text(text_entry[1].get("content"))
            if content and content not in actual_texts:
                actual_texts.append(content)
        if actual_texts:
            descriptor["actual_text"] = actual_texts[0] if len(actual_texts) == 1 else actual_texts
    name = _text(
        material.get("material_name")
        or material.get("name")
        or material.get("effect_name")
        or material.get("effect_id")
        or material.get("resource_id")
    )
    return {
        "collection": collection,
        "parameter_group": _PARAMETER_MATERIAL_COLLECTIONS.get(collection, collection),
        "name": name,
        "parameters": descriptor,
    }


def _segment_parameter_row(
    segment: Mapping[str, Any],
    *,
    segment_index: int,
    material_index: Mapping[str, tuple[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    timerange = _dict(segment.get("target_timerange"))
    start_us = max(0, int(timerange.get("start") or 0))
    duration_us = max(0, int(timerange.get("duration") or 0))
    aliases: list[str] = []
    for key in (
        "material_id", "effect_id", "filter_id", "transition_id", "transition_material_id",
        "audio_effect_id", "text_template_id", "animation_id",
    ):
        value = _text(segment.get(key))
        if value:
            aliases.append(value)
    aliases.extend(_text(ref) for ref in _list(segment.get("extra_material_refs")) if _text(ref))
    refs: list[dict[str, Any]] = []
    seen_refs: set[tuple[str, str]] = set()
    for alias in aliases:
        ref = _catalog_parameter_ref(alias, material_index)
        if ref is None:
            continue
        dedupe_key = (_text(ref.get("collection")), _text(ref.get("name")))
        if dedupe_key in seen_refs:
            continue
        seen_refs.add(dedupe_key)
        refs.append(ref)

    groups: list[dict[str, Any]] = []
    property_types: list[str] = []
    point_count = 0
    for raw_group in _list(segment.get("common_keyframes")):
        group = _dict(raw_group)
        property_type = _text(group.get("property_type")) or "unknown"
        points = _list(group.get("keyframe_list"))
        point_count += len(points)
        property_types.append(property_type)
        groups.append({
            "property_type": property_type,
            "point_count": len(points),
            "points": [
                {
                    "time_offset_us": int(_dict(point).get("time_offset") or 0),
                    "values": copy.deepcopy(_dict(point).get("values")),
                }
                for point in points
                if isinstance(point, Mapping)
            ],
        })

    material_name = ""
    material_type = ""
    material_ref = _catalog_parameter_ref(segment.get("material_id"), material_index)
    if material_ref:
        material_name = _text(material_ref.get("name"))
        material_parameters = _dict(material_ref.get("parameters"))
        material_type = _text(material_parameters.get("type") or material_parameters.get("category"))
    row: dict[str, Any] = {
        "segment_index": segment_index,
        "start_us": start_us,
        "duration_us": duration_us,
        "end_us": start_us + duration_us,
        "material_id_present": bool(_text(segment.get("material_id"))),
        "material_name": material_name,
        "material_type": material_type,
        "parameter_refs": refs,
        "keyframes": {
            "group_count": len(groups),
            "point_count": point_count,
            "property_types": sorted(set(property_types)),
            "groups": groups,
        },
    }
    for key in ("volume", "speed", "visible", "enable_adjust", "enable_lut", "uniform_scale", "opacity"):
        if key in segment and segment.get(key) is not None:
            row[key] = _catalog_descriptor(segment.get(key), key=key)
    return row


def _track_parameter_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    keyframe_point_count = 0
    keyframe_group_count = 0
    keyframe_types: set[str] = set()
    volumes: list[float] = []
    material_names: list[str] = []
    time_ranges: list[tuple[int, int]] = []
    for row in rows:
        start_us = int(row.get("start_us") or 0)
        end_us = int(row.get("end_us") or start_us)
        if end_us > start_us:
            time_ranges.append((start_us, end_us))
        material_name = _text(row.get("material_name"))
        if material_name and material_name not in material_names:
            material_names.append(material_name)
        keyframes = _dict(row.get("keyframes"))
        keyframe_point_count += int(keyframes.get("point_count") or 0)
        keyframe_group_count += int(keyframes.get("group_count") or 0)
        keyframe_types.update(_text(item) for item in _list(keyframes.get("property_types")) if _text(item))
        volume = row.get("volume")
        if isinstance(volume, (int, float)) and not isinstance(volume, bool):
            volumes.append(float(volume))
        for raw_ref in _list(row.get("parameter_refs")):
            ref = _dict(raw_ref)
            group = _text(ref.get("parameter_group")) or "other"
            name = _text(ref.get("name")) or "未命名资源"
            grouped.setdefault(group, {})[name] = {
                "collection": ref.get("collection"),
                "name": name,
                "parameters": copy.deepcopy(ref.get("parameters") or {}),
            }
    length_us = sum(end - start for start, end in time_ranges)
    summary: dict[str, Any] = {
        "material_names": material_names,
        "parameter_groups": {
            group: list(values.values())
            for group, values in sorted(grouped.items())
        },
        "keyframes": {
            "group_count": keyframe_group_count,
            "point_count": keyframe_point_count,
            "property_types": sorted(keyframe_types),
        },
        "covered_duration_us": length_us,
    }
    if volumes:
        summary["volume"] = {
            "min": round(min(volumes), 6),
            "max": round(max(volumes), 6),
            "average": round(sum(volumes) / len(volumes), 6),
            "min_percent": round(min(volumes) * 100, 3),
            "max_percent": round(max(volumes) * 100, 3),
            "average_percent": round(sum(volumes) / len(volumes) * 100, 3),
        }
    return summary


def _catalog_track_purpose(track_type: str, category: str, track_name: str, *, role: str = "") -> str:
    label = _PARAMETER_CATEGORY_LABELS.get(category, category)
    if category == "variable":
        if track_type == "audio":
            return f"音频变量轨：{role or track_name or '待确认用途'}"
        return "文案变量轨：主标题/小标题等"
    if category == "narration_audio":
        return f"解说音轨：{role or track_name or '待确认用途'}"
    if category == "global_audio":
        return f"全局音频：{role or track_name or '待确认用途'}"
    if category == "global_subtitles":
        return "全片字幕/字幕样式"
    if track_type == "effect":
        return f"{label}包装特效轨"
    if track_type == "filter":
        return f"{label}滤镜轨"
    if track_type == "text":
        return f"{label}文字/模板轨"
    if track_type == "audio":
        return f"{label}音频轨"
    return f"{label}内容素材轨"


def _normalize_track_binding_overrides(value: Mapping[str, Any] | None) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    if not isinstance(value, Mapping):
        return result
    for raw_index, raw_binding in value.items():
        try:
            track_index = int(raw_index)
        except (TypeError, ValueError):
            raise PackagingCaptureError(f"轨道确认索引无法解析：{raw_index}") from None
        binding = _dict(raw_binding)
        category = _text(binding.get("category") or binding.get("classification") or "auto")
        role = _text(binding.get("element_role") or binding.get("role") or "auto")
        if category not in _TRACK_BINDING_CATEGORIES and category != "auto":
            raise PackagingCaptureError(f"轨道 {track_index} 的分类确认值不支持：{category}")
        if role not in _TRACK_BINDING_ROLES:
            raise PackagingCaptureError(f"轨道 {track_index} 的元素确认值不支持：{role}")
        text_variable_role = _text(binding.get("text_variable_role") or "auto")
        if text_variable_role not in _TEXT_VARIABLE_ROLES:
            raise PackagingCaptureError(
                f"轨道 {track_index} 的文字变量用途不支持：{text_variable_role}"
            )
        target_track_index = binding.get("text_target_track_index")
        if target_track_index in ("", None):
            target_track_index = None
        else:
            try:
                target_track_index = int(target_track_index)
            except (TypeError, ValueError):
                raise PackagingCaptureError(f"轨道 {track_index} 的文案目标轨道索引无法解析：{target_track_index}") from None
        segment_overrides: dict[int, dict[str, Any]] = {}
        raw_segment_overrides = binding.get("segments")
        if isinstance(raw_segment_overrides, Mapping):
            for raw_segment_index, raw_segment_binding in raw_segment_overrides.items():
                try:
                    segment_index = int(raw_segment_index)
                except (TypeError, ValueError):
                    raise PackagingCaptureError(
                        f"轨道 {track_index} 的片段确认索引无法解析：{raw_segment_index}"
                    ) from None
                segment_binding = _dict(raw_segment_binding)
                segment_category = _text(
                    segment_binding.get("category")
                    or segment_binding.get("classification")
                    or "auto"
                )
                segment_role = _text(
                    segment_binding.get("element_role")
                    or segment_binding.get("role")
                    or "auto"
                )
                if segment_category not in _TRACK_BINDING_CATEGORIES and segment_category != "auto":
                    raise PackagingCaptureError(
                        f"轨道 {track_index} 片段 {segment_index} 的分类确认值不支持：{segment_category}"
                    )
                if segment_role not in _TRACK_BINDING_ROLES:
                    raise PackagingCaptureError(
                        f"轨道 {track_index} 片段 {segment_index} 的元素确认值不支持：{segment_role}"
                    )
                segment_text_variable_role = _text(
                    segment_binding.get("text_variable_role") or "auto"
                )
                if segment_text_variable_role not in _TEXT_VARIABLE_ROLES:
                    raise PackagingCaptureError(
                        f"轨道 {track_index} 片段 {segment_index} 的文字变量用途不支持：{segment_text_variable_role}"
                    )
                segment_overrides[segment_index] = {
                    "category": segment_category,
                    "element_role": segment_role,
                    "text_variable_role": segment_text_variable_role,
                    "confirmed": bool(segment_binding.get("confirmed")),
                    "note": _text(segment_binding.get("note")),
                }
        result[track_index] = {
            "category": category,
            "element_role": role,
            "text_variable_role": text_variable_role,
            "confirmed": bool(binding.get("confirmed")),
            "note": _text(binding.get("note")),
            "text_target_track_index": target_track_index,
            "segment_bindings": segment_overrides,
        }
    return result


def build_parameter_catalog(
    packaged_draft: Mapping[str, Any],
    *,
    baseline_draft: Mapping[str, Any] | None = None,
    track_roles: Mapping[str, Any] | None = None,
    track_binding_overrides: Mapping[str, Any] | None = None,
    enforce_confirmation: bool = False,
) -> dict[str, Any]:
    """按镜头分类生成可审阅的轨道级参数清单。

    该清单是 B 的可读观测层：它展示每一类有哪些轨道、轨道用途和参数，
    不把无法确定的轨道静默归入某一类。真正可以回写 C 的字段仍以
    ``candidate_package.parameters`` 为准。
    """

    materials = _dict(packaged_draft.get("materials"))
    material_index = _catalog_material_index(materials)
    duration_us = max(0, int(packaged_draft.get("duration") or 0))
    raw_tracks = _list(packaged_draft.get("tracks"))
    jianying_order = _jianying_track_order(raw_tracks)
    image_sequences = _adjacent_image_sequences(
        raw_tracks,
        material_index,
        draft_duration_us=duration_us,
    )
    image_sequence_member_keys = {
        (int(member["raw_track_index"]), int(member["segment_index"]))
        for sequence in image_sequences
        for member in _list(sequence.get("members"))
    }
    image_sequence_visual_keys = {
        (int(member["raw_track_index"]), int(member["segment_index"]))
        for sequence in image_sequences
        for member in _list(sequence.get("shared_visual_elements"))
    }
    baseline_variables = _baseline_variable_manifest(baseline_draft)
    baseline_tracks = _list(_dict(baseline_draft).get("tracks")) if isinstance(baseline_draft, Mapping) else []
    baseline_text_track_count = sum(
        1
        for raw_track in baseline_tracks
        if _text(_dict(raw_track).get("type") or _dict(raw_track).get("track_type")) == "text"
    )
    baseline_content_track_count = sum(
        1
        for raw_track in baseline_tracks
        if _text(_dict(raw_track).get("type") or _dict(raw_track).get("track_type")) in {"video", "image"}
    )
    raw_role_tracks = _list(_dict(track_roles).get("tracks"))
    binding_overrides = _normalize_track_binding_overrides(track_binding_overrides)
    role_by_name = {
        _text(_dict(item).get("track_name")): _text(_dict(item).get("suggested_role"))
        for item in raw_role_tracks
        if _text(_dict(item).get("track_name"))
    }
    role_by_source_index: dict[int, str] = {}
    for raw_item in raw_role_tracks:
        item = _dict(raw_item)
        try:
            source_index = int(item.get("source_track_index"))
        except (TypeError, ValueError):
            continue
        suggested_role = _text(item.get("suggested_role"))
        if suggested_role:
            role_by_source_index[source_index] = suggested_role
    categories: dict[str, dict[str, Any]] = {
        key: {
            "key": key,
            "label": _PARAMETER_CATEGORY_LABELS.get(key, key),
            "track_count": 0,
            "segment_count": 0,
            "time_range_us": {"start": None, "end": None},
            "track_type_counts": {},
            "tracks": [],
        }
        for key in _PARAMETER_CATEGORY_ORDER
    }
    warnings: list[str] = []
    represented_track_indices: set[int] = set()
    confirmation_tracks: dict[int, dict[str, Any]] = {}
    # 单独保留一份完整时间轴轨道清单。它覆盖 B 的每条原始轨道，最终按剪映
    # 界面顺序排列；即使轨道没有片段也必须保留，供 UI 绘制空轨道行。
    timeline_lanes: list[dict[str, Any]] = []
    # 当剪映把轨道名重置成 effect_track_7/sticker_track_10 时，使用同一时间轴上
    # 的内容素材分类做第二判据，避免包装轨道因为改名而全部落入未分类区。
    content_intervals: dict[str, list[tuple[int, int]]] = {}
    for content_track_index, raw_content_track in enumerate(raw_tracks):
        content_track = _dict(raw_content_track)
        if _text(content_track.get("type") or content_track.get("track_type")) != "video":
            continue
        fallback_category = _infer_parameter_category(content_track.get("name"))
        for content_segment_index, raw_content_segment in enumerate(_list(content_track.get("segments"))):
            content_segment = _dict(raw_content_segment)
            content_row = _segment_parameter_row(
                content_segment,
                segment_index=content_segment_index,
                material_index=material_index,
            )
            content_row["content_media_type"] = _infer_content_media_type(
                content_track.get("type") or content_track.get("track_type"),
                content_row.get("material_type"),
                content_row.get("material_name"),
            ) or ""
            content_category = _infer_content_category(
                content_track.get("name"),
                content_row.get("material_name"),
                track_type=content_track.get("type") or content_track.get("track_type"),
                material_type=content_row.get("material_type"),
            )
            if content_category == "image" and (content_track_index, content_segment_index) in image_sequence_member_keys:
                content_category = "image_sequence"
            start_us = int(content_row.get("start_us") or 0)
            end_us = int(content_row.get("end_us") or start_us)
            # 片头/片尾是可叠加的时间边界，不能覆盖图片/AIGC 等内容分类。
            if content_category in _TRACK_BINDING_CATEGORIES and end_us > start_us:
                content_intervals.setdefault(content_category, []).append((start_us, end_us))

    def infer_packaging_time_category(
        row: Mapping[str, Any],
        track_type: str,
        *,
        text_template: bool = False,
    ) -> str | None:
        # 片头/片尾不是轨道类型，而是时间边界分类。文字模板也属于包装
        # 元素；普通字幕、BGM、解说和主画面仍保持全片/内容分类。
        if track_type not in {"effect", "filter", "sticker"} and not (track_type == "text" and text_template):
            return None
        start_us = int(row.get("start_us") or 0)
        end_us = int(row.get("end_us") or start_us)
        boundary_category = _boundary_packaging_category(row, duration_us)
        if boundary_category is not None:
            return boundary_category
        if track_type == "text":
            return None
        if duration_us and end_us - start_us >= int(duration_us * BOUNDARY_PACKAGING_MAX_DURATION_RATIO):
            return None
        # 先按同一时间范围内的内容素材归属。特效可能从片头 5 秒内开始，
        # 但实际跟随第一张图片/数字人持续到 5 秒之后，不能仅凭时间点误标为片头。
        scores: dict[str, int] = {}
        for category, intervals in content_intervals.items():
            overlap = sum(
                max(0, min(end_us, interval_end) - max(start_us, interval_start))
                for interval_start, interval_end in intervals
            )
            if overlap > 0:
                scores[category] = overlap
        if not scores:
            return None
        best_score = max(scores.values())
        best = sorted(category for category, score in scores.items() if score == best_score)
        return best[0] if len(best) == 1 else None

    for track_index, raw_track in enumerate(raw_tracks):
        track = _dict(raw_track)
        order_meta = jianying_order.get(
            track_index,
            {
                "raw_track_index": track_index,
                "jianying_track_index": track_index,
                "ui_order": track_index,
                "render_index": _track_render_index(track),
            },
        )
        track_type = _text(track.get("type") or track.get("track_type")) or "unknown"
        track_name = _text(track.get("name")) or f"{track_type}_track_{track_index + 1}"
        track_category = _infer_parameter_category(track_name)
        role = role_by_name.get(track_name, "") or role_by_source_index.get(track_index, "")
        binding = binding_overrides.get(track_index, {})
        binding_category = _text(binding.get("category") or "auto")
        binding_role = _text(binding.get("element_role") or "auto")
        if binding_category not in {"", "auto"}:
            track_category = binding_category
        if track_type == "audio" and _is_audio_variable_role(role):
            # 解说、原声随脚本/镜头变化，是内容变量，不应被首尾保护区改写成
            # 片头/片尾包装，也不能作为包装音频参与归化。
            track_category = "variable"
        elif track_type == "audio" and not track_category:
            track_category = "narration_audio" if role == "narration" else "global_audio"
        elif track_type == "text" and not track_category:
            track_category = "global_subtitles"

        rows_by_category: dict[str, list[dict[str, Any]]] = {}
        all_rows: list[dict[str, Any]] = []
        manual_binding_category = binding_category not in {"", "auto"}
        segment_binding_overrides = binding.get("segment_bindings")
        if not isinstance(segment_binding_overrides, Mapping):
            segment_binding_overrides = {}
        for segment_index, raw_segment in enumerate(_list(track.get("segments"))):
            segment = _dict(raw_segment)
            row = _segment_parameter_row(segment, segment_index=segment_index, material_index=material_index)
            row["content_media_type"] = _infer_content_media_type(
                track_type,
                row.get("material_type"),
                row.get("material_name"),
            ) or ""
            if track_type == "audio":
                row["audio_role"] = _role_from_catalog_row(row, role)
            all_rows.append(row)
            raw_segment_binding = segment_binding_overrides.get(segment_index)
            if raw_segment_binding is None:
                raw_segment_binding = segment_binding_overrides.get(str(segment_index))
            segment_binding = _dict(raw_segment_binding)
            is_text_template = track_type == "text" and any(
                _text(_dict(ref).get("collection")) == "text_templates"
                for ref in _list(row.get("parameter_refs"))
            )
            if is_text_template:
                row["text_variable_role"] = _text(
                    segment_binding.get("text_variable_role")
                    or binding.get("text_variable_role")
                    or "auto"
                )
                signature = _text_template_row_signature(row)
                baseline_matches = [
                    item for item in baseline_variables
                    if item.get("signature") == signature
                    and abs(int(item.get("start_us") or 0) - int(row.get("start_us") or 0)) <= DEFAULT_ALIGNMENT_TOLERANCE_US
                    and abs(int(item.get("end_us") or 0) - int(row.get("end_us") or 0)) <= DEFAULT_ALIGNMENT_TOLERANCE_US
                ]
                row["baseline_variable_match"] = bool(baseline_matches)
                row["baseline_variable_role"] = (
                    _text(baseline_matches[0].get("variable_role")) if baseline_matches else "auto"
                )
                row["variable_kind"] = "text"
            is_audio_variable = track_type == "audio" and (
                _is_audio_variable_role(row.get("audio_role"))
                or _text(segment_binding.get("element_role") or segment_binding.get("role")) == "audio_variable"
            )
            if is_audio_variable:
                row["variable_kind"] = "audio"
            row["element_role"] = _text(
                segment_binding.get("element_role")
                or segment_binding.get("role")
                or binding_role
                or ("audio_variable" if is_audio_variable else "auto")
            )
            segment_category = _text(segment_binding.get("category") or "auto")
            boundary_category = _boundary_packaging_category(row, duration_us)
            # 用户在轨道确认面板中指定的分类优先级最高，不能再被素材名的
            # 自动推断覆盖；否则用户纠正了归类，下一次生成清单又会跳回原分类。
            if segment_category not in {"", "auto"}:
                category = segment_category
                category_source = "manual_segment"
            elif manual_binding_category:
                category = binding_category
                category_source = "manual_track"
            elif track_type == "audio" and _is_packaging_audio_role(row.get("audio_role")):
                # 音效是包装层音频，不是全局 BGM。保留待校对分类，后续按与
                # 镜头/特效的时间重叠进入对应包装分组，避免被误标为“全局音频”。
                category = "unclassified"
                category_source = "automatic_packaging_audio"
            elif is_text_template or is_audio_variable:
                # 变量层与镜头分类、首尾保护区是正交关系：它会在组合图中随
                # 时间落入片头/片尾窗口，但自身必须保留为独立变量轨。
                category = "variable"
                category_source = "automatic_variable"
            else:
                # 片头/片尾作为可叠加边界单独保留，不能抢占图片/AIGC 等内容类型。
                category = _infer_content_category(
                    track_name,
                    row.get("material_name"),
                    track_type=track_type,
                    material_type=row.get("material_type"),
                )
                category_source = "automatic_inference"
                parameter_refs = row.get("parameter_refs")
                if category is None and is_text_template:
                    category = infer_packaging_time_category(row, track_type, text_template=True)
                if category is None:
                    category = track_category
                if category is None:
                    category = infer_packaging_time_category(row, track_type)
                if category == "image" and (track_index, segment_index) in image_sequence_member_keys:
                    category = "image_sequence"
                elif (track_index, segment_index) in image_sequence_visual_keys:
                    category = "image_sequence"
            if category is None:
                category = "unclassified"
            if (
                category in {"opening", "ending"}
                and category_source.startswith("automatic")
                and boundary_category is None
            ):
                # 名称推断不能推翻硬边界规则：长覆盖素材不自动认作片头/片尾。
                category = "unclassified"
                category_source = "automatic_long_coverage"
            row["boundary_category"] = boundary_category or ""
            row["category_source"] = category_source
            rows_by_category.setdefault(category, []).append(row)

        if not rows_by_category:
            rows_by_category[track_category or "unclassified"] = []
        if "unclassified" in rows_by_category and track_category is None:
            warnings.append(f"轨道 {track_index}「{track_name}」无法从名称/素材确定镜头分类。")

        contains_packaging_audio = track_type == "audio" and any(
            _is_packaging_audio_role(row.get("audio_role")) for row in all_rows
        )
        scope = "packaging" if track_name.startswith("包装-") or track_type in {"effect", "filter"} or contains_packaging_audio else "content"
        if (
            track_type in {"audio", "text"}
            and track_category in {"global_audio", "narration_audio", "global_subtitles"}
            and "variable" not in rows_by_category
            and not contains_packaging_audio
        ):
            scope = "global"
        all_summary = _track_parameter_summary(all_rows)
        template_items = _list(_dict(all_summary.get("parameter_groups")).get("text_templates"))
        template_names = [
            _text(_dict(item).get("name"))
            for item in template_items
            if _text(_dict(item).get("name"))
        ]
        explicit_category = _infer_parameter_category(track_name) is not None
        needs_confirmation = (
            (track_type in {"effect", "filter", "sticker"} and (not explicit_category or track_type == "sticker"))
            or (track_type == "text" and bool(template_names))
        )
        confirmation_reason: list[str] = []
        if track_type in {"effect", "filter", "sticker"} and not explicit_category:
            confirmation_reason.append("轨道名称没有明确镜头分类，可能是全局特效/贴纸。")
        if track_type == "text" and template_names:
            confirmation_reason.append("检测到文字模板，需要确认变量文字应绑定到哪条文字轨。")
        requires_confirmation = bool(needs_confirmation or enforce_confirmation)
        if enforce_confirmation and not needs_confirmation:
            confirmation_reason.append("请确认系统自动归类是否正确。")
        confirmed = bool(binding.get("confirmed")) if requires_confirmation else True
        confirmation_tracks[track_index] = {
            "track_index": track_index,
            "raw_track_index": track_index,
            "jianying_track_index": int(order_meta["jianying_track_index"]),
            "ui_order": int(order_meta["ui_order"]),
            "track_name": track_name,
            "track_type": track_type,
            "suggested_category": track_category or "unclassified",
            "suggested_element_role": binding_role if binding_role != "auto" else (
                "text_template_variable" if template_names else (
                    "global_effect" if track_type in {"effect", "filter"} and not explicit_category else (
                        "sticker" if track_type == "sticker" else "auto"
                    )
                )
            ),
            "category": binding_category if binding_category != "auto" else (track_category or "unclassified"),
            "element_role": binding_role,
            "text_variable_role": _text(binding.get("text_variable_role") or "auto"),
            "requires_confirmation": requires_confirmation,
            "confirmed": confirmed,
            "reason": confirmation_reason,
            "text_template_names": template_names,
            "text_target_track_index": binding.get("text_target_track_index") if binding.get("text_target_track_index") is not None else (track_index if template_names else None),
            "segment_bindings": {
                str(segment_index): copy.deepcopy(segment_binding)
                for segment_index, segment_binding in segment_binding_overrides.items()
            },
            "note": _text(binding.get("note")),
        }
        timeline_segments: list[dict[str, Any]] = []
        for category, rows in rows_by_category.items():
            for row in rows:
                timeline_row = copy.deepcopy(row)
                timeline_row["category"] = category if category in categories else "unclassified"
                timeline_segments.append(timeline_row)
        timeline_lanes.append(
            {
                "track_index": track_index,
                "raw_track_index": track_index,
                "jianying_track_index": int(order_meta["jianying_track_index"]),
                "ui_order": int(order_meta["ui_order"]),
                "render_index": int(order_meta["render_index"]),
                "track_name": track_name,
                "track_type": track_type,
                "audio_role": role if track_type == "audio" else "",
                "scope": scope,
                "segments": timeline_segments,
                "confirmation": copy.deepcopy(confirmation_tracks[track_index]),
            }
        )
        for category, rows in rows_by_category.items():
            if category not in categories:
                category = "unclassified"
            represented_track_indices.add(track_index)
            starts = [int(row.get("start_us") or 0) for row in rows]
            ends = [int(row.get("end_us") or 0) for row in rows]
            start_us = min(starts) if starts else 0
            end_us = max(ends) if ends else 0
            summary = _track_parameter_summary(rows)
            raw_material_ids = [
                _text(_dict(segment).get("material_id"))
                for segment in _list(track.get("segments"))
                if _text(_dict(segment).get("material_id"))
            ]
            track_entry = {
                "track_index": track_index,
                "raw_track_index": track_index,
                "jianying_track_index": int(order_meta["jianying_track_index"]),
                "ui_order": int(order_meta["ui_order"]),
                "render_index": int(order_meta["render_index"]),
                "track_name": track_name,
                "track_type": track_type,
                "audio_role": role if track_type == "audio" else "",
                "scope": scope,
                "purpose": _catalog_track_purpose(track_type, category, track_name, role=role),
                "segment_count": len(rows),
                "segment_indices": [int(row.get("segment_index") or 0) for row in rows],
                "time_range_us": {"start": start_us, "end": end_us},
                "coverage_ratio": round((end_us - start_us) / duration_us, 6) if duration_us and end_us > start_us else 0,
                "track_parameters": _catalog_descriptor({
                    "attribute": track.get("attribute"),
                    "flag": track.get("flag"),
                    "is_default_name": track.get("is_default_name"),
                    "muted": track.get("muted"),
                    "hidden": track.get("hidden"),
                    "volume": track.get("volume"),
                    "role": role,
                    "material_id_count": len(set(raw_material_ids)),
                }),
                "parameter_summary": summary,
                "segments": rows,
                "confirmation": copy.deepcopy(confirmation_tracks[track_index]),
            }
            bucket = categories[category]
            bucket["tracks"].append(track_entry)
            bucket["track_count"] = len(bucket["tracks"])
            bucket["segment_count"] = sum(int(item.get("segment_count") or 0) for item in bucket["tracks"])
            type_counts = Counter(_text(item.get("track_type")) or "unknown" for item in bucket["tracks"])
            bucket["track_type_counts"] = dict(sorted(type_counts.items()))
            bucket_starts = [
                int(_dict(item.get("time_range_us")).get("start") or 0)
                for item in bucket["tracks"]
                if _dict(item.get("time_range_us")).get("start") is not None
            ]
            bucket_ends = [
                int(_dict(item.get("time_range_us")).get("end") or 0)
                for item in bucket["tracks"]
                if _dict(item.get("time_range_us")).get("end") is not None
            ]
            bucket["time_range_us"] = {
                "start": min(bucket_starts) if bucket_starts else None,
                "end": max(bucket_ends) if bucket_ends else None,
            }

    # 分类对话框也按剪映界面顺序显示；参数回写仍使用 raw_track_index。
    for bucket in categories.values():
        bucket["tracks"].sort(key=lambda item: int(item.get("ui_order") or 0))
    timeline_lanes.sort(key=lambda item: int(item.get("ui_order") or 0))
    composition_patterns = _build_composition_patterns(timeline_lanes, duration_us)

    unclassified = categories["unclassified"]
    if unclassified["track_count"]:
        warnings.append(f"共有 {unclassified['track_count']} 条轨道仍在未分类区，生成包装包前需要人工确认。")
    required_confirmation = (
        list(confirmation_tracks.values())
        if enforce_confirmation
        else [item for item in confirmation_tracks.values() if item.get("requires_confirmation")]
    )
    unresolved_confirmation = [
        item for item in required_confirmation
        if not item.get("confirmed")
    ]
    confirmation = {
        "enforced": bool(enforce_confirmation),
        "confirmed": not unresolved_confirmation if enforce_confirmation else False,
        "required_track_indices": [int(item["track_index"]) for item in required_confirmation],
        "required_jianying_track_indices": [int(item["jianying_track_index"]) for item in required_confirmation],
        "unresolved_track_indices": [int(item["track_index"]) for item in unresolved_confirmation],
        "unresolved_jianying_track_indices": [int(item["jianying_track_index"]) for item in unresolved_confirmation],
        "tracks": list(sorted(confirmation_tracks.values(), key=lambda item: int(item.get("ui_order") or 0))),
        "text_template_targets": [
            {
                "track_index": int(item["track_index"]),
                "track_name": item["track_name"],
                "template_names": item["text_template_names"],
                "confirmed": item["confirmed"],
                "target_track_index": item.get("text_target_track_index"),
            }
            for item in required_confirmation
            if item.get("text_template_names")
        ],
        "instructions": (
            "已进入全轨道归类确认模式：每条轨道都必须确认分类；未确认时禁止生成 C。"
            if enforce_confirmation
            else "全局特效、贴纸和文字模板轨必须由用户确认；未确认时禁止生成 C。"
        ),
    }
    return {
        "schema_version": PARAMETER_CATALOG_SCHEMA_VERSION,
        "source": {
            "scope": "packaged_B",
            "draft_name": _text(packaged_draft.get("name")),
            "duration_us": duration_us,
            "timeline_unit": TIMELINE_UNIT,
        },
        "category_order": list(_PARAMETER_CATEGORY_ORDER),
        "categories": categories,
        "track_index_contract": {
            "display_field": "jianying_track_index",
            "raw_field": "raw_track_index",
            "ui_order_field": "ui_order",
            "audio": "负数轨道：-1、-2…",
            "visual": "非负整数轨道：0、1、2…",
            "order": "剪映界面从顶部到下方",
        },
        "track_lanes": timeline_lanes,
        "image_packaging_patterns": {
            "policy": "首尾 5 秒保护区不参与图片包装样例；其余区域中，三张及以上连续相邻图片且至少共用一条局部特效/滤镜/贴纸视觉包装时，归为连续多图共用包装；覆盖全片的全局特效不参与此判定，其他图片归为单图/少图包装。",
            "adjacency_tolerance_us": IMAGE_ADJACENCY_TOLERANCE_US,
            "single_image_segment_count": int(categories["image"]["segment_count"]),
            "sequence_count": len(image_sequences),
            "sequences": copy.deepcopy(image_sequences),
        },
        "composition_patterns": composition_patterns,
        "variable_baseline": {
            # 即使 A 没有 text_template，A 仍是变量层依附的内容基线；
            # text_template_match_count 仅表示可直接一一匹配的 A 文字模板数。
            "source": "baseline_A" if isinstance(baseline_draft, Mapping) else "none",
            "alignment_tolerance_us": DEFAULT_ALIGNMENT_TOLERANCE_US,
            "text_template_match_count": len(baseline_variables),
            "baseline_content_track_count": baseline_content_track_count,
            "baseline_text_track_count": baseline_text_track_count,
            "templates": copy.deepcopy(baseline_variables),
        },
        "confirmation": confirmation,
        "totals": {
            "track_count": len(_list(packaged_draft.get("tracks"))),
            "represented_track_count": len(represented_track_indices),
            "segment_count": sum(len(_list(_dict(track).get("segments"))) for track in _list(packaged_draft.get("tracks"))),
        },
        "inference": {
            "policy": "首尾 5 秒保护区优先归片头/片尾，不参与图片/AIGC自动样例；文案模板、解说和原声优先归变量轨，不被保护区改写成片头/片尾。中段再按数字人/AIGC/图片/说明镜头名称或素材名分类。视觉层、包装音频层、变量层按组合规则归化；无法确认的轨道保留在未分类区，不静默归类。",
            "warnings": warnings,
        },
    }


def _inventory(draft: Mapping[str, Any]) -> dict[str, Any]:
    materials = _dict(draft.get("materials"))
    tracks = _list(draft.get("tracks"))
    track_types = Counter()
    segment_count = 0
    for raw_track in tracks:
        track = _dict(raw_track)
        track_types[_text(track.get("type") or track.get("track_type")) or "unknown"] += 1
        segment_count += len(_list(track.get("segments")))
    material_counts = {
        str(key): len(_list(value))
        for key, value in materials.items()
        if isinstance(value, list)
    }
    # A/B 的 B 草稿允许新增包装音频、特效、滤镜、转场和包装文字；脚本
    # 兼容性只检查视频/图片内容素材，不能把合法包装增量误判成内容改稿。
    content_material_counts = {
        key: material_counts.get(key, 0)
        for key in ("videos", "images")
    }
    content_track_signature: list[tuple[str, str, int]] = []
    for raw_track in tracks:
        track = _dict(raw_track)
        track_type = _text(track.get("type") or track.get("track_type"))
        track_name = _text(track.get("name"))
        if track_type in {"video", "audio", "text"} and not track_name.startswith("包装-"):
            content_track_signature.append((track_type, track_name, len(_list(track.get("segments")))))

    def text_content(item: Mapping[str, Any]) -> str:
        raw = item.get("content")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, Mapping):
                return _text(parsed.get("text"))
        return _text(item.get("base_content") or item.get("text"))

    content_visual_signature = [
        (
            _text(_dict(item).get("material_name") or _dict(item).get("name")),
            _text(_dict(item).get("type")),
            int(_dict(item).get("duration") or 0),
            int(_dict(item).get("width") or 0),
            int(_dict(item).get("height") or 0),
        )
        for key in ("videos", "images")
        for item in _list(materials.get(key))
    ]
    content_text_signature = [text_content(_dict(item)) for item in _list(materials.get("texts"))]
    return {
        "track_count": len(tracks),
        "track_types": dict(sorted(track_types.items())),
        "segment_count": segment_count,
        "material_counts": dict(sorted(material_counts.items())),
        "content_material_counts": content_material_counts,
        "content_track_signature": content_track_signature,
        "content_visual_signature": content_visual_signature,
        "content_text_signature": content_text_signature,
        "keyframe_point_count": _count_keyframes(draft),
        "duration_us": int(draft.get("duration") or 0),
    }


def _analysis_summary(analysis: Mapping[str, Any]) -> dict[str, Any]:
    audio = _dict(analysis.get("audio"))
    visual = _dict(analysis.get("visual"))
    subtitles = _dict(analysis.get("subtitles"))
    transitions = _dict(analysis.get("transitions"))
    return {
        "audio_track_count": int(audio.get("track_count") or 0),
        "visual_track_count": len(_list(visual.get("tracks"))),
        "overall_effect_count": len(_list(visual.get("overall_effects"))),
        "overall_filter_count": len(_list(visual.get("overall_filters"))),
        "subtitle_profile_count": int(subtitles.get("profile_count") or 0),
        "applied_transition_count": int(transitions.get("applied_count") or 0),
        "transition_audio_companion_count": int(transitions.get("audio_companion_count") or 0),
    }


def _role_from_signal(signal_text: str, track_role: str = "") -> tuple[str, str, bool]:
    signal_text = _text(signal_text).lower()
    track_role = _text(track_role).lower()
    if track_role == "music" or any(token in signal_text for token in ("bgm", "音乐", "配乐", "背景音乐")):
        return "bgm", "根据轨道/片段素材判断为 BGM", False
    if any(token in signal_text for token in ("解说", "旁白", "人声", "配音", "narration", "voice")):
        return "narration", "根据轨道和片段素材判断为解说/人声", False
    if any(token in signal_text for token in ("转场", "transition", "whoosh", "呼的")):
        return "transition_sfx", "根据片段素材判断为转场配合音效", False
    if any(token in signal_text for token in ("画面增强", "增强音效", "写字", "翻页", "翻书", "点击", "敲击", "键盘", "击打", "hit", "impact")):
        return "visual_enhancement_sfx", "根据片段素材判断为画面增强音效", False
    if any(token in signal_text for token in ("环境音", "氛围音", "背景音", "ambience", "ambient", "room tone")):
        return "ambience", "根据片段素材判断为背景/环境音", False
    if track_role == "sound_effect" or any(token in signal_text for token in ("音效", "sfx", "声音")):
        return "sfx", "检测到音效轨，但需要确认具体用途", True
    return "unknown", "未能从剪映元数据确定音频用途", True


def _role_from_track(track: Mapping[str, Any]) -> tuple[str, str, bool]:
    name = _text(track.get("name")).lower()
    source_type = _text(track.get("source_type"))
    track_role = _text(track.get("track_role"))
    # 剪映经常把音频轨道命名成 audio_track_N 或“音效”，真正用途只出现在
    # 片段素材名、sound_effects 或附加音效资源里，因此不能只看轨道名。
    material_names: list[str] = []
    for raw_segment in _list(track.get("segments")):
        segment = _dict(raw_segment)
        for key in ("material_name", "name"):
            value = _text(segment.get(key))
            if value:
                material_names.append(value.lower())
    for raw_effect in _list(track.get("sound_effects")):
        effect = _dict(raw_effect)
        value = _text(effect.get("name"))
        if value:
            material_names.append(value.lower())
    signal_text = " ".join([name, *material_names])
    if track.get("is_video_original_audio") or source_type == "video_original_audio":
        return "original_audio", "视频原声轨", False
    return _role_from_signal(signal_text, track_role)


def _role_from_catalog_row(row: Mapping[str, Any], fallback_role: str = "") -> str:
    """按单个参数目录片段判断音频用途，避免混合音效轨被整轨误归类。"""

    values = [_text(row.get("material_name")), _text(row.get("material_type"))]
    for raw_ref in _list(row.get("parameter_refs")):
        ref = _dict(raw_ref)
        values.extend((_text(ref.get("collection")), _text(ref.get("name"))))
    role, _reason, _uncertain = _role_from_signal(" ".join(values), "")
    return role if role != "unknown" else (_text(fallback_role) or "unknown")


def _is_audio_variable_role(role: object) -> bool:
    """解说和视频原声随内容片段变化，属于内容变量，不是包装音频。"""

    return _text(role) in {"narration", "original_audio"}


def _is_packaging_audio_role(role: object) -> bool:
    """转场、画面增强等音效属于包装层音频，不是全局 BGM。"""

    return _text(role) in {"sfx", "sound_effect", "transition_sfx", "visual_enhancement_sfx"}


def _text_template_row_signature(row: Mapping[str, Any]) -> str:
    names = [
        _composition_token(_dict(ref).get("name"))
        for ref in _list(row.get("parameter_refs"))
        if _text(_dict(ref).get("collection")) == "text_templates" and _text(_dict(ref).get("name"))
    ]
    return "|".join(sorted(set(names)))


def _baseline_variable_manifest(draft: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """读取 A 中的文字模板轨道，作为 B 变量层的稳定身份基准。"""

    if not isinstance(draft, Mapping):
        return []
    material_index = _catalog_material_index(_dict(draft.get("materials")))
    result: list[dict[str, Any]] = []
    for track_index, raw_track in enumerate(_list(draft.get("tracks"))):
        track = _dict(raw_track)
        if _text(track.get("type") or track.get("track_type")) != "text":
            continue
        for segment_index, raw_segment in enumerate(_list(track.get("segments"))):
            row = _segment_parameter_row(_dict(raw_segment), segment_index=segment_index, material_index=material_index)
            signature = _text_template_row_signature(row)
            if not signature:
                continue
            result.append({
                "raw_track_index": track_index,
                "segment_index": segment_index,
                "track_name": _text(track.get("name")),
                "signature": signature,
                "start_us": int(row.get("start_us") or 0),
                "end_us": int(row.get("end_us") or row.get("start_us") or 0),
                "variable_role": "main_title" if any(token in signature for token in ("主标题", "main_title", "main title")) else (
                    "subtitle" if any(token in signature for token in ("副标题", "subtitle", "sub title")) else "auto"
                ),
            })
    return result


def _composition_token(value: object) -> str:
    text = _text(value).lower()
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _composition_candidate(lane: Mapping[str, Any], segment: Mapping[str, Any]) -> dict[str, Any] | None:
    track_type = _text(lane.get("track_type"))
    refs = [_dict(ref) for ref in _list(segment.get("parameter_refs"))]
    collections = sorted({_text(ref.get("collection")) for ref in refs if _text(ref.get("collection"))})
    names = [_text(segment.get("material_name"))]
    names.extend(_text(ref.get("name")) for ref in refs if _text(ref.get("name")))
    if track_type in {"effect", "filter", "sticker"}:
        layer = "visual"
        role = "visual"
        variable_kind = ""
    elif track_type == "text" and (
        _text(segment.get("material_type")) == "text_template"
        or "text_templates" in collections
    ):
        layer = "variable"
        # A 是变量层的身份基线；B 上的手工确认只在没有匹配到 A 时作为兜底。
        role = _text(
            segment.get("baseline_variable_role")
            if segment.get("baseline_variable_match")
            else segment.get("text_variable_role")
        ) or "auto"
        variable_kind = "text"
    elif track_type == "audio":
        role = _text(segment.get("audio_role") or lane.get("audio_role") or "unknown")
        if _is_audio_variable_role(role) or _text(segment.get("element_role")) == "audio_variable":
            # 解说/原声是随镜头替换的内容变量；用户也可以把其他音频片段
            # 明确标为 audio_variable。它们展示在变量轴，不计入包装音频。
            layer = "variable"
            role = f"{role}_variable" if role else "audio_variable"
            variable_kind = "audio"
        else:
            layer = "audio"
            variable_kind = ""
    else:
        return None
    start_us = max(0, int(segment.get("start_us") or 0))
    end_us = max(start_us, int(segment.get("end_us") or start_us))
    if end_us <= start_us:
        return None
    normalized_names = sorted({_composition_token(name) for name in names if _composition_token(name)})
    material_token = "/".join(normalized_names[:4]) or "unnamed"
    pattern_key = f"{layer}:{track_type}:{role}:{','.join(collections) or 'none'}"
    return {
        "raw_track_index": int(lane.get("raw_track_index") or lane.get("track_index") or 0),
        "jianying_track_index": int(lane.get("jianying_track_index") or lane.get("track_index") or 0),
        "segment_index": int(segment.get("segment_index") or 0),
        "track_name": _text(lane.get("track_name")),
        "track_type": track_type,
        "layer": layer,
        "role": role,
        "variable_kind": variable_kind,
        "category": _text(segment.get("category")),
        "category_source": _text(segment.get("category_source")),
        "pattern_key": pattern_key,
        "material_name": _text(segment.get("material_name")),
        "start_us": start_us,
        "end_us": end_us,
        "duration_us": end_us - start_us,
        "material_token": material_token,
    }


def _composition_overlap(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    tolerance_us: int = DEFAULT_ALIGNMENT_TOLERANCE_US,
) -> int:
    """按人工剪辑的时间容差判断重叠，不要求微秒级完全对齐。"""

    tolerance = max(0, int(tolerance_us))
    return max(
        0,
        min(int(left.get("end_us") or 0) + tolerance, int(right.get("end_us") or 0) + tolerance)
        - max(int(left.get("start_us") or 0) - tolerance, int(right.get("start_us") or 0) - tolerance),
    )


def _composition_group_view(group: Mapping[str, Any]) -> dict[str, Any]:
    members = list(group.get("members") or [])
    return {
        "raw_track_index": int(group.get("raw_track_index") or 0),
        "jianying_track_index": int(group.get("jianying_track_index") or 0),
        "segment_index": int(group.get("segment_index") or 0),
        "track_name": _text(group.get("track_name")),
        "track_type": _text(group.get("track_type")),
        "layer": _text(group.get("layer")),
        "role": _text(group.get("role")),
        "variable_kind": _text(group.get("variable_kind")),
        "audio_scope": _text(group.get("audio_scope")),
        "material_name": _text(group.get("material_name")),
        "start_us": int(group.get("start_us") or 0),
        "end_us": int(group.get("end_us") or 0),
    }


def _build_composition_patterns(timeline_lanes: list[dict[str, Any]], duration_us: int) -> dict[str, Any]:
    """把视觉、音频、文字变量按时间重叠组成可审阅的包装组合。"""

    full_duration_threshold = int(duration_us * 0.8) if duration_us else 0

    def is_local_packaging_candidate(candidate: Mapping[str, Any]) -> bool:
        """排除全片底色、BGM、解说等基础层，防止它们污染局部包装归化。"""

        if full_duration_threshold and int(candidate.get("duration_us") or 0) >= full_duration_threshold:
            return False
        if candidate.get("layer") == "audio":
            role = _text(candidate.get("role"))
            # BGM、未知音频默认是全局/内容音频，只作为片头片尾的关联音频展示；
            # 只有用户显式改成片头/片尾时，才允许它成为包装音频。首尾保护区
            # 的自动分类不能改变 BGM 的全局属性。
            if role in {"bgm", "narration", "original_audio", "unknown"}:
                return (
                    _text(candidate.get("category")) in {"opening", "ending"}
                    and _text(candidate.get("category_source")).startswith("manual_")
                )
        return True

    all_candidates: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for lane in timeline_lanes:
        for segment in _list(lane.get("segments")):
            candidate = _composition_candidate(lane, _dict(segment))
            if not candidate:
                continue
            all_candidates.append(candidate)
            if is_local_packaging_candidate(candidate):
                candidates.append(candidate)

    def relevant_for_boundary(candidate: Mapping[str, Any]) -> bool:
        return True

    def make_group(
        group_id: str,
        label: str,
        members: list[dict[str, Any]],
        *,
        occurrence_count: int = 1,
        status: str = "composite_confirmed",
        display_range: Mapping[str, int] | None = None,
        occurrence_ranges: list[dict[str, int]] | None = None,
        audio_packaging_member_count: int | None = None,
        audio_context_member_count: int | None = None,
        audio_status: str = "not_applicable",
    ) -> dict[str, Any] | None:
        layer_types = sorted({_text(item.get("layer")) for item in members if _text(item.get("layer"))})
        # 包装组合的最小闭环是视频包装 + 音频包装；变量层是可选的第三层。
        if "visual" not in layer_types or "audio" not in layer_types:
            return None
        if display_range is not None:
            start_us = int(display_range.get("start_us") or 0)
            end_us = int(display_range.get("end_us") or start_us)
        else:
            start_us = min(int(item.get("start_us") or 0) for item in members)
            end_us = max(int(item.get("end_us") or 0) for item in members)
        return {
            "group_id": group_id,
            "label": label,
            "classification": "opening" if label == "片头包装组合" else ("ending" if label == "片尾包装组合" else "common_packaging"),
            "status": status,
            "common_pattern": True,
            "occurrence_count": occurrence_count,
            "layer_types": layer_types,
            "audio_packaging_member_count": int(audio_packaging_member_count or 0),
            "audio_context_member_count": int(audio_context_member_count or 0),
            "audio_status": audio_status,
            "time_range_us": {"start": start_us, "end": end_us},
            "members": [_composition_group_view(item) for item in members],
            "occurrence_ranges": copy.deepcopy(occurrence_ranges or [{"start_us": start_us, "end_us": end_us}]),
            "signature": "|".join(sorted(
                f"{_text(item.get('pattern_key'))}:{_text(item.get('material_token'))}"
                for item in members
            )),
        }

    groups: list[dict[str, Any]] = []
    boundary_windows: list[dict[str, Any]] = []
    window_size = BOUNDARY_PACKAGING_WINDOW_US
    boundaries = (
        ("opening", "片头包装组合", 0, min(duration_us or window_size, window_size)),
        ("ending", "片尾包装组合", max(0, (duration_us or window_size) - window_size), duration_us or window_size),
    )
    for boundary_key, label, start_us, end_us in boundaries:
        window = {"start_us": start_us, "end_us": end_us}
        local_members = [item for item in candidates if relevant_for_boundary(item) and _composition_overlap(item, window) > 0]
        # BGM/解说等即使默认不自动归为“包装音频”，也必须在图形组合中展示，
        # 让用户能判断它是否需要重新标注成片头/片尾音频包装。
        audio_context = [
            item
            for item in all_candidates
            if item.get("layer") == "audio" and _composition_overlap(item, window) > 0
        ]
        # 一个音频片段在同一边界内只能有一种身份：局部音效是“包装音频”，
        # BGM 等全局音频是“关联上下文”。不能因为同在时间窗口就覆盖身份。
        members_by_key: dict[str, dict[str, Any]] = {}
        for item in local_members:
            member = copy.deepcopy(item)
            if member.get("layer") == "audio":
                member["audio_scope"] = "packaging"
            members_by_key[f"{member['raw_track_index']}:{member['segment_index']}"] = member
        for item in audio_context:
            key = f"{item['raw_track_index']}:{item['segment_index']}"
            if key in members_by_key:
                continue
            member = copy.deepcopy(item)
            member["audio_scope"] = "context"
            members_by_key[key] = member
        members = list(members_by_key.values())
        layer_types = sorted({_text(item.get("layer")) for item in members})
        audio_packaging_member_count = sum(
            1
            for item in members
            if item.get("layer") == "audio" and item.get("audio_scope") == "packaging"
        )
        audio_context_member_count = sum(
            1
            for item in members
            if item.get("layer") == "audio" and item.get("audio_scope") == "context"
        )
        has_visual = "visual" in layer_types
        if has_visual and audio_packaging_member_count:
            status = "composite_confirmed"
            audio_status = "packaging_audio_confirmed"
        elif has_visual and audio_context_member_count:
            status = "audio_context_needs_confirmation"
            audio_status = "context_audio_needs_confirmation"
        elif has_visual:
            status = "missing_audio_packaging"
            audio_status = "missing_audio_packaging"
        else:
            status = "insufficient_layers"
            audio_status = "not_applicable"
        boundary_windows.append({
            "category": boundary_key,
            "label": label,
            "time_range_us": {"start": start_us, "end": end_us},
            "layer_types": layer_types,
            "status": status,
            "member_count": len(members),
            "audio_packaging_member_count": audio_packaging_member_count,
            "audio_context_member_count": audio_context_member_count,
            "audio_status": audio_status,
        })
        group = make_group(
            f"{boundary_key}_composition_1",
            label,
            members,
            status=status,
            display_range=window,
            audio_packaging_member_count=audio_packaging_member_count,
            audio_context_member_count=audio_context_member_count,
            audio_status=audio_status,
        )
        if group:
            groups.append(group)

    # 对片头/片尾之外的重复组合，按共同层结构和参数资源集合归并。
    occurrences_by_members: dict[str, dict[str, Any]] = {}
    for anchor in candidates:
        if anchor.get("layer") == "audio":
            continue
        members = [item for item in candidates if relevant_for_boundary(item) and _composition_overlap(item, anchor) > 0]
        if len({_text(item.get("layer")) for item in members}) < 2:
            continue
        member_key = "|".join(sorted(f"{item['raw_track_index']}:{item['segment_index']}" for item in members))
        signature = "|".join(sorted(
            f"{_text(item.get('pattern_key'))}:{_text(item.get('material_token'))}"
            for item in members
        ))
        occurrences_by_members.setdefault(member_key, {"signature": signature, "members": members})
    occurrences_by_signature: dict[str, list[dict[str, Any]]] = {}
    for occurrence in occurrences_by_members.values():
        occurrences_by_signature.setdefault(occurrence["signature"], []).append(occurrence)
    repeated_index = 1
    for signature, occurrences in occurrences_by_signature.items():
        if len(occurrences) < 2:
            continue
        unique_members = {
            f"{member['raw_track_index']}:{member['segment_index']}": member
            for occurrence in occurrences
            for member in occurrence["members"]
        }
        all_members = list(unique_members.values())
        occurrence_ranges = [
            {
                "start_us": min(int(item.get("start_us") or 0) for item in occurrence["members"]),
                "end_us": max(int(item.get("end_us") or 0) for item in occurrence["members"]),
            }
            for occurrence in occurrences
        ]
        group = make_group(
            f"common_composition_{repeated_index}",
            "重复包装组合",
            all_members,
            occurrence_count=len(occurrences),
            status="repeated_pattern",
            occurrence_ranges=occurrence_ranges,
            audio_packaging_member_count=sum(1 for item in all_members if item.get("layer") == "audio"),
            audio_context_member_count=0,
            audio_status="packaging_audio_confirmed",
        )
        if group:
            groups.append(group)
            repeated_index += 1

    segment_group_ids: dict[str, list[str]] = {}
    for group in groups:
        members = list(group.get("members") or [])
        for member in members:
            key = f"{member.get('raw_track_index')}:{member.get('segment_index')}"
            segment_group_ids.setdefault(key, []).append(_text(group.get("group_id")))
    for lane in timeline_lanes:
        raw_index = int(lane.get("raw_track_index") or lane.get("track_index") or 0)
        for segment in _list(lane.get("segments")):
            key = f"{raw_index}:{int(_dict(segment).get('segment_index') or 0)}"
            ids = segment_group_ids.get(key)
            if ids:
                segment["composition_group_ids"] = ids

    return {
        "schema_version": "packaging-composition-v1",
        "policy": "包装组合必须包含视频/视觉包装层和音频包装层；文案变量、解说/原声等音频变量独立展示在变量轴，不计入包装音频。BGM 仅作为关联音频展示；未被标注为片头/片尾音频包装时需要人工确认。",
        "alignment_tolerance_us": DEFAULT_ALIGNMENT_TOLERANCE_US,
        "boundary_windows": boundary_windows,
        "groups": groups,
        "common_pattern_count": sum(1 for group in groups if group.get("status") == "repeated_pattern"),
        "composite_group_count": len(groups),
    }


def infer_track_roles(analysis: Mapping[str, Any], overrides: Mapping[str, object] | None = None) -> dict[str, Any]:
    """给出轨道角色建议；只对无法确定的用途要求人工确认。"""

    raw_overrides = overrides if isinstance(overrides, Mapping) else {}
    tracks = _list(_dict(analysis.get("audio")).get("tracks"))
    result: list[dict[str, Any]] = []
    needs_confirmation = False
    for index, raw_track in enumerate(tracks):
        track = _dict(raw_track)
        role, reason, uncertain = _role_from_track(track)
        override = raw_overrides.get(str(index), raw_overrides.get(index))
        if override is not None:
            role = _text(override)
            if role not in _TRACK_ROLE_VALUES:
                raise PackagingCaptureError(f"音频轨 {index} 的角色不支持：{role}")
            reason = "用户确认"
            uncertain = False
        needs_confirmation = needs_confirmation or uncertain
        source_track_index = track.get("track_index")
        try:
            source_track_index = int(source_track_index)
        except (TypeError, ValueError):
            source_track_index = None
        result.append({
            "track_index": index,
            "source_track_index": source_track_index,
            "track_name": _text(track.get("name")) or f"audio_track_{index + 1}",
            "suggested_role": role,
            "reason": reason,
            "needs_confirmation": uncertain,
            "segment_count": int(track.get("segment_count") or 0),
        })
    return {"tracks": result, "needs_confirmation": needs_confirmation}


def _compatibility(
    baseline_manifest: Mapping[str, Any],
    packaged_manifest: Mapping[str, Any],
    baseline_inventory: Mapping[str, Any],
    packaged_inventory: Mapping[str, Any],
) -> dict[str, Any]:
    warnings: list[str] = []
    if baseline_manifest.get("duration_us") and packaged_manifest.get("duration_us"):
        if baseline_manifest.get("duration_us") != packaged_manifest.get("duration_us"):
            warnings.append("A/B 草稿时长不同，无法把所有时间变化都归因于包装。")
    if baseline_manifest.get("canvas") != packaged_manifest.get("canvas"):
        warnings.append("A/B 画布配置不同，属于内容/项目结构变化，不应直接写入包装参数。")
    if baseline_inventory.get("content_material_counts") != packaged_inventory.get("content_material_counts"):
        warnings.append("A/B 视频或图片内容素材数量不同，可能存在内容增删。")
    if baseline_inventory.get("content_track_signature") != packaged_inventory.get("content_track_signature"):
        warnings.append("A/B 内容轨道的类型、名称或片段数量不同，可能存在脚本改稿。")
    if baseline_inventory.get("content_visual_signature") != packaged_inventory.get("content_visual_signature"):
        warnings.append("A/B 视频或图片素材的内容签名不同，不能直接归因于包装。")
    baseline_text = list(baseline_inventory.get("content_text_signature") or [])
    packaged_text = list(packaged_inventory.get("content_text_signature") or [])
    if baseline_text != packaged_text[: len(baseline_text)]:
        warnings.append("A/B 原字幕文案或顺序不同，不能直接归因于包装。")
    return {
        "same_script_assumption": not warnings,
        "warnings": warnings,
        "status": "review_required" if warnings else "compatible",
    }


def capture_packaging_pair(
    baseline_source: str | Path,
    packaged_source: str | Path,
    *,
    track_role_overrides: Mapping[str, object] | None = None,
    track_binding_overrides: Mapping[str, Any] | None = None,
    enforce_confirmation: bool = False,
    shot_binding_manifest: Mapping[str, Any] | None = None,
    raw_diff_limit: int = DEFAULT_RAW_DIFF_LIMIT,
) -> dict[str, Any]:
    """读取 A/B 草稿并生成隔离测试区候选抓取结果。"""

    baseline_path = _draft_content_path(baseline_source)
    packaged_path = _draft_content_path(packaged_source)
    try:
        baseline_draft = load_draft_content(baseline_path)
        packaged_draft = load_draft_content(packaged_path)
    except Exception as exc:  # normalize third-party/JSON errors into lab contract
        raise PackagingCaptureError(f"读取 A/B 草稿失败：{type(exc).__name__}: {exc}") from exc

    baseline_manifest = _source_manifest(baseline_source, baseline_path, baseline_draft)
    packaged_manifest = _source_manifest(packaged_source, packaged_path, packaged_draft)
    baseline_inventory = _inventory(baseline_draft)
    packaged_inventory = _inventory(packaged_draft)
    baseline_projection = _raw_projection(baseline_draft)
    packaged_projection = _raw_projection(packaged_draft)
    raw_changes, raw_truncated = _raw_diff(
        baseline_projection,
        packaged_projection,
        limit=max(1, int(raw_diff_limit)),
    )

    baseline_analysis = analyze_full_draft_style(baseline_draft, source_path=str(Path(baseline_source).expanduser().resolve()))
    packaged_analysis = analyze_full_draft_style(packaged_draft, source_path=str(Path(packaged_source).expanduser().resolve()))
    baseline_timing = None
    packaged_timing = None
    timing_warnings: list[str] = []
    try:
        baseline_timing = normalize_template_timing(baseline_draft)
        packaged_timing = normalize_template_timing(packaged_draft)
    except (OSError, ValueError, VideoPackagingBundleError) as exc:
        timing_warnings.append(f"关键帧相对时间分析未完成：{type(exc).__name__}: {exc}")
    semantic_diff = diff_full_draft_style(
        baseline_analysis,
        packaged_analysis,
        baseline_timing=baseline_timing,
        current_timing=packaged_timing,
    )
    role_manifest = infer_track_roles(packaged_analysis, track_role_overrides)
    parameter_catalog = build_parameter_catalog(
        packaged_draft,
        baseline_draft=baseline_draft,
        track_roles=role_manifest,
        track_binding_overrides=track_binding_overrides,
        enforce_confirmation=enforce_confirmation,
    )
    confirmation = copy.deepcopy(parameter_catalog.get("confirmation") or {})
    compatibility = _compatibility(
        baseline_manifest,
        packaged_manifest,
        baseline_inventory,
        packaged_inventory,
    )
    if timing_warnings:
        compatibility = dict(compatibility)
        compatibility["warnings"] = list(compatibility.get("warnings") or []) + timing_warnings
        compatibility["status"] = "review_required"

    candidate_package = {
        "schema_version": PACKAGING_CAPTURE_SCHEMA_VERSION,
        "package_type": "video_packaging_candidate",
        "status": "pending_review" if role_manifest["needs_confirmation"] or compatibility["status"] != "compatible" or semantic_diff.get("resource_review") else "ready_for_review",
        "source": {
            "baseline": baseline_manifest,
            "packaged": packaged_manifest,
            "comparison": "same_script_a_to_b",
        },
        "timeline_unit": TIMELINE_UNIT,
        "shot_binding_manifest": copy.deepcopy(dict(shot_binding_manifest or {})),
        "track_roles": role_manifest,
        "parameters": copy.deepcopy(semantic_diff.get("packaging_parameters") or {}),
        "raw_change_count": len(raw_changes),
        "semantic_change_count": int(semantic_diff.get("changed_count") or 0),
        "resource_review": copy.deepcopy(semantic_diff.get("resource_review") or []),
        "compatibility": compatibility,
        "confirmation": confirmation,
        "parameter_catalog_schema_version": PARAMETER_CATALOG_SCHEMA_VERSION,
        "parameter_catalog_file": "parameter_catalog.json",
        "write_policy": "candidate_only_no_production_write",
    }
    return {
        "schema_version": PACKAGING_CAPTURE_SCHEMA_VERSION,
        "status": candidate_package["status"],
        "created_at": int(time.time()),
        "source": {"baseline": baseline_manifest, "packaged": packaged_manifest},
        "inventory": {"baseline": baseline_inventory, "packaged": packaged_inventory},
        "analysis_summary": {
            "baseline": _analysis_summary(baseline_analysis),
            "packaged": _analysis_summary(packaged_analysis),
        },
        "compatibility": compatibility,
        "track_roles": role_manifest,
        "confirmation": confirmation,
        "raw_diff": {
            "schema_version": RAW_DIFF_SCHEMA_VERSION,
            "change_count": len(raw_changes),
            "truncated": raw_truncated,
            "changes": raw_changes,
        },
        "semantic_diff": semantic_diff,
        "candidate_package": candidate_package,
        "parameter_catalog": parameter_catalog,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def write_capture_artifact(result: Mapping[str, Any], output_dir: str | Path) -> dict[str, Any]:
    """把候选结果写入隔离区目录，不写入正式包装包根目录。"""

    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise PackagingCaptureError(f"隔离测试输出目录已存在，为避免覆盖已停止：{target}")
    target.mkdir(parents=True, exist_ok=False)
    _write_json(target / "capture_result.json", result)
    _write_json(target / "raw_diff.json", result.get("raw_diff") or {})
    _write_json(target / "semantic_diff.json", result.get("semantic_diff") or {})
    _write_json(target / "candidate_package.json", result.get("candidate_package") or {})
    _write_json(target / "track_roles.json", result.get("track_roles") or {})
    _write_json(target / "inventory.json", result.get("inventory") or {})
    _write_json(target / "parameter_catalog.json", result.get("parameter_catalog") or {})
    return {"output_dir": str(target), "files": sorted(str(path.relative_to(target)) for path in target.iterdir())}


__all__ = [
    "PACKAGING_CAPTURE_SCHEMA_VERSION",
    "PARAMETER_CATALOG_SCHEMA_VERSION",
    "PackagingCaptureError",
    "build_parameter_catalog",
    "capture_packaging_pair",
    "infer_track_roles",
    "write_capture_artifact",
]
