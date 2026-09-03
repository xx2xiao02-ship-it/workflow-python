"""从人工包装草稿 B 生成隔离的无包装基准草稿 A。

该入口用于测试区没有现成 A 的情况。它只移除明确可识别的包装轨道、
包装素材引用、包装关键帧和文字模板，不会修改 B，也不会写正式包装包。
生成结果必须被用户在剪映中检查；它是 ``derived_from_packaged_b`` 候选基线，
不能替代同一脚本真正生成的原始无包装草稿。
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .editing_style_package import load_draft_content
from .packaging_capture import _draft_content_path, _sha256_file
from .packaging_replay import _id_aliases, _rewrite_local_paths


PACKAGING_BASELINE_SCHEMA_VERSION = "packaging-baseline-v1"
PACKAGING_BASELINE_REGISTRY_SCHEMA_VERSION = "packaging-baseline-registry-v1"
_BASELINE_REGISTRY_MANIFEST = "baseline_registry_manifest.json"
_PACKAGING_COLLECTIONS = {
    "video_effects",
    "effects",
    "plugin_effects",
    "filters",
    "transitions",
    "audio_effects",
    "audio_fades",
    "text_templates",
    "material_animations",
}
_REFERENCE_KEYS = {
    "material_id",
    "extra_material_refs",
    "music_id",
    "audio_id",
    "video_id",
    "image_id",
    "canvas_id",
    "speed_id",
    "vocal_separation_id",
    "sound_channel_mapping_id",
    "text_id",
}
_ESSENTIAL_COLLECTIONS = {
    "videos",
    "images",
    "audios",
    "canvases",
    "speeds",
    "sound_channel_mappings",
    "vocal_separations",
}


class PackagingBaselineError(ValueError):
    """无包装基线生成输入或输出错误。"""


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


def _parse_text_value(item: Mapping[str, Any]) -> str:
    """读取字幕文本，不把包装标题等非字幕文本放入脚本指纹。"""

    raw = item.get("content")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, Mapping):
            return _text(parsed.get("text"))
    return _text(item.get("base_content") or item.get("text"))


def _script_fingerprint_payload(draft: Mapping[str, Any]) -> dict[str, Any]:
    """生成忽略包装增量和剪映易变 ID 的脚本结构指纹载荷。"""

    materials = _dict(draft.get("materials"))
    visual_materials: list[dict[str, Any]] = []
    for collection in ("videos", "images"):
        for raw_item in _list(materials.get(collection)):
            item = _dict(raw_item)
            visual_materials.append(
                {
                    "collection": collection,
                    "name": _text(item.get("material_name") or item.get("name")),
                    "duration": int(item.get("duration") or 0),
                    "width": int(item.get("width") or 0),
                    "height": int(item.get("height") or 0),
                }
            )

    content_tracks: list[dict[str, Any]] = []
    for raw_track in _list(draft.get("tracks")):
        track = _dict(raw_track)
        if _is_packaging_track(track):
            continue
        track_type = _text(track.get("type") or track.get("track_type")).lower()
        # 视频脚本的稳定锚点是画面轨与字幕轨。音频轨可能在包装时新增，
        # 因此不把它作为自动匹配的硬条件。
        if track_type not in {"video", "text"}:
            continue
        name = _text(track.get("name"))
        if name.lower().startswith("packaging-") or name.startswith("包装-"):
            continue
        segments: list[dict[str, int]] = []
        for raw_segment in _list(track.get("segments")):
            segment = _dict(raw_segment)
            timerange = _dict(segment.get("target_timerange"))
            segments.append(
                {
                    "start": int(timerange.get("start") or 0),
                    "duration": int(timerange.get("duration") or 0),
                }
            )
        content_tracks.append({"type": track_type, "name": name, "segments": segments})

    subtitle_texts: list[str] = []
    for raw_item in _list(materials.get("texts")):
        item = _dict(raw_item)
        item_type = _text(item.get("type")).lower()
        if item_type not in {"subtitle", "caption", ""}:
            continue
        subtitle_texts.append(_parse_text_value(item))

    canvas = _dict(draft.get("canvas_config"))
    return {
        "duration_us": int(draft.get("duration") or 0),
        "canvas": {
            "width": int(canvas.get("width") or 0),
            "height": int(canvas.get("height") or 0),
            "fps": int(canvas.get("fps") or 0),
        },
        "visual_materials": visual_materials,
        "content_tracks": content_tracks,
        "subtitle_texts": subtitle_texts,
    }


def _base_script_fingerprint_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """生成允许包装增量存在的宽松脚本指纹载荷。

    包装时可能新增文字、音频、关键帧并轻微改变总时长；画面素材集合和
    视频镜头顺序仍是更稳定的同源依据。
    """

    return {
        "canvas": copy.deepcopy(_dict(payload.get("canvas"))),
        "visual_materials": copy.deepcopy(_list(payload.get("visual_materials"))),
        "video_tracks": [
            copy.deepcopy(track)
            for track in _list(payload.get("content_tracks"))
            if _dict(track).get("type") == "video"
        ],
    }


def _fingerprint_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def script_fingerprint(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """返回同一脚本 A/B 可共享的稳定指纹及其载荷。"""

    if isinstance(source, Mapping):
        draft = source
        source_path = ""
    else:
        draft_path = _draft_content_path(source)
        draft = load_draft_content(draft_path)
        source_path = str(draft_path)
    payload = _script_fingerprint_payload(draft)
    base_payload = _base_script_fingerprint_payload(payload)
    return {
        "fingerprint": _fingerprint_payload(payload),
        "base_fingerprint": _fingerprint_payload(base_payload),
        "payload": payload,
        "base_payload": base_payload,
        "source": source_path,
    }


def _has_explicit_packaging(draft: Mapping[str, Any]) -> bool:
    if any(_is_packaging_track(_dict(track)) for track in _list(draft.get("tracks"))):
        return True
    materials = _dict(draft.get("materials"))
    return any(_list(materials.get(collection)) for collection in _PACKAGING_COLLECTIONS)


def _registry_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _is_packaging_track(track: Mapping[str, Any]) -> bool:
    track_type = _text(track.get("type") or track.get("track_type")).lower()
    name = _text(track.get("name"))
    return track_type in {"effect", "filter"} or name.startswith("包装-") or name.lower().startswith("packaging-")


def _collect_reference_ids(value: Any, output: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) in _REFERENCE_KEYS:
                if isinstance(child, str) and child.strip():
                    output.add(child.strip())
                elif isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
                    output.update(str(item).strip() for item in child if str(item).strip())
            _collect_reference_ids(child, output)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _collect_reference_ids(child, output)


def _strip_segment(segment: Mapping[str, Any], referenced_ids: set[str]) -> dict[str, Any]:
    result = copy.deepcopy(dict(segment))
    # 包装动作最容易遗漏的部分是内容轨上的关键帧和引用；基线必须清零。
    result["common_keyframes"] = []
    result["keyframe_refs"] = []
    refs = [str(item) for item in _list(result.get("extra_material_refs")) if str(item) in referenced_ids]
    result["extra_material_refs"] = refs
    return result


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def register_baseline_snapshot(
    baseline_source: str | Path,
    registry_root: str | Path,
    *,
    packaged_source: str | Path | None = None,
    registration_status: str = "imported_existing_baseline",
) -> dict[str, Any]:
    """把 A 复制到实验区内部，形成不依赖剪映原目录的基线快照。

    剪映里的 A 可能被用户直接编辑成 B，因此这里只保存一份内部副本。
    已存在且哈希一致的快照不会重复复制，也不会覆盖任何旧快照。
    """

    baseline_path = _draft_content_path(baseline_source)
    baseline_dir = baseline_path.parent
    fingerprint_info = script_fingerprint(baseline_path)
    fingerprint = str(fingerprint_info["fingerprint"])
    target_root = Path(registry_root).expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)

    existing_candidates = sorted(
        target_root.glob(f"{fingerprint}*/{_BASELINE_REGISTRY_MANIFEST}"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    source_hash = _sha256_file(baseline_path)
    for manifest_path in existing_candidates:
        manifest = _registry_manifest(manifest_path)
        snapshot = Path(str(_dict(manifest.get("baseline_a")).get("snapshot_dir") or manifest_path.parent)).expanduser().resolve()
        snapshot_content = snapshot / "draft_content.json"
        if (
            manifest.get("script_fingerprint") == fingerprint
            and snapshot_content.is_file()
            and _sha256_file(snapshot_content) == str(_dict(manifest.get("baseline_a")).get("draft_content_sha256") or "")
        ):
            return manifest

    target_dir = target_root / fingerprint
    if target_dir.exists():
        target_dir = target_root / f"{fingerprint}_{uuid.uuid4().hex[:8]}"
    shutil.copytree(baseline_dir, target_dir)
    snapshot_hash = _sha256_file(target_dir / "draft_content.json")
    manifest = {
        "schema_version": PACKAGING_BASELINE_REGISTRY_SCHEMA_VERSION,
        "status": "internal_immutable_snapshot",
        "registration_status": registration_status,
        "script_fingerprint": fingerprint,
        "script_fingerprint_payload": fingerprint_info["payload"],
        "base_script_fingerprint": str(fingerprint_info["base_fingerprint"]),
        "base_script_fingerprint_payload": fingerprint_info["base_payload"],
        "source": {
            "baseline_a_dir": str(baseline_dir),
            "baseline_a_draft_content_sha256": source_hash,
            "packaged_b": str(Path(packaged_source).expanduser().resolve()) if packaged_source else "",
        },
        "baseline_a": {
            "snapshot_dir": str(target_dir),
            "draft_content_path": str(target_dir / "draft_content.json"),
            "draft_content_sha256": snapshot_hash,
        },
        "write_policy": "internal_baseline_only_no_production_bundle_write",
    }
    _write_json(target_dir / _BASELINE_REGISTRY_MANIFEST, manifest)
    return manifest


def _manifest_is_valid_snapshot(manifest: Mapping[str, Any], fingerprint: str) -> bool:
    if manifest.get("script_fingerprint") != fingerprint:
        return False
    baseline = _dict(manifest.get("baseline_a"))
    snapshot_dir = Path(str(baseline.get("snapshot_dir") or "")).expanduser().resolve()
    content_path = snapshot_dir / "draft_content.json"
    expected_hash = str(baseline.get("draft_content_sha256") or "")
    if not content_path.is_file() or not expected_hash:
        return False
    try:
        return _sha256_file(content_path) == expected_hash
    except OSError:
        return False


def _manifest_base_fingerprint(manifest: Mapping[str, Any]) -> str:
    stored = _text(manifest.get("base_script_fingerprint"))
    if stored:
        return stored
    baseline = _dict(manifest.get("baseline_a"))
    snapshot_dir = Path(str(baseline.get("snapshot_dir") or "")).expanduser().resolve()
    content_path = snapshot_dir / "draft_content.json"
    if not content_path.is_file():
        return ""
    try:
        return str(script_fingerprint(content_path)["base_fingerprint"])
    except (OSError, ValueError, json.JSONDecodeError):
        return ""


def resolve_baseline_for_packaged(
    packaged_source: str | Path,
    registry_root: str | Path,
    *,
    search_roots: Sequence[str | Path] = (),
) -> dict[str, Any]:
    """按 B 的脚本指纹自动解析内部 A；找不到时再导入唯一的本机 A。"""

    packaged_path = _draft_content_path(packaged_source)
    packaged_dir = packaged_path.parent
    packaged_fingerprint = script_fingerprint(packaged_path)
    fingerprint = str(packaged_fingerprint["fingerprint"])
    base_fingerprint = str(packaged_fingerprint["base_fingerprint"])
    registry = Path(registry_root).expanduser().resolve()

    registry_matches: list[tuple[str, dict[str, Any]]] = []
    if registry.is_dir():
        for manifest_path in registry.glob(f"*/{_BASELINE_REGISTRY_MANIFEST}"):
            manifest = _registry_manifest(manifest_path)
            if _manifest_is_valid_snapshot(manifest, fingerprint):
                registry_matches.append(("internal_registry", manifest))
            elif _manifest_is_valid_snapshot(manifest, _text(manifest.get("script_fingerprint"))):
                # 旧快照没有宽松指纹字段时，仍读取快照内容计算一次。
                if _manifest_base_fingerprint(manifest) == base_fingerprint:
                    registry_matches.append(("internal_registry_relaxed", manifest))
    if registry_matches:
        # 同一指纹一般只有一个快照；多个时优先记录过同一 B 路径的快照。
        packaged_resolved = str(packaged_dir)
        registry_matches.sort(
            key=lambda item: (
                item[0] == "internal_registry",
                str(_dict(item[1].get("source")).get("packaged_b") or "") == packaged_resolved,
                str(_dict(item[1].get("baseline_a")).get("snapshot_dir") or ""),
            ),
            reverse=True,
        )
        match_method, selected = registry_matches[0]
        return {
            "status": "matched",
            "match_method": match_method,
            "script_fingerprint": fingerprint,
            "base_script_fingerprint": base_fingerprint,
            "baseline": _dict(selected.get("baseline_a")),
            "registry_manifest": selected,
        }

    roots: list[Path] = [packaged_dir.parent]
    roots.extend(Path(root).expanduser().resolve() for root in search_roots)
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir() or child.resolve() == packaged_dir.resolve():
                continue
            key = str(child.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            content_path = child / "draft_content.json"
            if not content_path.is_file():
                continue
            try:
                candidate = load_draft_content(content_path)
                if _has_explicit_packaging(candidate):
                    continue
                candidate_fingerprint = script_fingerprint(candidate)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if candidate_fingerprint["fingerprint"] != fingerprint:
                continue
            name = _text(candidate.get("name")) or child.name
            score = 0
            if any(token in name.lower() for token in ("基准", "无包装", "baseline", "unwrapped")):
                score += 20
            manifest = _registry_manifest(child / "baseline_manifest.json") if (child / "baseline_manifest.json").is_file() else {}
            source = _dict(manifest.get("source"))
            if str(Path(str(source.get("packaged_b") or "")).expanduser().resolve()) == str(packaged_dir.resolve()):
                score += 30
            candidates.append((score, child, candidate_fingerprint))

    if not candidates:
        return {
            "status": "not_found",
            "match_method": "none",
            "script_fingerprint": fingerprint,
            "base_script_fingerprint": base_fingerprint,
            "message": "未找到与 B 同一脚本的内部无包装基线 A。",
        }

    candidates.sort(key=lambda item: (item[0], item[1].stat().st_mtime if item[1].exists() else 0), reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return {
            "status": "ambiguous",
            "match_method": "draft_scan",
            "script_fingerprint": fingerprint,
            "base_script_fingerprint": base_fingerprint,
            "candidates": [{"draft_dir": str(item[1]), "score": item[0]} for item in candidates],
            "message": "找到多个同脚本无包装基线 A，需要在高级设置中确认。",
        }

    _, selected_dir, _ = candidates[0]
    manifest = register_baseline_snapshot(
        selected_dir,
        registry,
        packaged_source=packaged_dir,
        registration_status="auto_imported_existing_baseline",
    )
    return {
        "status": "matched",
        "match_method": "draft_scan_then_snapshot",
        "script_fingerprint": fingerprint,
        "base_script_fingerprint": base_fingerprint,
        "baseline": _dict(manifest.get("baseline_a")),
        "registry_manifest": manifest,
    }


def _patch_metadata(target_dir: Path, *, draft_name: str, draft_id: str, duration_us: int) -> None:
    now = int(time.time())
    content_path = target_dir / "draft_content.json"
    content = json.loads(content_path.read_text(encoding="utf-8"))
    content["id"] = draft_id
    content["name"] = draft_name
    content["create_time"] = now
    content["update_time"] = now
    _write_json(content_path, content)
    for filename in ("draft_info.json", "draft_meta_info.json"):
        path = target_dir / filename
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        if filename == "draft_info.json":
            value.update({"id": draft_id, "create_time": now, "duration": duration_us})
        else:
            value.update(
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
        _write_json(path, value)


def create_unwrapped_baseline_from_packaged(
    packaged_source: str | Path,
    output_root: str | Path,
    *,
    draft_name: str = "",
) -> dict[str, Any]:
    """从 B 创建一个不可覆盖的无包装基线 A 候选草稿。"""

    packaged_path = _draft_content_path(packaged_source)
    packaged_dir = packaged_path.parent
    source = load_draft_content(packaged_path)
    target_root = Path(output_root).expanduser().resolve()
    if not target_root.is_dir():
        raise PackagingBaselineError(f"输出草稿根目录不存在：{target_root}")
    name = _text(draft_name) or f"{_text(source.get('name')) or packaged_dir.name}_无包装基线_A"
    if name in {".", ".."} or any(char in name for char in '<>:"/\\|?*'):
        raise PackagingBaselineError("基准草稿名称为空或含 Windows 不允许的字符")
    target_dir = target_root / name
    if target_dir.exists():
        raise PackagingBaselineError(f"基准草稿已存在，为避免覆盖：{target_dir}")
    if target_dir == packaged_dir:
        raise PackagingBaselineError("基准草稿输出不能覆盖 B 草稿")

    retained_tracks: list[dict[str, Any]] = []
    for raw_track in _list(source.get("tracks")):
        track = _dict(raw_track)
        if _is_packaging_track(track):
            continue
        kept = copy.deepcopy(track)
        # 先保留引用用于识别 A 的内容素材；待素材保留集合确定后再过滤包装引用。
        kept["segments"] = [copy.deepcopy(_dict(segment)) for segment in _list(track.get("segments"))]
        retained_tracks.append(kept)

    referenced_ids: set[str] = set()
    for track in retained_tracks:
        _collect_reference_ids(track, referenced_ids)

    retained_materials: dict[str, list[Any]] = {}
    source_materials = _dict(source.get("materials"))
    for collection, raw_items in source_materials.items():
        if collection in _PACKAGING_COLLECTIONS:
            retained_materials[collection] = []
            continue
        items = []
        for raw_item in _list(raw_items):
            item = _dict(raw_item)
            # B 的片头文字模板通常是 type=text；字幕仍是 type=subtitle。
            if collection == "texts" and _text(item.get("type")).lower() not in {"subtitle", "caption", ""}:
                continue
            aliases = set(_id_aliases(item))
            if collection in _ESSENTIAL_COLLECTIONS or aliases & referenced_ids or collection == "texts":
                items.append(copy.deepcopy(item))
        retained_materials[collection] = items

    # 收集过滤后的引用，再清理内容轨中可能残留的包装引用。
    retained_ids = {
        alias
        for raw_items in retained_materials.values()
        for raw_item in _list(raw_items)
        for alias in _id_aliases(_dict(raw_item))
    }
    for track in retained_tracks:
        track["segments"] = [_strip_segment(_dict(segment), retained_ids) for segment in _list(track.get("segments"))]

    baseline = copy.deepcopy(dict(source))
    baseline["tracks"] = retained_tracks
    baseline["materials"] = retained_materials
    baseline["source"] = "derived_from_packaged_b_strip_packaging"

    shutil.copytree(packaged_dir, target_dir)
    copied_paths: dict[str, str] = {}
    baseline = _rewrite_local_paths(
        baseline,
        baseline_root=packaged_dir,
        packaged_root=packaged_dir,
        target_root=target_dir,
        copied_paths=copied_paths,
    )
    draft_id = str(uuid.uuid4()).upper()
    baseline["id"] = draft_id
    baseline["name"] = name
    baseline["create_time"] = int(time.time())
    baseline["update_time"] = int(time.time())
    _write_json(target_dir / "draft_content.json", baseline)
    _patch_metadata(target_dir, draft_name=name, draft_id=draft_id, duration_us=int(baseline.get("duration") or 0))

    manifest = {
        "schema_version": PACKAGING_BASELINE_SCHEMA_VERSION,
        "status": "generated_candidate",
        "source": {
            "packaged_b": str(packaged_dir),
            "packaged_b_draft_content_sha256": _sha256_file(packaged_path),
        },
        "baseline_a": {
            "draft_dir": str(target_dir),
            "draft_content_path": str(target_dir / "draft_content.json"),
            "draft_name": name,
            "draft_content_sha256": _sha256_file(target_dir / "draft_content.json"),
        },
        "removed": {
            "packaging_track_count": sum(1 for raw in _list(source.get("tracks")) if _is_packaging_track(_dict(raw))),
            "packaging_material_collections": sorted(_PACKAGING_COLLECTIONS),
            "keyframes_stripped": True,
        },
        "source_content_policy": "derived_candidate_requires_user_review",
        "write_policy": "test_draft_only_no_production_bundle_write",
    }
    _write_json(target_dir / "baseline_manifest.json", manifest)
    return manifest


__all__ = [
    "PACKAGING_BASELINE_SCHEMA_VERSION",
    "PACKAGING_BASELINE_REGISTRY_SCHEMA_VERSION",
    "PackagingBaselineError",
    "create_unwrapped_baseline_from_packaged",
    "register_baseline_snapshot",
    "resolve_baseline_for_packaged",
    "script_fingerprint",
]
