"""构建可复用的视频包装包。

包装包以独立的剪映母版草稿为输入，保留完整原始快照，同时生成供后续
应用层使用的相对时间投影。所有模板时间都相对于母版草稿时长或片段时长，
不会把母版中的绝对微秒时间直接带到目标镜头。
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .editing_style_package import load_draft_content
from .video_style_package import bind_packaging_manifest


SCHEMA_VERSION = "video-packaging-bundle-v1"
CATEGORY_SCHEMA_VERSION = "video-template-category-v1"
TIMING_SCHEMA_VERSION = "relative-timing-v1"
TIMELINE_UNIT = "microseconds"
REQUIRED_CATEGORIES = (
    "opening",
    "image",
    "digital_human",
    "aigc",
    "explanation",
    "mixed_explanation",
    "ending",
)
CATEGORY_LABELS = {
    "opening": "片头",
    "image": "图片",
    "digital_human": "数字人",
    "aigc": "AIGC",
    "explanation": "说明镜头",
    "mixed_explanation": "混合说明镜头",
    "ending": "片尾",
}
_TIMERANGE_KEYS = {"target_timerange", "time_range", "apply_time_range"}
_MATERIAL_TIME_COLLECTIONS = {
    "video_effects",
    "material_animations",
    "transitions",
    "audio_fades",
    "audio_effects",
    "speeds",
    "vocal_separations",
}
_CONTENT_MATERIAL_COLLECTIONS = {
    "videos": "visual_media",
    "images": "image",
    "audios": "audio",
    "digital_humans": "digital_human",
    "drafts": "nested_draft",
    "texts": "text",
}
_TEXT_CONTENT_KEYS = {
    "content",
    "base_content",
    "words",
    "subtitle_keywords",
    "text_to_audio_ids",
    "recognize_task_id",
}
_CONTENT_FILE_SUFFIXES = {
    ".3g2",
    ".3gp",
    ".aac",
    ".avi",
    ".flac",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".png",
    ".wav",
    ".webm",
    ".webp",
}

_COLLECTED_COMPONENT_KEYS = ("effects", "keyframes", "transitions", "text_templates")
FULL_DRAFT_STYLE_SCHEMA_VERSION = "full-draft-style-v1"


class VideoPackagingBundleError(ValueError):
    """包装包输入、提取或校验失败。"""


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


_VIDEO_AUDIO_SUFFIXES = frozenset({
    ".3g2", ".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm", ".wmv",
})


def _material_aliases(item: Mapping[str, Any]) -> list[str]:
    return [
        _text(item.get(key))
        for key in ("id", "material_id", "local_material_id", "local_id")
        if _text(item.get(key))
    ]


def _has_embedded_video_audio(material: Mapping[str, Any]) -> bool:
    value = material.get("has_audio")
    if isinstance(value, str):
        has_audio = value.strip().lower() in {"1", "true", "yes", "y"}
    else:
        has_audio = bool(value)
    if not has_audio:
        return False
    media_path = _text(
        material.get("path")
        or material.get("media_path")
        or material.get("material_url")
    ).split("?", 1)[0]
    return Path(media_path).suffix.lower() in _VIDEO_AUDIO_SUFFIXES


def _audio_track_style(
    raw_track: Mapping[str, Any],
    track_index: int,
    material_index: Mapping[str, tuple[str, Mapping[str, Any]]],
    *,
    source_type: str,
    video_material_index: Mapping[str, Mapping[str, Any]] | None = None,
    audio_material_index: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """统一提取独立音频轨和视频内嵌原声轨的音量信息。"""

    track = _dict(raw_track)
    segments: list[dict[str, Any]] = []
    volumes: list[float] = []
    effects: list[dict[str, Any]] = []
    for segment_index, raw_segment in enumerate(_list(track.get("segments"))):
        segment = _dict(raw_segment)
        material: Mapping[str, Any] | None = None
        if source_type == "video_original_audio":
            material = (video_material_index or {}).get(_text(segment.get("material_id")))
            if material is None or not _has_embedded_video_audio(material):
                continue
        elif source_type == "audio_track":
            material = (audio_material_index or {}).get(_text(segment.get("material_id")))
        volume = _number(segment.get("volume"), 1.0)
        volumes.append(volume)
        target = _dict(segment.get("target_timerange"))
        refs = []
        for ref in _list(segment.get("extra_material_refs")):
            kind_item = material_index.get(_text(ref))
            if kind_item and kind_item[0] in {"audio_effects", "audio_fades"}:
                refs.append(_style_descriptor(kind_item[1]))
        volume_keyframes = []
        for raw_group in _list(segment.get("common_keyframes")):
            group = _dict(raw_group)
            if "volume" not in _text(group.get("property_type")).lower():
                continue
            volume_keyframes.extend(
                {
                    "time_offset": _int(_dict(point).get("time_offset")),
                    "values": copy.deepcopy(_dict(point).get("values")),
                }
                for point in _list(group.get("keyframe_list"))
                if isinstance(point, Mapping)
            )
        item = {
            "segment_index": segment_index,
            "start_us": max(0, _int(target.get("start"))),
            "duration_us": max(0, _int(target.get("duration"))),
            "volume": volume,
            "volume_percent": round(volume * 100, 3),
            "volume_keyframes": volume_keyframes,
            "effects": refs,
        }
        if material is not None:
            item["material_name"] = _text(material.get("material_name") or material.get("name"))
            item["material_path"] = _text(material.get("path") or material.get("media_path"))
            item["material_type"] = _text(material.get("type") or material.get("category"))
            item["is_sound_effect"] = _text(material.get("type") or material.get("category")).lower() == "sound"
        segments.append(item)
        effects.extend(refs)
    if not segments:
        return None
    material_names = list(dict.fromkeys(
        _text(segment.get("material_name"))
        for segment in segments
        if _text(segment.get("material_name"))
    ))
    material_types = {
        _text(segment.get("material_type")).lower()
        for segment in segments
        if _text(segment.get("material_type"))
    }
    sound_effects = [
        {
            "name": segment.get("material_name"),
            "type": segment.get("material_type"),
            "start_us": segment.get("start_us", 0),
            "duration_us": segment.get("duration_us", 0),
            "volume_percent": segment.get("volume_percent", 0),
        }
        for segment in segments
        if segment.get("is_sound_effect") and segment.get("material_name")
    ]
    raw_name = _text(track.get("name"))
    if source_type == "video_original_audio":
        name = f"视频原声 · {raw_name or f'视频轨道 {track_index + 1}'}"
    elif not raw_name and "sound" in material_types:
        name = f"音效 · {'、'.join(material_names[:2]) or f'轨道 {track_index + 1}'}"
    elif not raw_name and material_names:
        name = f"音频 · {'、'.join(material_names[:2])}"
    else:
        name = raw_name or f"audio_track_{track_index + 1}"
    if "sound" in material_types:
        track_role = "sound_effect"
    elif "music" in material_types:
        track_role = "music"
    else:
        track_role = "audio"
    return {
        "track_index": track_index,
        "name": name,
        "source_type": source_type,
        "source_track_type": _text(track.get("type") or track.get("track_type")),
        "track_role": track_role,
        "is_video_original_audio": source_type == "video_original_audio",
        "muted": bool(track.get("muted", False)),
        "hidden": bool(track.get("hidden", False)),
        "track_volume": _number(track.get("volume")) if track.get("volume") is not None else None,
        "segment_count": len(segments),
        "volume": {
            "min": min(volumes) if volumes else 0,
            "max": max(volumes) if volumes else 0,
            "average": round(sum(volumes) / len(volumes), 6) if volumes else 0,
            "min_percent": round(min(volumes) * 100, 3) if volumes else 0,
            "max_percent": round(max(volumes) * 100, 3) if volumes else 0,
            "average_percent": round(sum(volumes) / len(volumes) * 100, 3) if volumes else 0,
        },
        "segments": segments,
        "effect_count": len(effects),
        "sound_effects": sound_effects,
    }


def _count_keyframe_points(value: Any) -> int:
    """统计草稿中所有 keyframe_list 的关键帧点，不依赖具体轨道结构。"""

    if isinstance(value, Mapping):
        total = 0
        for key, child in value.items():
            if key == "keyframe_list":
                total += len(_list(child))
            else:
                total += _count_keyframe_points(child)
        return total
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return sum(_count_keyframe_points(child) for child in value)
    return 0


def summarize_template_components(draft: Mapping[str, Any]) -> dict[str, int]:
    """返回模板卡片使用的简要采集清单。"""

    materials = _dict(draft.get("materials"))
    return {
        "effects": sum(len(_list(materials.get(key))) for key in ("video_effects", "effects", "plugin_effects")),
        "keyframes": _count_keyframe_points(draft),
        "transitions": len(_list(materials.get("transitions"))),
        "text_templates": len(_list(materials.get("text_templates"))),
    }


def _style_descriptor(item: Mapping[str, Any]) -> dict[str, Any]:
    """保留可复用的视觉参数，排除路径、文案和草稿随机 ID。"""

    blocked_exact = {"id", "material_id", "segment_id", "draft_id", "content", "text", "words"}
    blocked_tokens = ("path", "url", "uri", "token", "secret", "password")

    def clean(value: Any, key: str = "") -> Any:
        lowered = key.lower()
        if lowered in blocked_exact or any(token in lowered for token in blocked_tokens):
            return None
        if isinstance(value, Mapping):
            result = {}
            for raw_key, raw_value in value.items():
                child = clean(raw_value, str(raw_key))
                if child not in (None, "", [], {}):
                    result[str(raw_key)] = child
            return result
        if isinstance(value, list):
            return [clean(child, key) for child in value]
        return copy.deepcopy(value)

    return clean(dict(item)) or {}


def _text_style_detail(material: Mapping[str, Any]) -> dict[str, Any]:
    """提取字幕可复用的完整样式参数，不保留字幕原文。"""

    fields = (
        "type", "alignment", "typesetting", "line_spacing", "line_max_width",
        "force_apply_line_max_width", "line_feed", "global_alpha", "text_alpha",
        "font_size", "text_size", "font_name", "font_title", "font_category_name",
        "letter_spacing", "bold_width", "italic_degree", "underline", "underline_offset",
        "underline_width", "border_alpha", "border_color", "border_width",
        "background_alpha", "background_color", "background_height",
        "background_horizontal_offset", "background_round_radius", "background_style",
        "background_vertical_offset", "background_width", "has_shadow", "shadow_alpha",
        "shadow_angle", "shadow_color", "shadow_distance", "shadow_smoothing",
        "style_name", "preset_name", "preset_category", "text_preset_resource_id",
    )
    result = {key: copy.deepcopy(material[key]) for key in fields if key in material}
    content = material.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            content = {}
    styles = []
    for raw_style in _list(_dict(content).get("styles")):
        style = _dict(raw_style)
        fill = _dict(style.get("fill"))
        fill_content = _dict(fill.get("content"))
        solid = _dict(fill_content.get("solid"))
        strokes = []
        for raw_stroke in _list(style.get("strokes")):
            stroke = _dict(raw_stroke)
            stroke_content = _dict(stroke.get("content"))
            stroke_solid = _dict(stroke_content.get("solid"))
            strokes.append({
                "width": stroke.get("width"),
                "alpha": stroke_solid.get("alpha"),
                "color": copy.deepcopy(stroke_solid.get("color")),
            })
        styles.append({
            "size": style.get("size"),
            "bold": bool(style.get("bold", False)),
            "italic": bool(style.get("italic", False)),
            "underline": bool(style.get("underline", False)),
            "fill_render_type": fill_content.get("render_type"),
            "fill_alpha": solid.get("alpha"),
            "fill_color": copy.deepcopy(solid.get("color")),
            "strokes": strokes,
            "font": _style_descriptor(_dict(style.get("font"))),
        })
    result["rich_text_styles"] = styles
    return _style_descriptor(result)


def _merge_intervals(intervals: Sequence[tuple[int, int]]) -> tuple[int, int]:
    ordered = sorted((max(0, start), max(0, end)) for start, end in intervals if end > start)
    if not ordered:
        return 0, 0
    merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(item[1] - item[0] for item in merged), len(merged)


def analyze_full_draft_style(draft: Mapping[str, Any], *, source_path: str = "") -> dict[str, Any]:
    """识别完整草稿中的全片风格参数，不复制任何内容素材。"""

    materials = _dict(draft.get("materials"))
    duration_us = max(0, _int(draft.get("duration")))
    if duration_us <= 0:
        duration_us = 0
        for raw_track in _list(draft.get("tracks")):
            for raw_segment in _list(_dict(raw_track).get("segments")):
                segment = _dict(raw_segment)
                target = _dict(segment.get("target_timerange"))
                duration_us = max(
                    duration_us,
                    _int(target.get("start")) + _int(target.get("duration")),
                )
    material_index: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for collection in ("video_effects", "effects", "plugin_effects", "filters", "transitions", "audio_effects", "audio_fades"):
        for raw_item in _list(materials.get(collection)):
            item = _dict(raw_item)
            identity = _material_identity(item)
            if identity:
                material_index[identity] = (collection, item)

    video_material_index: dict[str, Mapping[str, Any]] = {}
    for raw_item in _list(materials.get("videos")):
        item = _dict(raw_item)
        for alias in _material_aliases(item):
            video_material_index[alias] = item

    audio_material_index: dict[str, Mapping[str, Any]] = {}
    for raw_item in _list(materials.get("audios")):
        item = _dict(raw_item)
        for alias in _material_aliases(item):
            audio_material_index[alias] = item

    audio_tracks: list[dict[str, Any]] = []
    for track_index, raw_track in enumerate(_list(draft.get("tracks"))):
        track = _dict(raw_track)
        track_type = _text(track.get("type") or track.get("track_type"))
        if track_type == "audio":
            parsed_track = _audio_track_style(
                track,
                track_index,
                material_index,
                source_type="audio_track",
                audio_material_index=audio_material_index,
            )
        elif track_type == "video":
            parsed_track = _audio_track_style(
                track,
                track_index,
                material_index,
                source_type="video_original_audio",
                video_material_index=video_material_index,
                audio_material_index=audio_material_index,
            )
        else:
            parsed_track = None
        if parsed_track is not None:
            audio_tracks.append(parsed_track)

    visual_tracks: list[dict[str, Any]] = []
    applied_visual_ids: set[str] = set()
    for track_index, raw_track in enumerate(_list(draft.get("tracks"))):
        track = _dict(raw_track)
        track_type = _text(track.get("type") or track.get("track_type"))
        if track_type not in {"effect", "filter"}:
            continue
        intervals = []
        refs: set[str] = set()
        segments = _list(track.get("segments"))
        for raw_segment in segments:
            segment = _dict(raw_segment)
            timerange = _dict(segment.get("target_timerange"))
            start = max(0, _int(timerange.get("start")))
            end = start + max(0, _int(timerange.get("duration")))
            intervals.append((start, end))
            for key in ("material_id", "effect_id", "filter_id"):
                value = _text(segment.get(key))
                if value in material_index:
                    refs.add(value)
            for ref in _list(segment.get("extra_material_refs")):
                if _text(ref) in material_index:
                    refs.add(_text(ref))
        covered_us, _ = _merge_intervals(intervals)
        track_start = min((start for start, _end in intervals), default=0)
        track_end = max((end for _start, end in intervals), default=0)
        full_timeline = bool(duration_us and track_start <= max(1, duration_us // 1000) and track_end >= duration_us - max(1, duration_us // 1000))
        track_items = []
        for ref in sorted(refs):
            collection, material = material_index[ref]
            applied_visual_ids.add(ref)
            track_items.append({
                "collection": collection,
                "material": _style_descriptor(material),
                "coverage_ratio": round(covered_us / duration_us, 6) if duration_us else 0,
                "full_timeline": full_timeline,
            })
        visual_tracks.append({
            "track_index": track_index,
            "track_name": _text(track.get("name")) or f"{track_type}_track_{track_index + 1}",
            "track_type": track_type,
            "segment_count": len(segments),
            "coverage_ratio": round(covered_us / duration_us, 6) if duration_us else 0,
            "full_timeline": full_timeline,
            "materials": track_items,
        })
    # 某些剪映版本把全片效果的范围写在素材本身，而不是独立轨道片段中。
    # 两种写法都识别，避免只看 track.type 漏掉全局滤镜。
    for collection in ("video_effects", "effects", "plugin_effects", "filters"):
        for raw_item in _list(materials.get(collection)):
            item = _dict(raw_item)
            identity = _material_identity(item)
            if not identity or identity in applied_visual_ids:
                continue
            timerange = next(
                (
                    _dict(item.get(key))
                    for key in ("apply_time_range", "time_range")
                    if isinstance(item.get(key), Mapping)
                ),
                {},
            )
            start = max(0, _int(timerange.get("start")))
            item_duration = max(0, _int(timerange.get("duration")))
            if duration_us and start <= max(1, duration_us // 1000) and start + item_duration >= duration_us - max(1, duration_us // 1000):
                visual_tracks.append({
                    "track_index": -1,
                    "track_name": "素材范围",
                    "track_type": "filter" if collection == "filters" else "effect",
                    "segment_count": 0,
                    "coverage_ratio": 1.0,
                    "full_timeline": True,
                    "materials": [{
                        "collection": collection,
                        "material": _style_descriptor(item),
                        "coverage_ratio": 1.0,
                        "full_timeline": True,
                    }],
                })
                applied_visual_ids.add(identity)
    overall_effects = [
        item
        for track in visual_tracks
        if track["full_timeline"] and track["coverage_ratio"] >= 0.999 and track["track_type"] == "effect"
        for item in track["materials"]
    ]
    overall_filters = [
        item
        for track in visual_tracks
        if track["full_timeline"] and track["coverage_ratio"] >= 0.999 and track["track_type"] == "filter"
        for item in track["materials"]
    ]

    transition_materials = {
        _material_identity(_dict(item)): _dict(item)
        for item in _list(materials.get("transitions"))
        if _material_identity(_dict(item))
    }
    applied_transitions: list[dict[str, Any]] = []
    for track_index, raw_track in enumerate(_list(draft.get("tracks"))):
        track = _dict(raw_track)
        for segment_index, raw_segment in enumerate(_list(track.get("segments"))):
            segment = _dict(raw_segment)
            target_timerange = _dict(segment.get("target_timerange"))
            segment_start_us = max(0, _int(target_timerange.get("start")))
            segment_duration_us = max(0, _int(target_timerange.get("duration")))
            transition_at_us = segment_start_us + segment_duration_us
            refs = {_text(segment.get(key)) for key in ("transition_id", "transition_material_id")}
            refs.update(_text(ref) for ref in _list(segment.get("extra_material_refs")))
            direct = segment.get("transition")
            if isinstance(direct, Mapping):
                applied_transitions.append({
                    "track_index": track_index,
                    "segment_index": segment_index,
                    "track_name": _text(track.get("name")),
                    "transition": _style_descriptor(direct),
                    "segment_start_us": segment_start_us,
                    "segment_end_us": transition_at_us,
                    "transition_at_us": transition_at_us,
                    "transition_duration_us": max(0, _int(_dict(direct).get("duration"))),
                })
            for ref in sorted(refs.intersection(transition_materials)):
                applied_transitions.append({
                    "track_index": track_index,
                    "segment_index": segment_index,
                    "track_name": _text(track.get("name")),
                    "transition": _style_descriptor(transition_materials[ref]),
                    "segment_start_us": segment_start_us,
                    "segment_end_us": transition_at_us,
                    "transition_at_us": transition_at_us,
                    "transition_duration_us": max(0, _int(_dict(transition_materials[ref]).get("duration"))),
                })

    transition_audio_companions: list[dict[str, Any]] = []
    for transition_index, entry in enumerate(applied_transitions):
        at_us = _int(entry.get("transition_at_us"))
        companions: list[dict[str, Any]] = []
        for track in audio_tracks:
            if track.get("track_role") != "sound_effect":
                continue
            for segment in _list(track.get("segments")):
                if not segment.get("is_sound_effect"):
                    continue
                start_us = _int(segment.get("start_us"))
                end_us = start_us + max(0, _int(segment.get("duration_us")))
                # Jianying usually places the sound effect across the seam;
                # accept a small boundary tolerance for rounded microseconds.
                overlaps = start_us <= at_us <= end_us
                near_start = abs(start_us - at_us) <= 500_000
                near_end = abs(end_us - at_us) <= 500_000
                if not (overlaps or near_start or near_end):
                    continue
                companion = {
                    "transition_index": transition_index,
                    "transition_at_us": at_us,
                    "track_name": track.get("name", ""),
                    "material_name": segment.get("material_name", ""),
                    "material_type": segment.get("material_type", ""),
                    "start_us": start_us,
                    "duration_us": max(0, _int(segment.get("duration_us"))),
                    "volume_percent": segment.get("volume_percent", 0),
                }
                companions.append(companion)
                transition_audio_companions.append(companion)
        entry["audio_companions"] = companions

    text_materials = [_dict(item) for item in _list(materials.get("texts")) if isinstance(item, Mapping)]
    text_profiles: dict[str, dict[str, Any]] = {}
    for item in text_materials:
        profile = _text_style_detail(item)
        key = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in text_profiles:
            text_profiles[key] = {"count": 0, "profile": profile}
        text_profiles[key]["count"] += 1

    return {
        "schema_version": FULL_DRAFT_STYLE_SCHEMA_VERSION,
        "source": {"path": source_path, "draft_name": _text(draft.get("name")), "duration_us": duration_us},
        "audio": {"track_count": len(audio_tracks), "tracks": audio_tracks},
        "visual": {
            "effect_track_count": sum(1 for item in visual_tracks if item["track_type"] == "effect"),
            "filter_track_count": sum(1 for item in visual_tracks if item["track_type"] == "filter"),
            "tracks": visual_tracks,
            "has_full_timeline_effect": bool(overall_effects),
            "has_full_timeline_filter": bool(overall_filters),
            "overall_effects": overall_effects,
            "overall_filters": overall_filters,
            "untracked_visual_material_count": len(
                [item for item in ("video_effects", "effects", "plugin_effects", "filters")
                 for material in _list(materials.get(item))
                 if _material_identity(_dict(material)) not in applied_visual_ids]
            ),
        },
        "subtitles": {
            "text_material_count": len(text_materials),
            "profile_count": len(text_profiles),
            "profiles": list(text_profiles.values()),
        },
        "transitions": {
            "defined_count": len(transition_materials),
            "applied_count": len(applied_transitions),
            "applied": applied_transitions,
            "audio_companion_count": len(transition_audio_companions),
            "audio_companions": transition_audio_companions,
            "defined_not_applied": [
                _style_descriptor(item)
                for identity, item in transition_materials.items()
                if not any(
                    json.dumps(_style_descriptor(item), ensure_ascii=False, sort_keys=True)
                    == json.dumps(entry.get("transition") or {}, ensure_ascii=False, sort_keys=True)
                    for entry in applied_transitions
                )
            ],
        },
    }


def _diff_number(before: Any, after: Any, *, precision: int = 3) -> bool:
    try:
        return round(float(before), precision) != round(float(after), precision)
    except (TypeError, ValueError):
        return before != after


def _diff_material_name(item: Mapping[str, Any]) -> str:
    material = _dict(item.get("material"))
    return _text(
        material.get("name")
        or material.get("effect_name")
        or material.get("effect_id")
        or material.get("resource_id")
        or item.get("name")
        or item.get("effect_id")
        or item.get("resource_id")
    )


def diff_full_draft_style(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    baseline_timing: Mapping[str, Any] | None = None,
    current_timing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """比较包装包基线和用户微调后的完整草稿，并生成可回写参数。

    只把当前实现能够安全回写的字段放入 ``packaging_parameters``；新增或
    删除的素材同时标记为需要人工确认，避免把陌生资源自动写入包装包。
    """

    changes: list[dict[str, Any]] = []
    parameters: dict[str, list[dict[str, Any]]] = {
        "audio_tracks": [],
        "overall_effects": [],
        "overall_filters": [],
        "transitions": [],
        "subtitle_profiles": [],
        "keyframes": [],
    }
    resource_review: list[dict[str, Any]] = []

    def add_change(area: str, index: int, field: str, before: Any, after: Any, *, key: str = "") -> None:
        if before == after or (_diff_number(before, after) is False and isinstance(before, (int, float, str))):
            return
        changes.append({
            "area": area,
            "index": index,
            "key": key,
            "field": field,
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
        })

    baseline_audio = _list(_dict(baseline.get("audio")).get("tracks"))
    current_audio = _list(_dict(current.get("audio")).get("tracks"))
    for index in range(max(len(baseline_audio), len(current_audio))):
        before = _dict(baseline_audio[index]) if index < len(baseline_audio) else {}
        after = _dict(current_audio[index]) if index < len(current_audio) else {}
        key = _text(after.get("name") or before.get("name")) or f"audio_track_{index + 1}"
        if not before or not after:
            resource_review.append({"area": "audio_tracks", "index": index, "reason": "track_added_or_removed", "key": key})
            continue
        before_volume = _dict(before.get("volume")).get("average_percent", 0)
        after_volume = _dict(after.get("volume")).get("average_percent", 0)
        if _diff_number(before_volume, after_volume):
            add_change("audio_tracks", index, "volume_percent", before_volume, after_volume, key=key)
        if bool(before.get("muted")) != bool(after.get("muted")):
            add_change("audio_tracks", index, "muted", bool(before.get("muted")), bool(after.get("muted")), key=key)
        row: dict[str, Any] = {"index": index, "track_name": key}
        if _diff_number(before_volume, after_volume):
            row["volume_percent"] = round(float(after_volume), 3)
        if bool(before.get("muted")) != bool(after.get("muted")):
            row["muted"] = bool(after.get("muted"))
        if len(row) > 2:
            parameters["audio_tracks"].append(row)

    def compare_visual(area: str) -> None:
        before_items = _list(_dict(baseline.get("visual")).get(area))
        after_items = _list(_dict(current.get("visual")).get(area))
        for index in range(max(len(before_items), len(after_items))):
            before = _dict(before_items[index]) if index < len(before_items) else {}
            after = _dict(after_items[index]) if index < len(after_items) else {}
            key = _diff_material_name(after or before) or f"{area}_{index + 1}"
            if not before or not after:
                resource_review.append({"area": area, "index": index, "reason": "material_added_or_removed", "key": key})
                if before and not after:
                    parameters[area].append({"index": index, "enabled": False})
                elif after:
                    parameters[area].append({"index": index, "enabled": True, "requires_resource_mapping": True})
                continue
            before_coverage = float(before.get("coverage_ratio") or 0) * 100
            after_coverage = float(after.get("coverage_ratio") or 0) * 100
            if _diff_number(before_coverage, after_coverage):
                add_change(area, index, "coverage_percent", before_coverage, after_coverage, key=key)
                parameters[area].append({"index": index, "coverage_percent": round(after_coverage, 3), "enabled": True})

    compare_visual("overall_effects")
    compare_visual("overall_filters")

    baseline_transitions = _list(_dict(baseline.get("transitions")).get("applied"))
    current_transitions = _list(_dict(current.get("transitions")).get("applied"))
    for index in range(max(len(baseline_transitions), len(current_transitions))):
        before = _dict(baseline_transitions[index]) if index < len(baseline_transitions) else {}
        after = _dict(current_transitions[index]) if index < len(current_transitions) else {}
        key = _diff_material_name({"material": _dict(after.get("transition")) or _dict(before.get("transition"))}) or f"transition_{index + 1}"
        if not before or not after:
            resource_review.append({"area": "transitions", "index": index, "reason": "transition_added_or_removed", "key": key})
            if before and not after:
                parameters["transitions"].append({"index": index, "enabled": False})
            continue
        before_duration = before.get("transition_duration_us", 0)
        after_duration = after.get("transition_duration_us", 0)
        if _diff_number(before_duration, after_duration):
            ratio = round(float(after_duration) / float(before_duration) * 100, 3) if float(before_duration or 0) else 100.0
            add_change("transitions", index, "duration_percent", before_duration, after_duration, key=key)
            parameters["transitions"].append({"index": index, "duration_percent": ratio, "enabled": True})

    def profile_value(profile: Mapping[str, Any], name: str) -> Any:
        if name == "font_name":
            return profile.get("font_name") or profile.get("font_title") or ""
        if name == "font_size":
            return profile.get("font_size") or profile.get("text_size") or 0
        return profile.get(name)

    subtitle_fields = ("font_name", "font_size", "alignment", "typesetting", "line_spacing", "has_shadow", "shadow_alpha", "shadow_distance", "shadow_smoothing", "border_width")
    baseline_profiles = _list(_dict(baseline.get("subtitles")).get("profiles"))
    current_profiles = _list(_dict(current.get("subtitles")).get("profiles"))
    for index in range(max(len(baseline_profiles), len(current_profiles))):
        before = _dict(_dict(baseline_profiles[index]).get("profile")) if index < len(baseline_profiles) else {}
        after = _dict(_dict(current_profiles[index]).get("profile")) if index < len(current_profiles) else {}
        if not before or not after:
            resource_review.append({"area": "subtitle_profiles", "index": index, "reason": "profile_added_or_removed"})
            continue
        row: dict[str, Any] = {"index": index}
        for field in subtitle_fields:
            before_value = profile_value(before, field)
            after_value = profile_value(after, field)
            equal = before_value == after_value or (not _diff_number(before_value, after_value) if isinstance(before_value, (int, float)) or isinstance(after_value, (int, float)) else False)
            if not equal:
                add_change("subtitle_profiles", index, field, before_value, after_value, key=f"style_{index + 1}")
                row[field] = copy.deepcopy(after_value)
        if len(row) > 1:
            parameters["subtitle_profiles"].append(row)

    def timing_groups(value: Mapping[str, Any] | None) -> dict[tuple[int, int], list[dict[str, Any]]]:
        result: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for raw_track in _list(_dict(value).get("tracks")):
            track = _dict(raw_track)
            track_index = _int(track.get("track_index"))
            for raw_segment in _list(track.get("segments")):
                segment = _dict(raw_segment)
                segment_index = _int(segment.get("segment_index"))
                result[(track_index, segment_index)] = copy.deepcopy(_list(segment.get("keyframes")))
        return result

    baseline_keyframes = timing_groups(baseline_timing)
    current_keyframes = timing_groups(current_timing)
    for track_index, segment_index in sorted(set(baseline_keyframes) | set(current_keyframes)):
        before = baseline_keyframes.get((track_index, segment_index), [])
        after = current_keyframes.get((track_index, segment_index), [])
        if json.dumps(before, ensure_ascii=False, sort_keys=True) == json.dumps(after, ensure_ascii=False, sort_keys=True):
            continue
        key = f"track_{track_index + 1}_segment_{segment_index + 1}"
        changes.append({
            "area": "keyframes",
            "index": segment_index,
            "key": key,
            "field": "groups",
            "before": before,
            "after": after,
        })
        parameters["keyframes"].append({
            "track_index": track_index,
            "segment_index": segment_index,
            "keyframes": after,
            "timing_unit": TIMELINE_UNIT,
            "timing_policy": "relative_to_segment_duration",
        })

    return {
        "schema_version": "full-draft-style-diff-v1",
        "status": "changed" if changes or resource_review else "unchanged",
        "changed_count": len(changes),
        "changes": changes,
        "resource_review": resource_review,
        "packaging_parameters": {key: value for key, value in parameters.items() if value},
        "summary": {
            area: sum(1 for item in changes if item["area"] == area)
            for area in ("audio_tracks", "overall_effects", "overall_filters", "transitions", "subtitle_profiles", "keyframes")
        },
    }


def _ratio(value_us: int, basis_us: int) -> float:
    if basis_us <= 0:
        raise VideoPackagingBundleError("相对时间计算的基准时长必须大于 0 微秒")
    return round(max(0, value_us) / basis_us, 9)


def _timerange_projection(timerange: Mapping[str, Any], basis_us: int) -> dict[str, Any]:
    start_us = max(0, _int(timerange.get("start")))
    duration_us = max(0, _int(timerange.get("duration")))
    return {
        "start_ratio": _ratio(start_us, basis_us),
        "duration_ratio": _ratio(duration_us, basis_us),
        "end_ratio": _ratio(start_us + duration_us, basis_us),
        "timing_basis": "source_duration",
        "basis_duration_us": basis_us,
    }


def _keyframe_projection(segment: Mapping[str, Any], segment_duration_us: int) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    uniform_scale = segment.get("uniform_scale")
    uniform_scale_on = isinstance(uniform_scale, Mapping) and bool(uniform_scale.get("on"))
    for group_index, raw_group in enumerate(_list(segment.get("common_keyframes"))):
        group = _dict(raw_group)
        points: list[dict[str, Any]] = []
        for point_index, raw_point in enumerate(_list(group.get("keyframe_list"))):
            point = _dict(raw_point)
            point_payload = {
                key: copy.deepcopy(value)
                for key, value in point.items()
                if key not in {"id", "time_offset"}
            }
            point_payload.update(
                {
                    "point_index": point_index,
                    "offset_ratio": _ratio(_int(point.get("time_offset")), segment_duration_us),
                    "timing_basis": "segment_duration",
                    "basis_duration_us": segment_duration_us,
                }
            )
            points.append(point_payload)
        property_type = _text(group.get("property_type"))
        if uniform_scale_on and property_type == "KFTypeScaleX":
            property_type = "UNIFORM_SCALE"
        groups.append(
            {
                "group_index": group_index,
                "property_type": property_type,
                "points": points,
            }
        )
    return groups


def _collect_material_time_ranges(value: Any, path: str, basis_us: int, output: list[dict[str, Any]]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key in _TIMERANGE_KEYS and isinstance(child, Mapping):
                output.append(
                    {
                        "path": child_path,
                        "projection": _timerange_projection(child, basis_us),
                    }
                )
            _collect_material_time_ranges(child, child_path, basis_us, output)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _collect_material_time_ranges(child, f"{path}[{index}]", basis_us, output)


def _normalize_tracks(draft: Mapping[str, Any], source_duration_us: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for track_index, raw_track in enumerate(_list(draft.get("tracks"))):
        track = _dict(raw_track)
        segments: list[dict[str, Any]] = []
        for segment_index, raw_segment in enumerate(_list(track.get("segments"))):
            segment = _dict(raw_segment)
            target = _dict(segment.get("target_timerange"))
            segment_duration_us = max(0, _int(target.get("duration")))
            item: dict[str, Any] = {
                "path": f"tracks[{track_index}].segments[{segment_index}]",
                "track_index": track_index,
                "segment_index": segment_index,
                "track_name": _text(track.get("name")),
                "track_type": _text(track.get("type") or track.get("track_type")),
                "target_timerange": _timerange_projection(target, source_duration_us) if target else None,
                "keyframes": _keyframe_projection(segment, segment_duration_us) if segment_duration_us else [],
                "has_keyframes": bool(_list(segment.get("common_keyframes")) or _list(segment.get("keyframe_refs"))),
            }
            segments.append(item)
        result.append(
            {
                "track_index": track_index,
                "name": _text(track.get("name")) or f"track_{track_index + 1}",
                "type": _text(track.get("type") or track.get("track_type")),
                "segment_count": len(segments),
                "segments": segments,
            }
        )
    return result


def _material_summary(draft: Mapping[str, Any], source_duration_us: int) -> dict[str, Any]:
    materials = _dict(draft.get("materials"))
    collections: dict[str, Any] = {}
    timing_ranges: list[dict[str, Any]] = []
    for key, raw_items in materials.items():
        items = _list(raw_items)
        collections[key] = {
            "count": len(items),
            "paths": [f"materials.{key}[{index}]" for index in range(len(items))],
        }
        if key in _MATERIAL_TIME_COLLECTIONS:
            _collect_material_time_ranges(items, f"materials.{key}", source_duration_us, timing_ranges)
    return {"collections": collections, "time_ranges": timing_ranges}


def normalize_template_timing(draft: Mapping[str, Any]) -> dict[str, Any]:
    """提取不依赖绝对时间的轨道、关键帧和素材时间投影。"""

    source_duration_us = _int(draft.get("duration"))
    if source_duration_us <= 0:
        track_ends: list[int] = []
        for raw_track in _list(draft.get("tracks")):
            track = _dict(raw_track)
            for raw_segment in _list(track.get("segments")):
                segment = _dict(raw_segment)
                timerange = _dict(segment.get("target_timerange"))
                track_ends.append(max(0, _int(timerange.get("start"))) + max(0, _int(timerange.get("duration"))))
        source_duration_us = max(track_ends, default=0)
    if source_duration_us <= 0:
        raise VideoPackagingBundleError("模板草稿缺少有效 duration，无法计算相对时间")
    return {
        "schema_version": TIMING_SCHEMA_VERSION,
        "timeline_unit": TIMELINE_UNIT,
        "timing_policy": "relative_to_source_template_duration",
        "source_duration_us": source_duration_us,
        "tracks": _normalize_tracks(draft, source_duration_us),
        "materials": _material_summary(draft, source_duration_us),
    }


def scale_relative_timerange(projection: Mapping[str, Any], target_duration_us: int) -> dict[str, int]:
    """把比例时间还原为目标镜头的绝对微秒时间。"""

    if target_duration_us <= 0:
        raise VideoPackagingBundleError("目标镜头时长必须大于 0 微秒")
    return {
        "start": round(float(projection.get("start_ratio", 0.0)) * target_duration_us),
        "duration": round(float(projection.get("duration_ratio", 0.0)) * target_duration_us),
    }


def scale_relative_keyframe(offset_ratio: float, target_segment_duration_us: int) -> int:
    if target_segment_duration_us <= 0:
        raise VideoPackagingBundleError("目标关键帧片段时长必须大于 0 微秒")
    return round(float(offset_ratio) * target_segment_duration_us)


def materialize_relative_keyframes(
    keyframe_groups: Sequence[Mapping[str, Any]], target_segment_duration_us: int
) -> list[dict[str, Any]]:
    """把包装包中的关键帧比例还原为剪映可写入的 time_offset。"""

    materialized: list[dict[str, Any]] = []
    for group in keyframe_groups:
        group_value = {
            key: copy.deepcopy(value)
            for key, value in group.items()
            if key not in {"group_index", "points"}
        }
        points: list[dict[str, Any]] = []
        for point in _list(group.get("points")):
            point_value = _dict(point)
            item = {
                key: copy.deepcopy(value)
                for key, value in point_value.items()
                if key not in {"point_index", "offset_ratio", "timing_basis", "basis_duration_us"}
            }
            item["time_offset"] = scale_relative_keyframe(
                float(point_value.get("offset_ratio", 0.0)), target_segment_duration_us
            )
            points.append(item)
        group_value["keyframe_list"] = points
        materialized.append(group_value)
    return materialized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_info(source_dir: Path, draft: Mapping[str, Any]) -> dict[str, Any]:
    platform = _dict(draft.get("platform"))
    return {
        "draft_name": _text(draft.get("name")) or source_dir.name,
        "source_path": str(source_dir),
        "duration_us": _int(draft.get("duration")),
        "canvas": copy.deepcopy(_dict(draft.get("canvas_config"))),
        "platform": {
            "os": _text(platform.get("os")),
            "app_version": _text(platform.get("app_version")),
            "app_source": _text(platform.get("app_source")),
        },
        "source_files": [
            {"name": str(path.relative_to(source_dir)), "sha256": _sha256_file(path), "size": path.stat().st_size}
            for path in sorted(source_dir.rglob("*"))
            if path.is_file()
        ],
    }


def _material_identity(item: Mapping[str, Any]) -> str:
    for key in ("id", "material_id", "local_material_id", "local_id"):
        value = _text(item.get(key))
        if value:
            return value
    return ""


def _content_file(path: Path) -> bool:
    return path.suffix.lower() in _CONTENT_FILE_SUFFIXES


def _sanitize_text_material(item: Mapping[str, Any], slot_id: str) -> dict[str, Any]:
    """保留字幕样式字段，但清掉母版中的实际文案和识别结果。"""

    sanitized = copy.deepcopy(dict(item))
    sanitized["id"] = slot_id
    if "material_id" in sanitized:
        sanitized["material_id"] = slot_id
    if "local_material_id" in sanitized:
        sanitized["local_material_id"] = ""
    sanitized["name"] = slot_id
    for key in _TEXT_CONTENT_KEYS:
        if key not in sanitized:
            continue
        sanitized[key] = [] if isinstance(sanitized[key], list) else ""
    return sanitized


def _replace_source_material_ids(value: Any, source_to_slot: Mapping[str, str]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _replace_source_material_ids(child, source_to_slot)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_replace_source_material_ids(child, source_to_slot) for child in value]
    if isinstance(value, str):
        return source_to_slot.get(value, value)
    return copy.deepcopy(value)


def _find_exact_values(value: Any, targets: set[str], path: str = "") -> list[str]:
    if isinstance(value, Mapping):
        paths: list[str] = []
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            paths.extend(_find_exact_values(child, targets, child_path))
        return paths
    if isinstance(value, list):
        paths: list[str] = []
        for index, child in enumerate(value):
            paths.extend(_find_exact_values(child, targets, f"{path}[{index}]"))
        return paths
    return [path] if isinstance(value, str) and value in targets else []


def _sanitize_template_draft(draft: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """把母版中的内容素材替换为槽位，保留可复用的视觉结构。"""

    sanitized = copy.deepcopy(dict(draft))
    materials = _dict(sanitized.get("materials"))
    sanitized["materials"] = materials
    source_to_slot: dict[str, str] = {}
    slots: list[dict[str, Any]] = []
    excluded_counts: dict[str, int] = {}

    for collection, media_type in _CONTENT_MATERIAL_COLLECTIONS.items():
        items = _list(materials.get(collection))
        if not items:
            continue
        excluded_counts[collection] = len(items)
        sanitized_items: list[dict[str, Any]] = []
        for index, raw_item in enumerate(items, start=1):
            item = _dict(raw_item)
            slot_id = f"slot_{media_type}_{index:03d}"
            source_id = _material_identity(item)
            if source_id:
                source_to_slot[source_id] = slot_id
            slots.append(
                {
                    "slot_id": slot_id,
                    "media_type": media_type,
                    "source_collection": collection,
                    "source_index": index - 1,
                }
            )
            if collection == "texts":
                sanitized_items.append(_sanitize_text_material(item, slot_id))
        sanitized["materials"][collection] = sanitized_items

    sanitized = _replace_source_material_ids(sanitized, source_to_slot)
    sanitized["template_slots"] = slots
    sanitized["name"] = f"template_{_text(draft.get('name')) or 'unnamed'}"
    sanitized.pop("cover", None)
    sanitized.pop("static_cover_image_path", None)

    leaks: list[str] = []
    active_materials = _dict(sanitized.get("materials"))
    for collection in _CONTENT_MATERIAL_COLLECTIONS:
        if collection == "texts":
            for index, item in enumerate(_list(active_materials.get(collection))):
                text_item = _dict(item)
                for key in _TEXT_CONTENT_KEYS:
                    value = text_item.get(key)
                    if value not in (None, "", []):
                        leaks.append(f"materials.{collection}[{index}].{key}")
        elif _list(active_materials.get(collection)):
            leaks.append(f"materials.{collection}")
    leaks.extend(_find_exact_values(sanitized, set(source_to_slot)))
    audit = {
        "content_policy": "source_content_excluded_slot_bound",
        "excluded_material_counts": excluded_counts,
        "excluded_material_count": sum(excluded_counts.values()),
        "slot_count": len(slots),
        "slots": slots,
        "content_leak_count": len(leaks),
        "content_leaks": leaks[:50],
    }
    return sanitized, audit


def _validate_category_template(category_package: Mapping[str, Any]) -> dict[str, Any]:
    validation = _dict(category_package.get("validation"))
    audit = _dict(category_package.get("content_audit"))
    template_draft = _dict(category_package.get("template_draft") or category_package.get("raw_draft"))
    collected_components = _dict(validation.get("collected_components"))
    if not collected_components:
        collected_components = summarize_template_components(template_draft)
    leaks = _list(audit.get("content_leaks"))
    if int(audit.get("content_leak_count") or 0) or leaks:
        category = _text(category_package.get("category")) or "unknown"
        paths = "、".join(str(item) for item in leaks[:5])
        raise VideoPackagingBundleError(f"{CATEGORY_LABELS.get(category, category)}模板校检发现内容变量泄漏：{paths}")
    result = dict(validation)
    result.update(
        {
            "status": "validated",
            "content_variables_excluded": True,
            "content_excluded_count": int(audit.get("excluded_material_count") or 0),
            "content_leak_count": 0,
            "relative_timing_ready": bool(validation.get("relative_timing_ready", False)),
            "collected_components": {
                key: _int(collected_components.get(key)) for key in _COLLECTED_COMPONENT_KEYS
            },
        }
    )
    return result


def extract_category_template(category: str, source_draft: str | Path) -> dict[str, Any]:
    """从一个独立剪映草稿提取完整分类模板包。"""

    category = _text(category)
    if not category:
        raise VideoPackagingBundleError(f"不支持的包装分类：{category}")
    source_dir = Path(source_draft).expanduser().resolve()
    if not source_dir.is_dir():
        raise VideoPackagingBundleError(f"分类模板草稿不存在或不是文件夹：{source_dir}")
    draft = load_draft_content(source_dir)
    normalized = normalize_template_timing(draft)
    template_draft, content_audit = _sanitize_template_draft(draft)
    collected_components = summarize_template_components(template_draft)
    validation = {
        "status": "extracted",
        "raw_data_preserved": True,
        "relative_timing_ready": True,
        "timeline_unit": TIMELINE_UNIT,
        "content_variables_excluded": True,
        "content_excluded_count": int(content_audit.get("excluded_material_count") or 0),
        "content_leak_count": int(content_audit.get("content_leak_count") or 0),
        "collected_components": collected_components,
    }
    return {
        "schema_version": CATEGORY_SCHEMA_VERSION,
        "package_type": "video_template_category",
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, category),
        "source": _source_info(source_dir, draft),
        # 保留原字段名兼容旧调用方，但这里已经是去内容后的可复用草稿。
        "raw_draft": template_draft,
        "template_draft": template_draft,
        "normalized_timing": normalized,
        "content_audit": content_audit,
        "slot_policy": {
            "mode": "declared_slots_only",
            "content_binding": "target_shot",
            "slots": content_audit.get("slots", []),
            "unresolved_slots": [],
            "source_data_preserved": True,
            "source_content_excluded": True,
        },
        "validation": validation,
    }


def _safe_bundle_name(value: str) -> str:
    name = _text(value) or "video_packaging_bundle"
    if name in {".", ".."} or any(char in name for char in '<>:"/\\|?*'):
        raise VideoPackagingBundleError("包装包名称包含 Windows 不允许的字符")
    return name


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_category_package(category_package: Mapping[str, Any], source_dir: Path, category_dir: Path) -> dict[str, Any]:
    category_dir.mkdir(parents=True, exist_ok=False)
    raw_dir = category_dir / "raw_snapshot"
    raw_dir.mkdir(parents=True, exist_ok=False)
    _write_json(raw_dir / "draft_content.json", category_package["template_draft"])
    _write_json(raw_dir / "template_slots.json", category_package["slot_policy"].get("slots", []))
    _write_json(
        category_dir / "source_audit.json",
        {
            "source": category_package["source"],
            "content_audit": category_package["content_audit"],
            "snapshot_policy": "sanitized_template_no_source_media",
        },
    )
    written_package = copy.deepcopy(dict(category_package))
    written_package["validation"] = _validate_category_template(written_package)
    written_package["snapshot_policy"] = "sanitized_template_no_source_media"
    _write_json(category_dir / "category_manifest.json", written_package)
    _write_json(category_dir / "normalized_timing.json", category_package["normalized_timing"])
    return {
        "category": category_package["category"],
        "label": category_package["category_label"],
        "path": str(category_dir.name),
        "raw_snapshot": str((category_dir / "raw_snapshot").relative_to(category_dir.parent.parent)),
        "source": category_package["source"],
        "validation": written_package["validation"],
        "content_audit": category_package["content_audit"],
        "collected_components": written_package["validation"].get("collected_components", {}),
        "snapshot_policy": "sanitized_template_no_source_media",
    }


def build_video_packaging_bundle(
    category_sources: Mapping[str, str | Path],
    output_dir: str | Path,
    *,
    bundle_name: str = "",
    version: str = "1.0",
    require_all_categories: bool = True,
    video_style_package: Mapping[str, Any] | None = None,
    full_draft_source: str | Path | None = None,
) -> dict[str, Any]:
    """把多个分类母版草稿打包成一个不可覆盖源草稿的包装包。"""

    sources = {str(key).strip(): value for key, value in category_sources.items() if str(key).strip()}
    if any(not category for category in sources):
        raise VideoPackagingBundleError("存在空的包装分类")
    required_categories = list((video_style_package or {}).get("required_categories") or REQUIRED_CATEGORIES)
    missing = [category for category in required_categories if category not in sources]
    if require_all_categories and missing:
        raise VideoPackagingBundleError(f"包装包缺少分类母版：{', '.join(CATEGORY_LABELS[item] for item in missing)}")
    if not sources:
        raise VideoPackagingBundleError("至少需要一个分类母版草稿")

    full_draft_style: dict[str, Any] | None = None
    if full_draft_source is not None and _text(full_draft_source):
        full_source_dir = Path(full_draft_source).expanduser().resolve()
        if not full_source_dir.is_dir():
            raise VideoPackagingBundleError(f"完整视频草稿不存在或不是文件夹：{full_source_dir}")
        full_draft = load_draft_content(full_source_dir)
        full_draft_style = analyze_full_draft_style(full_draft, source_path=str(full_source_dir))

    final_dir = Path(output_dir).expanduser().resolve()
    if final_dir.exists():
        raise VideoPackagingBundleError(f"输出包装包已存在，为避免覆盖已停止：{final_dir}")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = final_dir.parent / f".{final_dir.name}.{uuid.uuid4().hex}.tmp"
    try:
        temp_dir.mkdir(parents=False)
        category_manifests: list[dict[str, Any]] = []
        category_validations: list[dict[str, Any]] = []
        total_excluded_content = 0
        collected_components = {key: 0 for key in _COLLECTED_COMPONENT_KEYS}
        for category in required_categories:
            if category not in sources:
                continue
            source_dir = Path(sources[category]).expanduser().resolve()
            package = extract_category_template(category, source_dir)
            category_validation = _validate_category_template(package)
            category_validations.append({"category": category, **category_validation})
            total_excluded_content += int(category_validation.get("content_excluded_count") or 0)
            category_components = _dict(category_validation.get("collected_components"))
            for key in _COLLECTED_COMPONENT_KEYS:
                collected_components[key] += _int(category_components.get(key))
            category_manifests.append(_write_category_package(package, source_dir, temp_dir / "categories" / category))
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "package_type": "video_packaging_bundle",
            "bundle_name": _safe_bundle_name(bundle_name),
            "version": _text(version) or "1.0",
            "category_schema_version": CATEGORY_SCHEMA_VERSION,
            "timing_schema_version": TIMING_SCHEMA_VERSION,
            "timeline_unit": TIMELINE_UNIT,
            "required_categories": required_categories,
            "categories": category_manifests,
            "full_draft_style": full_draft_style,
            "merge_policy": "source_draft_preserved_additive_template",
            "validation": {
                "status": "validated",
                "category_count": len(category_manifests),
                "missing_categories": [item for item in required_categories if item not in sources],
                "source_drafts_modified": False,
                "raw_snapshots_preserved": True,
                "snapshot_policy": "sanitized_template_no_source_media",
                "content_variables_excluded": True,
                "content_excluded_count": total_excluded_content,
                "content_leak_count": 0,
                "collected_components": collected_components,
                "category_validations": category_validations,
                "relative_timing_ready": True,
                "full_draft_style_enabled": full_draft_style is not None,
                "full_draft_style_schema_version": FULL_DRAFT_STYLE_SCHEMA_VERSION if full_draft_style else "",
            },
        }
        if video_style_package is not None:
            manifest = bind_packaging_manifest(manifest, video_style_package)
        _write_json(temp_dir / "bundle_manifest.json", manifest)
        _write_json(temp_dir / "validation_report.json", manifest["validation"])
        temp_dir.replace(final_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return {**manifest, "output_dir": str(final_dir)}


def update_bundle_full_draft_style(
    bundle_dir: str | Path,
    full_draft_source: str | Path,
) -> dict[str, Any]:
    """把完整视频的全片级风格识别结果增量写入已有包装包。"""

    bundle_path = Path(bundle_dir).expanduser().resolve()
    manifest = load_bundle_manifest(bundle_path)
    source_path = Path(full_draft_source).expanduser().resolve()
    if not source_path.is_dir():
        raise VideoPackagingBundleError(f"完整视频草稿不存在或不是文件夹：{source_path}")
    style = analyze_full_draft_style(
        load_draft_content(source_path),
        source_path=str(source_path),
    )
    updated = copy.deepcopy(manifest)
    updated["full_draft_style"] = style
    validation = dict(updated.get("validation") or {})
    validation.update(
        {
            "full_draft_style_enabled": True,
            "full_draft_style_schema_version": FULL_DRAFT_STYLE_SCHEMA_VERSION,
        }
    )
    updated["validation"] = validation
    _write_json(bundle_path / "bundle_manifest.json", updated)
    _write_json(bundle_path / "validation_report.json", validation)
    return {**updated, "output_dir": str(bundle_path)}


def load_bundle_manifest(bundle_dir: str | Path) -> dict[str, Any]:
    path = Path(bundle_dir).expanduser().resolve() / "bundle_manifest.json"
    if not path.is_file():
        raise VideoPackagingBundleError(f"包装包缺少 bundle_manifest.json：{path.parent}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VideoPackagingBundleError(f"无法读取包装包清单：{path}") from exc
    if not isinstance(value, dict) or value.get("package_type") != "video_packaging_bundle":
        raise VideoPackagingBundleError("包装包清单类型不正确")
    return value


def build_bundle_application_preview(
    bundle_dir: str | Path,
    shot_categories: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """根据脚本分类生成只读应用计划，不修改目标草稿。"""

    bundle_path = Path(bundle_dir).expanduser().resolve()
    manifest = load_bundle_manifest(bundle_path)
    category_map = {
        str(item.get("category")): item
        for item in _list(manifest.get("categories"))
        if isinstance(item, Mapping)
    }
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, raw_shot in enumerate(shot_categories):
        shot = _dict(raw_shot)
        shot_id = _text(shot.get("shot_id")) or f"shot_{index + 1:03d}"
        category = _text(shot.get("category") or shot.get("shot_class"))
        if category not in category_map:
            errors.append({"shot_id": shot_id, "category": category, "reason": "category_template_missing"})
            continue
        start_us = max(0, _int(shot.get("start_us")))
        end_us = max(start_us, _int(shot.get("end_us")))
        duration_us = max(0, _int(shot.get("duration_us")) or end_us - start_us)
        actions.append(
            {
                "shot_id": shot_id,
                "category": category,
                "template_path": str(bundle_path / "categories" / category),
                "target_start_us": start_us,
                "target_duration_us": duration_us,
                "timing_policy": "relative_to_target_shot",
                "write_policy": "additive_only",
                "status": "ready" if duration_us > 0 else "blocked",
            }
        )
        if duration_us <= 0:
            errors.append({"shot_id": shot_id, "category": category, "reason": "target_duration_missing"})
    return {
        "schema_version": "video-packaging-application-preview-v1",
        "bundle_dir": str(bundle_path),
        "status": "ready" if not errors else "blocked",
        "action_count": len(actions),
        "actions": actions,
        "errors": errors,
        "source_draft_mutated": False,
    }


__all__ = [
    "CATEGORY_LABELS",
    "CATEGORY_SCHEMA_VERSION",
    "FULL_DRAFT_STYLE_SCHEMA_VERSION",
    "REQUIRED_CATEGORIES",
    "SCHEMA_VERSION",
    "TIMING_SCHEMA_VERSION",
    "VideoPackagingBundleError",
    "build_video_packaging_bundle",
    "build_bundle_application_preview",
    "extract_category_template",
    "analyze_full_draft_style",
    "diff_full_draft_style",
    "update_bundle_full_draft_style",
    "load_bundle_manifest",
    "materialize_relative_keyframes",
    "normalize_template_timing",
    "scale_relative_keyframe",
    "scale_relative_timerange",
    "summarize_template_components",
]
