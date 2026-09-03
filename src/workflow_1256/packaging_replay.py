"""把 A/B 抓取结果在隔离区回放为草稿 C。

回放的目标不是把 B 草稿直接复制一份，而是：

* 以 A 的完整草稿和素材目录为基础；
* 只把 B 中相对于 A 新增或修改的包装轨道、包装素材、样式和关键帧合并；
* 对 A/B 复制草稿重新生成的运行时 ID 做引用映射；
* 用 C 与 B 的规范化结构差异做复核。

该模块只允许写入调用方指定的隔离输出目录，不写正式包装包，也不修改 A/B。
时间单位统一为微秒。
"""

from __future__ import annotations

import copy
import json
import shutil
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .editing_style_package import load_draft_content
from .packaging_capture import (
    DEFAULT_RAW_DIFF_LIMIT,
    PackagingCaptureError,
    _draft_content_path,
    _raw_diff,
    _raw_projection,
    _sha256_file,
)
from .video_packaging_bundle import (
    VideoPackagingBundleError,
    analyze_full_draft_style,
    diff_full_draft_style,
    normalize_template_timing,
)


PACKAGING_REPLAY_SCHEMA_VERSION = "packaging-replay-v1"
_ID_ALIAS_KEYS = (
    "id",
    "material_id",
    "local_material_id",
    "local_id",
    "music_id",
    "audio_id",
    "video_id",
    "image_id",
    "canvas_id",
    "speed_id",
    "vocal_separation_id",
    "sound_channel_mapping_id",
    "text_id",
)
_PATH_KEYS = {"path", "media_path", "material_url", "url", "uri"}
_VOLATILE_TOP_LEVEL_KEYS = {"id", "name", "create_time", "update_time", "source"}
_POSITIONAL_COLLECTIONS = {
    "videos",
    "audios",
    "canvases",
    "speeds",
    "sound_channel_mappings",
    "vocal_separations",
    "texts",
}
_CONTENT_COLLECTIONS = {"videos", "images", "audios", "digital_humans", "drafts", "texts"}


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _text(value: object) -> str:
    return str(value or "").strip()


def _id_aliases(value: Mapping[str, Any]) -> list[str]:
    return [
        _text(value.get(key))
        for key in _ID_ALIAS_KEYS
        if _text(value.get(key))
    ]


def _text_content(value: Mapping[str, Any]) -> str:
    raw = value.get("content")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, Mapping):
            return _text(parsed.get("text"))
    return _text(value.get("base_content") or value.get("text"))


