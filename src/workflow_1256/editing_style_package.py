"""从剪映/剪映小助手草稿反向提取可复用的剪辑风格包。

这个模块只读草稿。它不修改草稿、不写回剪映，也不把素材文件路径、原始
字幕文本或随机 ID 放进风格包。推荐使用「基线草稿 + 用户修改后的草稿」
两份输入，这样 ``changes`` 才能说明哪些调整确实来自用户。

``capcut-cli`` 是只读审计/解析后端；没有安装时仍可用 raw-draft 模式做
结构提取，但该模式不能声称完成了 capcut-cli 的真实验收。
"""

from __future__ import annotations

import json
import shlex
import statistics
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


STYLE_PACKAGE_SCHEMA_VERSION = "editing-style-package-v1"
NORMALIZED_DRAFT_SCHEMA_VERSION = "normalized-editing-draft-v1"
MICROSECONDS_PER_SECOND = 1_000_000

_MATERIAL_COLLECTIONS = (
    "videos",
    "images",
    "audios",
    "texts",
    "transitions",
    "effects",
    "video_effects",
    "material_animations",
    "masks",
    "filters",
    "text_templates",
    "audio_fades",
    "audio_effects",
)

_STYLE_COLLECTIONS = (
    "transitions",
    "effects",
    "video_effects",
    "material_animations",
    "masks",
    "filters",
    "text_templates",
    "audio_fades",
    "audio_effects",
    "keyframes",
)

_DIFF_SECTIONS = (
    "canvas",
    "track_layout",
    "rhythm",
    "subtitle_style",
    "visual_style",
    "motion_style",
    "audio_style",
)


class EditingStylePackageError(ValueError):
    """草稿、capcut-cli 输出或风格包不符合提取契约。"""


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _round_number(value: Any, digits: int = 6) -> int | float:
    number = _number(value)
    rounded = round(number, digits)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def _duration(timerange: Any) -> int:
    item = _dict(timerange)
    return max(0, _int(item.get("duration")))


def _start(timerange: Any) -> int:
    return max(0, _int(_dict(timerange).get("start")))


def _percentile(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def _distribution(values: Sequence[int]) -> dict[str, Any]:
    values = [max(0, _int(value)) for value in values]
    if not values:
        return {"count": 0, "min_us": 0, "max_us": 0, "avg_us": 0, "median_us": 0, "p25_us": 0, "p75_us": 0}
    return {
        "count": len(values),
        "min_us": min(values),
        "max_us": max(values),
        "avg_us": round(statistics.mean(values)),
        "median_us": round(statistics.median(values)),
        "p25_us": _percentile(values, 0.25),
        "p75_us": _percentile(values, 0.75),
    }


def _safe_source_value(value: Any, *, allow_name: bool = False) -> Any:
    """递归保留风格枚举，排除路径、URL、文本、随机 ID 和凭据。"""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key)
            lowered = key.lower()
            if any(token in lowered for token in ("path", "url", "uri", "token", "secret", "password", "content")):
                continue
            if lowered in {"id", "material_id", "segment_id", "draft_id"}:
                continue
            if lowered in {"text", "transcript", "caption", "label"}:
                continue
            if lowered == "name" and not allow_name:
                continue
            normalized = _safe_source_value(raw_value, allow_name=allow_name)
            if normalized not in (None, "", [], {}):
                result[key] = normalized
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_source_value(item, allow_name=allow_name) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return _text(value)


def _descriptor(item: Mapping[str, Any]) -> dict[str, Any]:
    """把效果/转场/动画条目压成稳定的风格描述。"""
    result: dict[str, Any] = {}
    for key, value in item.items():
        lowered = _text(key).lower()
        if lowered in {"id", "material_id", "segment_id", "draft_id"}:
            continue
        if any(token in lowered for token in ("path", "url", "uri", "token", "secret", "password", "content")):
            continue
        if lowered in {"text", "label"}:
            continue
        if lowered == "name":
            result[key] = _text(value)
            continue
        if lowered.endswith("_id") and lowered not in {"resource_id", "effect_id", "transition_id"}:
            continue
        result[key] = _safe_source_value(value, allow_name=True)
    return {key: value for key, value in sorted(result.items()) if value not in (None, "", [], {})}


