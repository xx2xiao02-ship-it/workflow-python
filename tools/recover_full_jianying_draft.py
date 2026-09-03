"""从可解析的 draft_info.json 恢复完整 Windows 剪映草稿。

该工具不会读取或伪造加密的 draft_content.json；它以源草稿的明文
draft_info.json 为完整时间轴和素材契约，并将素材路径改写到新的草稿目录。
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import uuid
from pathlib import Path
from typing import Any


def _rewrite(value: Any, source_root: str, target_root: str) -> Any:
    if isinstance(value, str):
        return value.replace(source_root, target_root)
    if isinstance(value, list):
        return [_rewrite(item, source_root, target_root) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, source_root, target_root) for key, item in value.items()}
    return value


def _replace_exact(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_exact(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_exact(item, replacements) for key, item in value.items()}
    return value


def _vendor_external_media(data: dict[str, Any], target: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """把仍引用剪映缓存的本地音频复制进草稿，避免导入后丢失音效。"""

    replacements: dict[str, str] = {}
    external_dir = target / "assets" / "external_audio"
    for collection_name in ("videos", "audios"):
        for material in data.get("materials", {}).get(collection_name, []):
            if not isinstance(material, dict):
                continue
            raw_path = material.get("path")
            if not raw_path:
                continue
            source_path = Path(raw_path)
            if not source_path.is_file():
                continue
            try:
                source_path.relative_to(target)
                continue
            except ValueError:
                pass
            target_path = external_dir / source_path.name
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if not target_path.exists():
                shutil.copy2(source_path, target_path)
            # 草稿 JSON 可能使用正斜杠，而 Path 在 Windows 上会规范成反斜杠；
            # 同时直接更新 material.path，确保不会残留缓存路径。
            material["path"] = str(target_path)
            replacements[str(source_path)] = str(target_path)
            replacements[str(raw_path)] = str(target_path)
    if replacements:
        data = _replace_exact(data, replacements)
    return data, replacements


def _count_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for track in data.get("tracks", []):
        segments = track.get("segments", [])
        result.append(
            {
                "name": track.get("name", ""),
                "type": track.get("type", ""),
                "count": len(segments) if isinstance(segments, list) else 0,
            }
        )
    return result


def _crop_to_window(data: dict[str, Any], window_us: int) -> None:
    """裁剪所有轨道片段到 [0, window_us)，保留片段原有引用和动画参数。"""

    if window_us <= 0:
        raise ValueError("window_us 必须大于 0")
    for track in data.get("tracks", []):
        retained: list[dict[str, Any]] = []
        for original in track.get("segments", []):
            timerange = original.get("target_timerange") or {}
            start = int(timerange.get("start", 0))
            duration = int(timerange.get("duration", 0))
            end = start + duration
            if end <= 0 or start >= window_us or duration <= 0:
                continue
            clip_start = max(start, 0)
            clip_end = min(end, window_us)
            segment = copy.deepcopy(original)
            segment["target_timerange"] = {
                "start": clip_start,
                "duration": clip_end - clip_start,
            }
            source_timerange = segment.get("source_timerange")
            if isinstance(source_timerange, dict):
                speed = float(segment.get("speed", 1.0) or 1.0)
                source_start = int(source_timerange.get("start", 0))
                source_duration = int(source_timerange.get("duration", 0))
                target_offset = clip_start - start
                source_offset = int(round(target_offset * speed))
                clipped_source_duration = int(round((clip_end - clip_start) * speed))
                segment["source_timerange"] = {
                    "start": source_start + source_offset,
                    "duration": min(source_duration - source_offset, clipped_source_duration),
                }
            retained.append(segment)
        track["segments"] = retained
    data["duration"] = window_us


def build(
    source: Path,
    target: Path,
    base: Path | None = None,
    source_json: Path | None = None,
    window_us: int | None = None,
) -> dict[str, Any]:
    source = source.resolve()
    target = target.resolve()
    json_path = source_json.resolve() if source_json else source / "draft_info.json"
    if not json_path.is_file():
        raise FileNotFoundError(f"源草稿缺少 draft_info.json: {source}")
    if target.exists():
        raise FileExistsError(f"目标草稿已存在，为避免覆盖已停止: {target}")

    # 以现有可打开的 0821 目录作为元文件基底；素材和完整时间轴来自原始完整草稿。
    if base is not None:
        base = base.resolve()
        shutil.copytree(base, target)
    else:
        target.mkdir(parents=True)

    # 合并完整源草稿的素材目录，保留基底中的剪映元文件。
    source_assets = source / "assets"
    if source_assets.is_dir():
        shutil.copytree(source_assets, target / "assets", dirs_exist_ok=True)

    source_root = str(source)
    target_root = str(target)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    data = _rewrite(data, source_root, target_root)
    data, external_media = _vendor_external_media(data, target)
    if window_us is not None:
        _crop_to_window(data, window_us)
    data["id"] = str(uuid.uuid4()).upper()
    data["name"] = target.name
    data["platform"] = "windows"
    data["last_modified_platform"] = "windows"

    rendered = json.dumps(data, ensure_ascii=False, indent=2)
    (target / "draft_info.json").write_text(rendered, encoding="utf-8")
    (target / "draft_content.json").write_text(rendered, encoding="utf-8")

    # 更新剪映侧元信息，避免仍显示 0821 和 5 秒时长。
    meta_path = target / "draft_meta_info.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta = _rewrite(meta, source_root, target_root)
            meta["draft_fold_path"] = target_root.replace("\\", "/")
            meta["draft_id"] = data["id"]
            meta["draft_name"] = target.name
            meta["tm_duration"] = int(data.get("duration", 0))
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        except json.JSONDecodeError:
            pass

    # 递归确认所有草稿内引用的本地媒体路径已复制到新目录。
    missing: list[str] = []
    for collection_name in ("videos", "audios"):
        for material in data.get("materials", {}).get(collection_name, []):
            path = material.get("path") if isinstance(material, dict) else None
            if path and not Path(path).is_file():
                missing.append(path)

    tracks = _count_segments(data)
    expected = {
        "duration_us": int(data.get("duration", 0)),
        "track_counts": tracks,
        "media_paths_exist": not missing,
        "missing_media_count": len(missing),
    }
    if missing:
        expected["missing_media_examples"] = missing[:5]
    return {
        "output": str(target),
        "draft_id": data["id"],
        "duration_us": int(data.get("duration", 0)),
        "window_us": window_us,
        "tracks": tracks,
        "material_counts": {
            key: len(value) for key, value in data.get("materials", {}).items() if isinstance(value, list) and value
        },
        "validation": expected,
        "external_media_copied": external_media,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--source-json", type=Path, help="使用指定的可解析备份 JSON 作为时间轴和特效数据")
    parser.add_argument("--window-us", type=int, help="只保留从 0 开始的时间窗口，单位为微秒")
    args = parser.parse_args()
    result = build(args.source, args.target, args.base, args.source_json, args.window_us)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