def _material_signature(collection: str, value: Mapping[str, Any]) -> tuple[Any, ...]:
    """返回用于判断 A/B 是否同一内容素材的稳定签名。"""

    collection = _text(collection)
    if collection == "texts":
        return (collection, _text(value.get("type")), _text_content(value))
    if collection in {"videos", "images"}:
        return (
            collection,
            _text(value.get("material_name") or value.get("name")),
            _text(value.get("type")),
            int(value.get("duration") or 0),
            int(value.get("width") or 0),
            int(value.get("height") or 0),
            bool(value.get("has_audio")),
        )
    if collection == "audios":
        return (
            collection,
            _text(value.get("material_name") or value.get("name")),
            _text(value.get("type") or value.get("category")),
            int(value.get("duration") or 0),
        )
    if collection in _POSITIONAL_COLLECTIONS:
        # 这些剪映辅助素材在复制草稿时只变更 ID；位置是同源草稿的稳定绑定。
        return (collection, "positional")
    projected = _raw_projection(value)
    return (collection, json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _content_signature(draft: Mapping[str, Any]) -> dict[str, Any]:
    materials = _dict(draft.get("materials"))
    tracks: list[tuple[str, str, int]] = []
    for raw_track in _list(draft.get("tracks")):
        track = _dict(raw_track)
        track_type = _text(track.get("type") or track.get("track_type"))
        name = _text(track.get("name"))
        if track_type in {"video", "audio", "text"}:
            # B 允许新增包装文字轨；它不是脚本内容轨，不能阻断 A/B 回放。
            if name.startswith("包装-") or name.lower().startswith("packaging-"):
                continue
            tracks.append((track_type, name, len(_list(track.get("segments")))))
    material_signatures: dict[str, list[tuple[Any, ...]]] = {}
    for collection in sorted(_CONTENT_COLLECTIONS):
        material_signatures[collection] = [
            _material_signature(collection, _dict(item))
            for item in _list(materials.get(collection))
        ]
    return {"tracks": tracks, "materials": material_signatures}


def _same_script_warnings(baseline: Mapping[str, Any], packaged: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    if int(baseline.get("duration") or 0) != int(packaged.get("duration") or 0):
        warnings.append("A/B 时长不同")
    if _dict(baseline.get("canvas_config")) != _dict(packaged.get("canvas_config")):
        warnings.append("A/B 画布不同")
    before = _content_signature(baseline)
    after = _content_signature(packaged)
    if before["tracks"] != after["tracks"]:
        warnings.append("A/B 内容轨道的类型、名称或片段数量不同")
    for collection in sorted(_CONTENT_COLLECTIONS):
        before_items = before["materials"].get(collection, [])
        after_items = after["materials"].get(collection, [])
        if collection == "texts":
            # B 可以新增片头文字模板文本；A 的原字幕必须保持一一对应。
            if before_items != after_items[: len(before_items)]:
                warnings.append("A/B 原字幕内容或顺序不同")
        elif before_items != after_items:
            warnings.append(f"A/B {collection} 内容素材不同")
    return warnings


def _find_material_matches(
    collection: str,
    baseline_items: Sequence[Any],
    packaged_items: Sequence[Any],
) -> tuple[list[tuple[int, int]], list[int]]:
    """匹配同源素材；返回 (A 索引, B 索引) 和 B 中新增索引。"""

    baseline = [_dict(item) for item in baseline_items]
    packaged = [_dict(item) for item in packaged_items]
    used: set[int] = set()
    matches: list[tuple[int, int]] = []
    added: list[int] = []
    for b_index, b_item in enumerate(packaged):
        candidate: int | None = None
        if collection in _POSITIONAL_COLLECTIONS and b_index < len(baseline):
            if collection == "texts":
                if _material_signature(collection, baseline[b_index]) == _material_signature(collection, b_item):
                    candidate = b_index
            else:
                candidate = b_index
        if candidate is None:
            signature = _material_signature(collection, b_item)
            candidate = next(
                (
                    index
                    for index, a_item in enumerate(baseline)
                    if index not in used and _material_signature(collection, a_item) == signature
                ),
                None,
            )
        if candidate is None or candidate in used:
            added.append(b_index)
            continue
        used.add(candidate)
        matches.append((candidate, b_index))
    return matches, added


def _rewrite_refs(value: Any, id_map: Mapping[str, str]) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _rewrite_refs(child, id_map) for key, child in value.items()}
    if isinstance(value, list):
        return [_rewrite_refs(child, id_map) for child in value]
    if isinstance(value, tuple):
        return [_rewrite_refs(child, id_map) for child in value]
    if isinstance(value, str):
        return id_map.get(value, value)
    return copy.deepcopy(value)


def _merge_shared_material(
    baseline_item: Mapping[str, Any],
    packaged_item: Mapping[str, Any],
    id_map: Mapping[str, str],
) -> dict[str, Any]:
    """保留 A 的本地素材路径/身份，吸收 B 的样式字段。"""

    result = copy.deepcopy(dict(baseline_item))
    for key, value in packaged_item.items():
        lowered = str(key).lower()
        if lowered in _ID_ALIAS_KEYS or lowered in _PATH_KEYS:
            continue
        result[str(key)] = _rewrite_refs(value, id_map)
    return result


def _rewrite_local_paths(
    value: Any,
    *,
    baseline_root: Path,
    packaged_root: Path,
    target_root: Path,
    copied_paths: dict[str, str],
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _rewrite_local_paths(
                child,
                baseline_root=baseline_root,
                packaged_root=packaged_root,
                target_root=target_root,
                copied_paths=copied_paths,
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _rewrite_local_paths(
                child,
                baseline_root=baseline_root,
                packaged_root=packaged_root,
                target_root=target_root,
                copied_paths=copied_paths,
            )
            for child in value
        ]
    if not isinstance(value, str):
        return copy.deepcopy(value)

    raw = value.strip()
    if not raw:
        return value
    for source_root in (baseline_root, packaged_root):
        root_text = str(source_root)
        if not raw.lower().startswith(root_text.lower()):
            continue
        relative = raw[len(root_text) :].lstrip("\\/")
        source_file = source_root / Path(relative)
        target_file = target_root / Path(relative)
        cache_key = str(source_file).lower()
        if cache_key in copied_paths:
            return copied_paths[cache_key]
        if source_root == packaged_root and source_file.is_file():
            target_file.parent.mkdir(parents=True, exist_ok=True)
            if target_file.exists():
                try:
                    same = _sha256_file(target_file) == _sha256_file(source_file)
                except OSError:
                    same = False
                if not same:
                    target_file = target_root / "replay_added_assets" / f"{uuid.uuid4().hex[:8]}_{source_file.name}"
                    target_file.parent.mkdir(parents=True, exist_ok=True)
            if not target_file.exists():
                shutil.copy2(source_file, target_file)
        copied_paths[cache_key] = str(target_file)
        return str(target_file)
    return value


def _replay_projection(draft: Mapping[str, Any]) -> Any:
    runtime_ids: dict[str, str] = {}
    materials = _dict(draft.get("materials"))
    for collection, raw_items in materials.items():
        for index, raw_item in enumerate(_list(raw_items)):
            token = f"material:{collection}:{index}"
            for alias in _id_aliases(_dict(raw_item)):
                runtime_ids.setdefault(alias, token)
    for track_index, raw_track in enumerate(_list(draft.get("tracks"))):
        track = _dict(raw_track)
        track_type = _text(track.get("type") or track.get("track_type"))
        track_name = _text(track.get("name"))
        for alias in _id_aliases(track):
            runtime_ids.setdefault(alias, f"track:{track_type}:{track_name}:{track_index}")
        for segment_index, raw_segment in enumerate(_list(track.get("segments"))):
            for alias in _id_aliases(_dict(raw_segment)):
                runtime_ids.setdefault(alias, f"segment:{track_type}:{track_name}:{segment_index}")

    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): normalize(child) for key, child in value.items()}
        if isinstance(value, list):
            return [normalize(child) for child in value]
        if isinstance(value, str):
            return runtime_ids.get(value, value)
        return copy.deepcopy(value)

    projected = normalize(_raw_projection(draft))
    if isinstance(projected, dict):
        for key in _VOLATILE_TOP_LEVEL_KEYS:
            projected.pop(key, None)
    return projected


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _patch_metadata(target_dir: Path, *, draft_name: str, draft_id: str, duration_us: int) -> None:
    now = int(time.time())
    content_path = target_dir / "draft_content.json"
    content = json.loads(content_path.read_text(encoding="utf-8"))
    if isinstance(content, dict):
        content["id"] = draft_id
        content["name"] = draft_name
        content["create_time"] = now
        content["update_time"] = now
        content_path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    info_path = target_dir / "draft_info.json"
    if info_path.is_file():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            info = None
        if isinstance(info, dict):
            info.update({"id": draft_id, "create_time": now, "duration": duration_us})
            _write_json(info_path, info)

    meta_path = target_dir / "draft_meta_info.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            meta = None
        if isinstance(meta, dict):
            meta.update(
                {
                    "draft_id": draft_id,
                    "draft_name": draft_name,
                    "draft_fold_path": str(target_dir).replace("\\", "/"),
                    "draft_duration": duration_us,
                    "tm_duration": duration_us,
                    "tm_draft_create": now * 1_000_000,
                    "tm_draft_modified": now * 1_000_000,
                }
            )
            _write_json(meta_path, meta)