def _parse_text_content(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raw = _text(value)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return _dict(parsed)


def _text_style_profile(material: Mapping[str, Any]) -> dict[str, Any]:
    content = _parse_text_content(material.get("content"))
    style_variants: list[dict[str, Any]] = []
    for style in _list(content.get("styles")):
        style = _dict(style)
        fill = _dict(style.get("fill"))
        fill_content = _dict(fill.get("content"))
        solid = _dict(fill_content.get("solid"))
        strokes = _list(style.get("strokes"))
        variant = {
            "size": _round_number(style.get("size")),
            "bold": bool(style.get("bold", False)),
            "italic": bool(style.get("italic", False)),
            "underline": bool(style.get("underline", False)),
            "fill_render_type": _text(fill_content.get("render_type")),
            "fill_color": [_round_number(item) for item in _list(solid.get("color"))],
            "stroke_count": len(strokes),
        }
        style_variants.append({key: value for key, value in variant.items() if value not in ("", [], None)})
    return {
        "type": _text(material.get("type")),
        "alignment": _int(material.get("alignment")),
        "typesetting": _int(material.get("typesetting")),
        "line_spacing": _round_number(material.get("line_spacing")),
        "line_max_width": _round_number(material.get("line_max_width")),
        "force_apply_line_max_width": bool(material.get("force_apply_line_max_width", False)),
        "line_feed": _int(material.get("line_feed")),
        "global_alpha": _round_number(material.get("global_alpha"), 4),
        "style_variants": style_variants,
    }


def _style_profile_counter(profiles: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    parsed: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        normalized = _safe_source_value(profile, allow_name=True)
        key = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        counter[key] += 1
        parsed[key] = normalized
    return [
        {"count": count, "profile": parsed[key]}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:20]
    ]


def _material_index(materials: Mapping[str, Any]) -> tuple[dict[str, tuple[str, dict[str, Any]]], dict[str, int]]:
    index: dict[str, tuple[str, dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for kind in _MATERIAL_COLLECTIONS:
        entries = [_dict(item) for item in _list(materials.get(kind)) if isinstance(item, Mapping)]
        counts[kind] = len(entries)
        for item in entries:
            for candidate in (item.get("id"), item.get("material_id"), item.get("local_material_id")):
                candidate_text = _text(candidate)
                if candidate_text:
                    index[candidate_text] = (kind, item)
    return index, counts


def _clip_style(segment: Mapping[str, Any]) -> dict[str, Any]:
    clip = _dict(segment.get("clip"))
    scale = _dict(clip.get("scale"))
    transform = _dict(clip.get("transform"))
    flip = _dict(clip.get("flip"))
    return {
        "alpha": _round_number(clip.get("alpha", 1.0)),
        "rotation": _round_number(clip.get("rotation", 0.0)),
        "scale": {"x": _round_number(scale.get("x", 1.0)), "y": _round_number(scale.get("y", 1.0))},
        "transform": {"x": _round_number(transform.get("x", 0.0)), "y": _round_number(transform.get("y", 0.0))},
        "flip": {"horizontal": bool(flip.get("horizontal", False)), "vertical": bool(flip.get("vertical", False))},
    }


def _segment_summary(segment: Mapping[str, Any], material_index: Mapping[str, tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    material_kind = "unknown"
    material: dict[str, Any] = {}
    material_id = _text(segment.get("material_id"))
    if material_id in material_index:
        material_kind, material = material_index[material_id]
    return {
        "start_us": _start(segment.get("target_timerange")),
        "duration_us": _duration(segment.get("target_timerange")),
        "source_duration_us": _duration(segment.get("source_timerange")),
        "speed": _round_number(segment.get("speed", 1.0)),
        "volume": _round_number(segment.get("volume", 1.0)),
        "material_kind": material_kind,
        "has_keyframes": bool(_list(segment.get("common_keyframes")) or _list(segment.get("keyframe_refs"))),
        "clip": _clip_style(segment),
        "render_index": _int(segment.get("render_index")),
        "material_type": _text(material.get("type")),
    }


def _track_type(track: Mapping[str, Any]) -> str:
    return _text(track.get("type") or track.get("track_type") or track.get("attribute")) or "unknown"


def _track_segment_summaries(track: Mapping[str, Any], material_index: Mapping[str, tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [_segment_summary(_dict(item), material_index) for item in _list(track.get("segments")) if isinstance(item, Mapping)]


def _track_layout(tracks: Sequence[Mapping[str, Any]], material_index: Mapping[str, tuple[str, dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    layout: list[dict[str, Any]] = []
    segment_groups: list[dict[str, Any]] = []
    for index, raw_track in enumerate(tracks):
        track = _dict(raw_track)
        segments = _track_segment_summaries(track, material_index)
        durations = [item["duration_us"] for item in segments]
        layout.append(
            {
                "index": index,
                "name": _text(track.get("name")) or f"track_{index + 1}",
                "type": _track_type(track),
                "segment_count": len(segments),
                "duration_us": sum(durations),
                "muted": bool(track.get("muted", False)),
                "hidden": bool(track.get("hidden", False)),
                "locked": bool(track.get("locked", False)),
            }
        )
        segment_groups.append(
            {
                "track_index": index,
                "track_name": _text(track.get("name")) or f"track_{index + 1}",
                "type": _track_type(track),
                "segments": segments,
            }
        )
    return layout, segment_groups


def _caption_lengths(tracks: Sequence[Mapping[str, Any]], materials: Mapping[str, Any]) -> dict[str, Any]:
    text_by_id: dict[str, str] = {}
    for material in _list(materials.get("texts")):
        item = _dict(material)
        content = _parse_text_content(item.get("content"))
        material_id = _text(item.get("id") or item.get("material_id"))
        if material_id:
            text_by_id[material_id] = _text(content.get("text"))
    lengths: list[int] = []
    line_counts: list[int] = []
    for track in tracks:
        if _track_type(_dict(track)) != "text" and "字幕" not in _text(_dict(track).get("name")):
            continue
        for segment in _list(_dict(track).get("segments")):
            item = _dict(segment)
            text = text_by_id.get(_text(item.get("material_id")), "")
            if text:
                lengths.append(len(text.replace("\n", "")))
                line_counts.append(max(1, text.count("\n") + 1))
    return {"count": len(lengths), "lengths": _distribution(lengths), "line_counts": _distribution(line_counts)}


def _rhythm(track_layout: Sequence[Mapping[str, Any]], segment_groups: Sequence[Mapping[str, Any]], caption_lengths: Mapping[str, Any], total_duration_us: int) -> dict[str, Any]:
    video_durations: list[int] = []
    caption_durations: list[int] = []
    audio_durations: list[int] = []
    video_segments = 0
    transitions = 0
    for group in segment_groups:
        segments = [_dict(item) for item in _list(group.get("segments"))]
        kind = _text(group.get("type"))
        durations = [_int(item.get("duration_us")) for item in segments]
        if kind == "video" or "首帧" in _text(group.get("track_name")) or "AIGC" in _text(group.get("track_name")):
            video_durations.extend(durations)
            video_segments += len(segments)
        elif kind == "text" or "字幕" in _text(group.get("track_name")):
            caption_durations.extend(durations)
        elif kind == "audio" or "解说" in _text(group.get("track_name")) or "BGM" in _text(group.get("track_name")):
            audio_durations.extend(durations)
        transitions += sum(1 for item in segments if item.get("transition"))
    total_seconds = max(total_duration_us / MICROSECONDS_PER_SECOND, 0.001)
    return {
        "timeline_duration_us": total_duration_us,
        "video_cut_count": video_segments,
        "video_cut_rate_per_minute": round(video_segments / total_seconds * 60, 3),
        "video_cut_durations": _distribution(video_durations),
        "caption_durations": _distribution(caption_durations),
        "caption_lengths": dict(caption_lengths),
        "audio_segment_durations": _distribution(audio_durations),
        "transition_count": transitions,
    }


def _visual_style(materials: Mapping[str, Any], counts: Mapping[str, int]) -> dict[str, Any]:
    collections: dict[str, Any] = {}
    for kind in _STYLE_COLLECTIONS:
        descriptors = [_descriptor(_dict(item)) for item in _list(materials.get(kind)) if isinstance(item, Mapping)]
        descriptors = [item for item in descriptors if item]
        unique = {json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")): item for item in descriptors}
        collections[kind] = {"count": len(descriptors), "unique": list(unique.values())[:30]}
    return {"material_counts": dict(counts), "presets": collections}


def _motion_style(segment_groups: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = []
    keyframe_segments = 0
    for group in segment_groups:
        if _text(group.get("type")) != "video" and "首帧" not in _text(group.get("track_name")) and "AIGC" not in _text(group.get("track_name")):
            continue
        for segment in _list(group.get("segments")):
            item = _dict(segment)
            profile = {"clip": item.get("clip", {}), "speed": item.get("speed", 1), "has_keyframes": bool(item.get("has_keyframes"))}
            profiles.append(profile)
            keyframe_segments += int(bool(item.get("has_keyframes")))
    return {
        "keyframed_segment_count": keyframe_segments,
        "clip_profiles": _style_profile_counter(profiles),
    }


def _audio_style(tracks: Sequence[Mapping[str, Any]], materials: Mapping[str, Any]) -> dict[str, Any]:
    roles: list[dict[str, Any]] = []
    for raw_track in tracks:
        track = _dict(raw_track)
        if _track_type(track) != "audio" and not any(name in _text(track.get("name")) for name in ("解说", "BGM", "音频")):
            continue
        segments = [_dict(item) for item in _list(track.get("segments"))]
        volumes = [_number(item.get("volume", 1.0), 1.0) for item in segments]
        roles.append(
            {
                "name": _text(track.get("name")),
                "segment_count": len(segments),
                "volume": {"min": _round_number(min(volumes), 4) if volumes else 0, "max": _round_number(max(volumes), 4) if volumes else 0, "avg": _round_number(statistics.mean(volumes), 4) if volumes else 0},
                "speed": [_round_number(item.get("speed", 1.0)) for item in segments],
            }
        )
    fades = [_descriptor(_dict(item)) for item in _list(materials.get("audio_fades")) if isinstance(item, Mapping)]
    effects = [_descriptor(_dict(item)) for item in _list(materials.get("audio_effects")) if isinstance(item, Mapping)]
    return {"tracks": roles, "fade_presets": [item for item in fades if item], "effect_presets": [item for item in effects if item]}


def normalize_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    """把原始草稿投影为不含媒体路径/原文/随机 ID 的可比较快照。"""
    data = _dict(draft)
    materials = _dict(data.get("materials"))
    material_index, material_counts = _material_index(materials)
    tracks = [_dict(item) for item in _list(data.get("tracks")) if isinstance(item, Mapping)]
    track_layout, segment_groups = _track_layout(tracks, material_index)
    canvas = _dict(data.get("canvas_config"))
    width = _int(canvas.get("width") or data.get("width"))
    height = _int(canvas.get("height") or data.get("height"))
    fps = _number(data.get("fps") or canvas.get("fps"), 0)
    duration_us = _int(data.get("duration"))
    if not duration_us:
        duration_us = max((item["duration_us"] for item in track_layout), default=0)
    text_lengths = _caption_lengths(tracks, materials)
    return {
        "schema_version": NORMALIZED_DRAFT_SCHEMA_VERSION,
        "source": {
            "platform": _text(_dict(data.get("platform")).get("os")),
            "app_version": _text(_dict(data.get("platform")).get("app_version")),
            "last_modified_platform": _text(_dict(data.get("last_modified_platform")).get("os")),
            "app_source": _text(_dict(data.get("platform")).get("app_source")),
        },
        "canvas": {"width": width, "height": height, "fps": _round_number(fps), "ratio": _text(canvas.get("ratio"))},
        "track_layout": track_layout,
        "rhythm": _rhythm(track_layout, segment_groups, text_lengths, duration_us),
        "subtitle_style": {"profiles": _style_profile_counter([_text_style_profile(_dict(item)) for item in _list(materials.get("texts")) if isinstance(item, Mapping)]), "caption_material_count": material_counts.get("texts", 0)},
        "visual_style": _visual_style(materials, material_counts),
        "motion_style": _motion_style(segment_groups),
        "audio_style": _audio_style(tracks, materials),
    }


def load_draft_content(project_or_file: str | Path) -> dict[str, Any]:
    """读取本地草稿目录或 draft_content.json。"""
    path = Path(project_or_file)
    candidates = [path]
    if path.is_dir():
        candidates = [path / name for name in ("draft_content.json", "draft_info.json", "draft_meta_info.json", "template-2.tmp")]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EditingStylePackageError(f"无法读取草稿 JSON：{candidate}") from exc
        if not isinstance(parsed, dict):
            raise EditingStylePackageError(f"草稿 JSON 顶层必须是对象：{candidate}")
        if "tracks" in parsed or "materials" in parsed:
            return parsed
    raise EditingStylePackageError(f"没有找到可识别的剪映草稿 JSON：{path}")


def _source_summary(project_or_file: str | Path, draft: Mapping[str, Any]) -> dict[str, Any]:
    platform = _dict(draft.get("platform"))
    return {
        "draft_name": Path(project_or_file).name,
        "platform": _text(platform.get("os")),
        "app_version": _text(platform.get("app_version")),
        "app_source": _text(platform.get("app_source")),
        "last_modified_platform": _text(_dict(draft.get("last_modified_platform")).get("os")),
    }


def _diff_paths(before: Any, after: Any, prefix: str = "", limit: int = 20) -> list[str]:
    if len(_diff_paths.seen) >= limit:
        return []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        paths: list[str] = []
        for key in sorted(set(before) | set(after)):
            child = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(_diff_paths(before.get(key), after.get(key), child, limit))
            if len(_diff_paths.seen) >= limit:
                break
        return paths
    if isinstance(before, list) and isinstance(after, list):
        paths: list[str] = []
        for index in range(max(len(before), len(after))):
            child = f"{prefix}[{index}]"
            paths.extend(_diff_paths(before[index] if index < len(before) else None, after[index] if index < len(after) else None, child, limit))
            if len(_diff_paths.seen) >= limit:
                break
        return paths
    if before != after:
        _diff_paths.seen.append(prefix)
        return [prefix]
    return []


_diff_paths.seen: list[str] = []


def _changed_paths(before: Any, after: Any, limit: int = 20) -> list[str]:
    _diff_paths.seen = []
    paths = _diff_paths(before, after, limit=limit)
    _diff_paths.seen = []
    return paths[:limit]


def _diff_count(before: Any, after: Any) -> int:
    return len(_changed_paths(before, after, limit=1000))


def _cli_info(raw: Any) -> dict[str, Any]:
    info = _dict(raw)
    keys = ("name", "duration_us", "fps", "width", "height", "ratio", "tracks", "segments", "platform", "material_types", "materials_with_items", "material_summary")
    return {key: info[key] for key in keys if key in info}


def _cli_tracks(raw: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _list(raw):
        item = _dict(item)
        result.append({key: item[key] for key in ("index", "type", "name", "segments", "duration_us", "muted", "hidden", "locked") if key in item})
    return result


def _cli_materials(raw: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _list(raw):
        item = _dict(item)
        result.append({key: item[key] for key in ("type", "count") if key in item})
    return result


def _cli_lint(raw: Any) -> dict[str, Any]:
    lint = _dict(raw)
    return {key: lint[key] for key in ("ok", "summary") if key in lint}


def sanitize_cli_evidence(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    operations = _dict(snapshot.get("operations"))
    evidence: dict[str, Any] = {"backend": "capcut-cli"}
    version = operations.get("version")
    if isinstance(version, Mapping):
        evidence["version"] = _text(_dict(version).get("version"))
    elif version not in (None, ""):
        evidence["version"] = _text(version)
    if operations.get("info") is not None:
        evidence["info"] = _cli_info(operations.get("info"))
    if operations.get("tracks") is not None:
        evidence["tracks"] = _cli_tracks(operations.get("tracks"))
    if operations.get("materials") is not None:
        evidence["materials"] = _cli_materials(operations.get("materials"))
    if operations.get("lint") is not None:
        evidence["lint"] = _cli_lint(operations.get("lint"))
    if operations.get("texts") is not None:
        texts = _list(operations.get("texts"))
        evidence["texts"] = {"count": len(texts), "has_text_output": bool(texts)}
    return evidence


def build_editing_style_package(
    baseline_draft: Mapping[str, Any] | None,
    edited_draft: Mapping[str, Any],
    *,
    baseline_source: str | Path = "",
    edited_source: str | Path = "",
    name: str = "",
    backend: str = "raw-draft",
    cli_evidence: Mapping[str, Any] | None = None,
    cli_diff: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """根据一份或两份草稿生成编辑风格包。"""
    edited_normalized = normalize_draft(edited_draft)
    baseline_normalized = normalize_draft(baseline_draft) if baseline_draft is not None else None
    changed_sections: list[dict[str, Any]] = []
    if baseline_normalized is not None:
        for section in _DIFF_SECTIONS:
            before = baseline_normalized.get(section)
            after = edited_normalized.get(section)
            if before == after:
                continue
            changed_sections.append(
                {
                    "section": section,
                    "change_count": _diff_count(before, after),
                    "example_paths": _changed_paths(before, after),
                    "before": before,
                    "after": after,
                }
            )
    source = {
        "backend": backend,
        "comparison": "baseline_and_edited" if baseline_normalized is not None else "edited_only",
        "baseline": _source_summary(baseline_source, baseline_draft) if baseline_draft is not None else None,
        "edited": _source_summary(edited_source, edited_draft),
    }
    warnings: list[str] = []
    if baseline_normalized is None:
        warnings.append("未提供基线草稿，无法把改动归因到用户编辑；当前包只能作为已编辑草稿的风格快照。")
    if edited_normalized["source"].get("platform") not in ("", "windows"):
        warnings.append("编辑草稿的平台标记不是 Windows；不能据此验收 Windows 剪映交付。")
    package = {
        "schema_version": STYLE_PACKAGE_SCHEMA_VERSION,
        "package_type": "editing_style_package",
        "name": name.strip() or f"{_source_summary(edited_source, edited_draft)['draft_name'] or 'edited-draft'}-editing-style",
        "source": source,
        "canvas": edited_normalized["canvas"],
        "track_layout": edited_normalized["track_layout"],
        "rhythm": edited_normalized["rhythm"],
        "subtitle_style": edited_normalized["subtitle_style"],
        "visual_style": edited_normalized["visual_style"],
        "motion_style": edited_normalized["motion_style"],
        "audio_style": edited_normalized["audio_style"],
        "changes": {
            "status": "attributed" if baseline_normalized is not None else "snapshot_only",
            "changed_section_count": len(changed_sections),
            "changed_sections": changed_sections,
            "capcut_cli_diff": dict(cli_diff or {}),
        },
        "guardrails": {
            "reuse_media_paths": False,
            "reuse_original_text": False,
            "reuse_random_ids": False,
            "timeline_unit": "microseconds",
            "source_of_truth": "edited_draft_style",
        },
        "warnings": warnings,
        "evidence": {key: value for key, value in (cli_evidence or {}).items()},
    }
    return package


def _command_tokens(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        tokens = shlex.split(command, posix=False)
        return [token.strip('"') for token in tokens]
    return [str(item) for item in command]


def _parse_json_output(stdout: str) -> Any:
    candidate = stdout.strip()
    if not candidate:
        raise EditingStylePackageError("capcut-cli 没有返回 JSON")
    decoder = json.JSONDecoder()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    for index, char in enumerate(candidate):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        return value
    raise EditingStylePackageError("capcut-cli 输出不是合法 JSON")


def _run_cli(
    command: str | Sequence[str],
    args: Sequence[str],
    *,
    timeout: float,
    allow_text: bool = False,
    allow_nonzero_json: bool = False,
) -> Any:
    tokens = _command_tokens(command) + [str(item) for item in args]
    try:
        completed = subprocess.run(
            tokens,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EditingStylePackageError(f"capcut-cli 命令超时：{' '.join(tokens)}") from exc
    except OSError as exc:
        raise EditingStylePackageError(f"无法启动 capcut-cli：{tokens[0] if tokens else command}") from exc
    if completed.returncode != 0 and allow_nonzero_json:
        try:
            parsed = _parse_json_output(completed.stdout or "")
        except EditingStylePackageError:
            parsed = None
        if isinstance(parsed, Mapping) and not parsed.get("error"):
            return parsed
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        message = detail[-1][:2000] if detail else f"退出码 {completed.returncode}"
        raise EditingStylePackageError(f"capcut-cli 执行失败（{' '.join(tokens)}）：{message}")
    if allow_text:
        try:
            return _parse_json_output(completed.stdout)
        except EditingStylePackageError:
            return completed.stdout.strip()
    return _parse_json_output(completed.stdout)


def run_capcut_cli_snapshot(
    project_or_file: str | Path,
    *,
    command: str | Sequence[str] = "capcut",
    timeout: float = 60.0,
    include_segments: bool = False,
    include_texts: bool = False,
) -> dict[str, Any]:
    """执行 capcut-cli 只读命令并返回原始快照，供后续脱敏。"""
    project = str(project_or_file)
    operations: dict[str, Any] = {}
    operations["version"] = _run_cli(command, ("--version",), timeout=timeout, allow_text=True)
    for operation in ("info", "tracks", "materials", "lint"):
        try:
            operations[operation] = _run_cli(
                command,
                (operation, project, "--jianying"),
                timeout=timeout,
                allow_nonzero_json=operation == "lint",
            )
        except EditingStylePackageError:
            operations[operation] = _run_cli(
                command,
                (operation, project),
                timeout=timeout,
                allow_nonzero_json=operation == "lint",
            )
    if include_segments:
        try:
            operations["segments"] = _run_cli(command, ("segments", project, "--jianying"), timeout=timeout)
        except EditingStylePackageError:
            operations["segments"] = _run_cli(command, ("segments", project), timeout=timeout)
    if include_texts:
        try:
            operations["texts"] = _run_cli(command, ("texts", project, "--jianying"), timeout=timeout)
        except EditingStylePackageError:
            operations["texts"] = _run_cli(command, ("texts", project), timeout=timeout)
    return {"project_name": Path(project).name, "operations": operations}


def run_capcut_cli_diff(project_a: str | Path, project_b: str | Path, *, command: str | Sequence[str] = "capcut", timeout: float = 60.0) -> dict[str, Any]:
    """运行 capcut-cli diff，并只返回不含媒体/随机 ID 的差异摘要。"""
    raw = _dict(_run_cli(command, ("diff", str(project_a), str(project_b), "--jianying"), timeout=timeout))
    if not raw:
        raw = _dict(_run_cli(command, ("diff", str(project_a), str(project_b)), timeout=timeout))
    summary: dict[str, Any] = {"ok": bool(raw.get("ok", True)), "changed": bool(raw.get("changed", False))}
    for section in ("tracks", "segments", "materials"):
        item = _dict(raw.get(section))
        summary[section] = {
            "added_count": len(_list(item.get("added"))),
            "removed_count": len(_list(item.get("removed"))),
            "changed_count": len(_list(item.get("changed"))),
        }
    return summary


def extract_editing_style_package(
    baseline_project: str | Path | None,
    edited_project: str | Path,
    *,
    name: str = "",
    capcut_command: str | Sequence[str] | None = None,
    timeout: float = 60.0,
    raw_draft_only: bool = False,
) -> dict[str, Any]:
    """读取草稿、可选调用 capcut-cli，并构建风格包。"""
    baseline = load_draft_content(baseline_project) if baseline_project else None
    edited = load_draft_content(edited_project)
    cli_evidence: dict[str, Any] = {}
    cli_diff: dict[str, Any] = {}
    backend = "raw-draft"
    if capcut_command is not None and not raw_draft_only:
        baseline_snapshot = run_capcut_cli_snapshot(baseline_project, command=capcut_command, timeout=timeout) if baseline_project else None
        edited_snapshot = run_capcut_cli_snapshot(edited_project, command=capcut_command, timeout=timeout)
        cli_evidence["baseline"] = sanitize_cli_evidence(baseline_snapshot) if baseline_snapshot else None
        cli_evidence["edited"] = sanitize_cli_evidence(edited_snapshot)
        if baseline_project:
            cli_diff = run_capcut_cli_diff(baseline_project, edited_project, command=capcut_command, timeout=timeout)
        backend = "capcut-cli"
    return build_editing_style_package(
        baseline,
        edited,
        baseline_source=baseline_project or "",
        edited_source=edited_project,
        name=name,
        backend=backend,
        cli_evidence=cli_evidence,
        cli_diff=cli_diff,
    )


def write_style_package(package: Mapping[str, Any], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(package), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