def _merge_drafts(
    baseline: Mapping[str, Any],
    packaged: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    warnings = _same_script_warnings(baseline, packaged)
    if warnings:
        raise PackagingCaptureError("A/B 不满足同源脚本回放条件：" + "；".join(warnings))

    base_materials = _dict(baseline.get("materials"))
    package_materials = _dict(packaged.get("materials"))
    merged_materials = copy.deepcopy(base_materials)
    id_map: dict[str, str] = {}
    added_materials: dict[str, int] = {}
    matched_materials: dict[str, int] = {}
    baseline_ids = {
        alias
        for raw_items in base_materials.values()
        for raw_item in _list(raw_items)
        for alias in _id_aliases(_dict(raw_item))
    }
    used_ids = set(baseline_ids)

    for collection in sorted(set(base_materials) | set(package_materials)):
        base_items = _list(base_materials.get(collection))
        package_items = _list(package_materials.get(collection))
        matches, added = _find_material_matches(collection, base_items, package_items)
        current = [copy.deepcopy(item) for item in base_items]
        match_by_b = {b_index: a_index for a_index, b_index in matches}
        for a_index, b_index in matches:
            a_item = _dict(base_items[a_index])
            b_item = _dict(package_items[b_index])
            a_aliases = _id_aliases(a_item)
            a_target = a_aliases[0] if a_aliases else ""
            if a_target:
                for alias in _id_aliases(b_item):
                    id_map[alias] = a_target
            current[a_index] = _merge_shared_material(a_item, b_item, id_map)
        for b_index in added:
            item = copy.deepcopy(_dict(package_items[b_index]))
            aliases = _id_aliases(item)
            replacement: dict[str, str] = {}
            for alias in aliases:
                if alias in used_ids:
                    replacement[alias] = str(uuid.uuid4()).replace("-", "")
                else:
                    replacement[alias] = alias
            if replacement:
                item = _rewrite_refs(item, replacement)
                for old, new in replacement.items():
                    id_map[old] = new
                    used_ids.add(new)
            current.append(item)
        merged_materials[collection] = current
        matched_materials[collection] = len(matches)
        added_materials[collection] = len(added)

    # 某些素材字段会引用另一个素材集合（例如音频的 music_id）。
    # 所有集合完成匹配后再做一次全量引用重写，避免受集合排序影响。
    merged_materials = _rewrite_refs(merged_materials, id_map)

    merged_tracks: list[dict[str, Any]] = []
    base_tracks = [_dict(item) for item in _list(baseline.get("tracks"))]
    package_tracks = [_dict(item) for item in _list(packaged.get("tracks"))]
    used_base_tracks: set[int] = set()
    track_counts = {"matched": 0, "added": 0}
    for package_track in package_tracks:
        track_type = _text(package_track.get("type") or package_track.get("track_type"))
        name = _text(package_track.get("name"))
        base_index = next(
            (
                index
                for index, base_track in enumerate(base_tracks)
                if index not in used_base_tracks
                and _text(base_track.get("type") or base_track.get("track_type")) == track_type
                and _text(base_track.get("name")) == name
            ),
            None,
        )
        if base_index is None:
            merged_tracks.append(_rewrite_refs(package_track, id_map))
            track_counts["added"] += 1
            continue

        base_track = base_tracks[base_index]
        used_base_tracks.add(base_index)
        if len(_list(base_track.get("segments"))) != len(_list(package_track.get("segments"))):
            raise PackagingCaptureError(f"同名内容轨道片段数量不同，无法安全回放：{name}")
        merged_track = copy.deepcopy(base_track)
        # 吸收 B 的轨道开关/属性，但保留 A 的轨道 ID。
        for key, value in package_track.items():
            if key in {"id", "segments"}:
                continue
            merged_track[key] = _rewrite_refs(value, id_map)
        merged_segments: list[Any] = []
        for base_segment, package_segment in zip(_list(base_track.get("segments")), _list(package_track.get("segments"))):
            base_value = _dict(base_segment)
            package_value = _dict(package_segment)
            merged_segment = _rewrite_refs(package_value, id_map)
            if base_value.get("id"):
                merged_segment["id"] = base_value["id"]
            merged_segments.append(merged_segment)
        merged_track["segments"] = merged_segments
        merged_tracks.append(merged_track)
        track_counts["matched"] += 1

    for index, base_track in enumerate(base_tracks):
        if index not in used_base_tracks:
            merged_tracks.append(copy.deepcopy(base_track))

    merged = copy.deepcopy(dict(baseline))
    merged["materials"] = merged_materials
    merged["tracks"] = merged_tracks
    report = {
        "id_map_count": len(id_map),
        "matched_materials": matched_materials,
        "added_materials": added_materials,
        "matched_tracks": track_counts["matched"],
        "added_tracks": track_counts["added"],
        "source_content_preserved": True,
        "write_policy": "isolated_replay_only_no_production_write",
    }
    return merged, report


def _keyframe_family(property_type: object) -> str:
    value = _text(property_type).lower()
    if "alpha" in value or "opacity" in value:
        return "alpha"
    if "scale" in value or "uniform" in value:
        return "scale"
    if "position" in value or "translate" in value:
        return "position"
    if "rotate" in value or "rotation" in value:
        return "rotation"
    if "volume" in value:
        return "volume"
    return value


def _apply_reviewed_parameters(
    draft: dict[str, Any],
    reviewed_parameters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把用户确认后的相对时间关键帧参数写回隔离 C。

    参数清单使用 ``offset_ratio``，因此不会把母版绝对微秒时间直接复制到
    其他镜头。其他尚未建立安全映射的参数保留在清单中并列入 warning。
    """

    raw_parameters = dict(reviewed_parameters or {})
    parameters_value = raw_parameters.get("parameters")
    parameters = _dict(parameters_value) if isinstance(parameters_value, Mapping) else raw_parameters
    keyframes = _list(parameters.get("keyframes"))
    applied = 0
    warnings: list[str] = []
    tracks = _list(draft.get("tracks"))

    for raw_item in keyframes:
        item = _dict(raw_item)
        try:
            track_index = int(item.get("track_index"))
            segment_index = int(item.get("segment_index"))
        except (TypeError, ValueError):
            warnings.append("存在无法解析轨道或片段索引的关键帧参数")
            continue
        if track_index < 0 or track_index >= len(tracks):
            warnings.append(f"关键帧参数轨道索引超出范围：{track_index}")
            continue
        track = _dict(tracks[track_index])
        segments = _list(track.get("segments"))
        if segment_index < 0 or segment_index >= len(segments):
            warnings.append(f"关键帧参数片段索引超出范围：轨道 {track_index} / 片段 {segment_index}")
            continue
        segment = _dict(segments[segment_index])
        raw_groups = [_dict(group) for group in _list(segment.get("common_keyframes"))]
        reviewed_groups = _list(item.get("keyframes"))
        duration_us = int(_dict(segment.get("target_timerange")).get("duration") or 0)
        for raw_reviewed_group in reviewed_groups:
            reviewed_group = _dict(raw_reviewed_group)
            family = _keyframe_family(reviewed_group.get("property_type"))
            target_group = next(
                (
                    group
                    for group in raw_groups
                    if _keyframe_family(group.get("property_type")) == family
                ),
                None,
            )
            if target_group is None:
                target_group = {
                    "id": uuid.uuid4().hex,
                    "keyframe_list": [],
                    "material_id": "",
                    "property_type": reviewed_group.get("property_type") or "KFTypeScaleX",
                }
                raw_groups.append(target_group)
            raw_points = [_dict(point) for point in _list(target_group.get("keyframe_list"))]
            reviewed_points = _list(reviewed_group.get("points"))
            for point_index, raw_reviewed_point in enumerate(reviewed_points):
                reviewed_point = _dict(raw_reviewed_point)
                point = raw_points[point_index] if point_index < len(raw_points) else {"id": uuid.uuid4().hex}
                if "curveType" in reviewed_point:
                    point["curveType"] = reviewed_point["curveType"]
                if "graphID" in reviewed_point:
                    point["graphID"] = reviewed_point["graphID"]
                if "left_control" in reviewed_point:
                    point["left_control"] = copy.deepcopy(reviewed_point["left_control"])
                if "right_control" in reviewed_point:
                    point["right_control"] = copy.deepcopy(reviewed_point["right_control"])
                if "values" in reviewed_point:
                    point["values"] = copy.deepcopy(reviewed_point["values"])
                if reviewed_point.get("offset_ratio") is not None and duration_us > 0:
                    try:
                        point["time_offset"] = round(float(reviewed_point["offset_ratio"]) * duration_us)
                    except (TypeError, ValueError):
                        warnings.append(f"关键帧 offset_ratio 无法解析：轨道 {track_index} / 片段 {segment_index}")
                elif reviewed_point.get("time_offset") is not None:
                    try:
                        point["time_offset"] = int(reviewed_point["time_offset"])
                    except (TypeError, ValueError):
                        warnings.append(f"关键帧 time_offset 无法解析：轨道 {track_index} / 片段 {segment_index}")
                if point_index >= len(raw_points):
                    raw_points.append(point)
            target_group["keyframe_list"] = raw_points
            applied += 1
        segment["common_keyframes"] = raw_groups
        segments[segment_index] = segment
        track["segments"] = segments
        tracks[track_index] = track

    draft["tracks"] = tracks
    unsupported = sorted(str(key) for key in parameters if key not in {"keyframes"})
    if unsupported:
        warnings.append("以下参数类别当前只做清单保留，尚未建立安全写回映射：" + ", ".join(unsupported))
    return {"applied_group_count": applied, "warnings": warnings}


def write_replay_artifact(
    baseline_source: str | Path,
    packaged_source: str | Path,
    capture_result: Mapping[str, Any],
    output_dir: str | Path,
    *,
    raw_diff_limit: int = DEFAULT_RAW_DIFF_LIMIT,
    reviewed_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """在 ``output_dir`` 下创建 replay_C，并返回 C/B 复核报告。"""

    baseline_path = _draft_content_path(baseline_source)
    packaged_path = _draft_content_path(packaged_source)
    baseline_dir = baseline_path.parent
    packaged_dir = packaged_path.parent
    target_parent = Path(output_dir).expanduser().resolve()
    target_dir = target_parent / "replay_C"
    if target_dir.exists():
        raise PackagingCaptureError(f"回放输出目录已存在，为避免覆盖已停止：{target_dir}")
    candidate = _dict(capture_result.get("candidate_package"))
    compatibility = _dict(candidate.get("compatibility") or capture_result.get("compatibility"))
    if _text(compatibility.get("status")) != "compatible":
        raise PackagingCaptureError("A/B 兼容性不是 compatible，已阻止自动回放")
    confirmation = _dict(
        capture_result.get("confirmation")
        or _dict(capture_result.get("parameter_catalog")).get("confirmation")
        or candidate.get("confirmation")
    )
    if bool(confirmation.get("enforced")) and not bool(confirmation.get("confirmed")):
        unresolved = _list(confirmation.get("unresolved_track_indices"))
        suffix = f"（未确认轨道：{', '.join(str(item) for item in unresolved)}）" if unresolved else ""
        raise PackagingCaptureError("仍有轨道元素或文字模板目标未确认，已阻止生成 C" + suffix)

    baseline_draft = load_draft_content(baseline_path)
    packaged_draft = load_draft_content(packaged_path)
    merged_draft, merge_report = _merge_drafts(baseline_draft, packaged_draft)
    parameter_review = _apply_reviewed_parameters(merged_draft, reviewed_parameters)
    shutil.copytree(baseline_dir, target_dir)
    copied_paths: dict[str, str] = {}
    merged_draft = _rewrite_local_paths(
        merged_draft,
        baseline_root=baseline_dir,
        packaged_root=packaged_dir,
        target_root=target_dir,
        copied_paths=copied_paths,
    )
    draft_name = f"{_text(baseline_draft.get('name')) or baseline_dir.name}_replay_C"
    draft_id = str(uuid.uuid4()).upper()
    merged_draft["id"] = draft_id
    merged_draft["name"] = draft_name
    merged_draft["create_time"] = int(time.time())
    merged_draft["update_time"] = int(time.time())
    _write_json(target_dir / "draft_content.json", merged_draft)
    _patch_metadata(
        target_dir,
        draft_name=draft_name,
        draft_id=draft_id,
        duration_us=int(merged_draft.get("duration") or 0),
    )

    before_projection = _replay_projection(merged_draft)
    after_projection = _replay_projection(packaged_draft)
    raw_changes, raw_truncated = _raw_diff(
        before_projection,
        after_projection,
        limit=max(1, int(raw_diff_limit)),
    )
    semantic_warnings: list[str] = []
    try:
        baseline_timing = normalize_template_timing(merged_draft)
        packaged_timing = normalize_template_timing(packaged_draft)
    except (OSError, ValueError, VideoPackagingBundleError) as exc:
        baseline_timing = None
        packaged_timing = None
        semantic_warnings.append(f"C/B 相对时间复核未完成：{type(exc).__name__}: {exc}")
    semantic_diff = diff_full_draft_style(
        analyze_full_draft_style(merged_draft, source_path=str(target_dir)),
        analyze_full_draft_style(packaged_draft, source_path=str(packaged_dir)),
        baseline_timing=baseline_timing,
        current_timing=packaged_timing,
    )
    if semantic_warnings:
        semantic_diff = dict(semantic_diff)
        semantic_diff["verification_warnings"] = semantic_warnings
    verified = not raw_changes and not raw_truncated and not semantic_diff.get("changed_count") and not semantic_diff.get("resource_review") and not semantic_warnings
    report = {
        "schema_version": PACKAGING_REPLAY_SCHEMA_VERSION,
        "status": "verified" if verified else "review_required",
        "source": {
            "baseline": str(baseline_dir),
            "packaged": str(packaged_dir),
            "candidate_capture_status": _text(capture_result.get("status")),
        },
        "replay_draft_dir": str(target_dir),
        "replay_draft_content_path": str(target_dir / "draft_content.json"),
        "comparison": "replay_C_vs_packaged_B",
        "timeline_unit": "microseconds",
        "merge": merge_report,
        "parameter_review": parameter_review,
        "verification": {
            "raw_change_count": len(raw_changes),
            "raw_diff_truncated": raw_truncated,
            "semantic_change_count": int(semantic_diff.get("changed_count") or 0),
            "semantic_resource_review_count": len(_list(semantic_diff.get("resource_review"))),
            "semantic_status": _text(semantic_diff.get("status")),
        },
        "write_policy": "isolated_replay_only_no_production_write",
    }
    target_parent.mkdir(parents=True, exist_ok=True)
    acceptance_path = target_parent / "acceptance_report.json"
    report["acceptance_report_path"] = str(acceptance_path)
    acceptance_report = {
        "schema_version": "packaging-capture-acceptance-v1",
        "capture": {
            "status": _text(capture_result.get("status")),
            "raw_change_count": int(_dict(capture_result.get("raw_diff")).get("change_count") or 0),
            "raw_diff_truncated": bool(_dict(capture_result.get("raw_diff")).get("truncated")),
            "semantic_change_count": int(_dict(capture_result.get("semantic_diff")).get("changed_count") or 0),
            "resource_review_count": len(_list(_dict(capture_result.get("semantic_diff")).get("resource_review"))),
            "track_roles_need_confirmation": bool(_dict(capture_result.get("track_roles")).get("needs_confirmation")),
            "compatibility": copy.deepcopy(_dict(capture_result.get("compatibility"))),
            "parameter_review": copy.deepcopy(parameter_review),
        },
        "replay": report,
        "write_policy": "isolated_replay_only_no_production_write",
    }
    _write_json(acceptance_path, acceptance_report)
    _write_json(target_parent / "replay_result.json", report)
    _write_json(target_dir / "replay_manifest.json", report)
    _write_json(target_dir / "replay_raw_diff.json", {"changes": raw_changes, "truncated": raw_truncated})
    _write_json(target_dir / "replay_semantic_diff.json", semantic_diff)
    return report


__all__ = [
    "PACKAGING_REPLAY_SCHEMA_VERSION",
    "write_replay_artifact",
]
