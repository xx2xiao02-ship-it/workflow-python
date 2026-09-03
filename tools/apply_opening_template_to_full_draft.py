"""把 5 秒片头模板套用到完整 Windows 剪映草稿的前 5 秒。"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import uuid
from pathlib import Path
from typing import Any


WINDOW_US = 5_000_000


def _replace_root(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace_root(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_root(item, old, new) for key, item in value.items()}
    return value


def _clip_segment(segment: dict[str, Any], start_limit: int = 0, end_limit: int = WINDOW_US) -> dict[str, Any] | None:
    timerange = segment.get("target_timerange") or {}
    start = int(timerange.get("start", 0))
    duration = int(timerange.get("duration", 0))
    end = start + duration
    if end <= start_limit or start >= end_limit or duration <= 0:
        return None
    clip_start = max(start, start_limit)
    clip_end = min(end, end_limit)
    result = copy.deepcopy(segment)
    result["target_timerange"] = {"start": clip_start, "duration": clip_end - clip_start}
    source = result.get("source_timerange")
    if isinstance(source, dict):
        speed = float(result.get("speed", 1.0) or 1.0)
        source_start = int(source.get("start", 0))
        source_duration = int(source.get("duration", 0))
        source_offset = int(round((clip_start - start) * speed))
        source_clip_duration = int(round((clip_end - clip_start) * speed))
        result["source_timerange"] = {
            "start": source_start + source_offset,
            "duration": min(max(source_duration - source_offset, 0), source_clip_duration),
        }
    return result


def _keep_after_window(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for segment in segments:
        clipped = _clip_segment(segment, start_limit=WINDOW_US, end_limit=10**18)
        if clipped is not None:
            result.append(clipped)
    return result


def _template_window(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for segment in segments:
        clipped = _clip_segment(segment)
        if clipped is not None:
            result.append(clipped)
    return result


def _merge_materials(target: dict[str, Any], template: dict[str, Any]) -> None:
    materials = target.setdefault("materials", {})
    for key, source_items in template.get("materials", {}).items():
        if not isinstance(source_items, list):
            continue
        existing = materials.setdefault(key, [])
        if not isinstance(existing, list):
            materials[key] = copy.deepcopy(source_items)
            continue
        by_id = {item.get("id"): index for index, item in enumerate(existing) if isinstance(item, dict) and item.get("id")}
        for item in source_items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id and item_id in by_id:
                existing[by_id[item_id]] = copy.deepcopy(item)
            else:
                existing.append(copy.deepcopy(item))
                if item_id:
                    by_id[item_id] = len(existing) - 1


def _find_track(tracks: list[dict[str, Any]], track_type: str, name: str) -> dict[str, Any] | None:
    return next((track for track in tracks if track.get("type") == track_type and track.get("name") == name), None)


def _replace_track_window(target_track: dict[str, Any], template_track: dict[str, Any]) -> None:
    after = _keep_after_window(target_track.get("segments", []))
    inside = _template_window(template_track.get("segments", []))
    target_track["segments"] = sorted(
        inside + after,
        key=lambda item: int((item.get("target_timerange") or {}).get("start", 0)),
    )


def _copy_assets(source: Path, target: Path) -> None:
    source_assets = source / "assets"
    if source_assets.is_dir():
        shutil.copytree(source_assets, target / "assets", dirs_exist_ok=True)


def _update_meta(target: Path, data: dict[str, Any], old_target_root: str) -> None:
    meta_path = target / "draft_meta_info.json"
    if not meta_path.is_file():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    meta = _replace_root(meta, old_target_root, str(target))
    meta["draft_fold_path"] = str(target).replace("\\", "/")
    meta["draft_id"] = data["id"]
    meta["draft_name"] = target.name
    meta["tm_duration"] = int(data.get("duration", 0))
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _load_draft_data(directory: Path) -> dict[str, Any]:
    """Read full Jianying timeline data across old and new DraftFolder layouts."""

    for filename in ("draft_info.json", "draft_content.json", "template-2.tmp"):
        path = directory / filename
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and ("tracks" in value or "materials" in value):
            return value
    raise ValueError(f"没有找到可识别的剪映草稿时间线数据：{directory}")


def build(template_dir: Path, full_dir: Path, output_dir: Path) -> dict[str, Any]:
    template_dir = template_dir.resolve()
    full_dir = full_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"目标草稿已存在，为避免覆盖已停止: {output_dir}")

    template_data = _load_draft_data(template_dir)
    data = _load_draft_data(full_dir)
    shutil.copytree(full_dir, output_dir)
    _copy_assets(template_dir, output_dir)

    data = _replace_root(data, str(full_dir), str(output_dir))
    template_data = _replace_root(template_data, str(template_dir), str(output_dir))
    _merge_materials(data, template_data)

    target_tracks = data.get("tracks", [])
    template_tracks = template_data.get("tracks", [])

    # 模板视频是完整草稿中最上层的视觉段；替换完整草稿最高视频轨道的前 5 秒。
    template_video = next((track for track in template_tracks if track.get("type") == "video"), None)
    video_tracks = [track for track in target_tracks if track.get("type") == "video"]
    if template_video is not None and video_tracks:
        _replace_track_window(video_tracks[-1], template_video)

    # 同名解说和 BGM 替换前 5 秒；未命名音频轨由模板追加。
    for name in ("解说", "BGM"):
        template_track = _find_track(template_tracks, "audio", name)
        target_track = _find_track(target_tracks, "audio", name)
        if template_track is not None and target_track is not None:
            _replace_track_window(target_track, template_track)

    # 用片头模板文字替换完整草稿前 5 秒字幕，同时保留模板的额外文字模板轨。
    target_subtitle = _find_track(target_tracks, "text", "字幕")
    template_subtitle = next(
        (track for track in template_tracks if track.get("type") == "text" and track.get("name") != "字幕"),
        None,
    )
    if target_subtitle is not None and template_subtitle is not None:
        _replace_track_window(target_subtitle, template_subtitle)

    for track in template_tracks:
        track_type = track.get("type")
        name = track.get("name", "")
        # 第一条模板文字轨已经替换进完整草稿的字幕轨；第二条文字轨是文字模板，必须保留。
        if track_type == "video" or (track_type == "audio" and name in {"解说", "BGM"}) or track is template_subtitle or (track_type == "text" and name == "字幕"):
            continue
        extra = copy.deepcopy(track)
        extra["segments"] = _template_window(extra.get("segments", []))
        if extra["segments"]:
            target_tracks.append(extra)

    data["tracks"] = target_tracks
    data["id"] = str(uuid.uuid4()).upper()
    data["name"] = output_dir.name
    data["platform"] = "windows"
    data["last_modified_platform"] = "windows"

    rendered = json.dumps(data, ensure_ascii=False, indent=2)
    for filename in ("draft_info.json", "draft_content.json", "draft_content.json.bak", "template-2.tmp"):
        (output_dir / filename).write_text(rendered, encoding="utf-8")
    _update_meta(output_dir, data, str(full_dir))

    paths = []
    for key in ("videos", "audios"):
        paths.extend(
            item.get("path", "")
            for item in data.get("materials", {}).get(key, [])
            if isinstance(item, dict) and item.get("path")
        )
    missing = [path for path in paths if not Path(path).is_file()]
    first_window = []
    for track in data.get("tracks", []):
        count = sum(
            int((segment.get("target_timerange") or {}).get("start", 0)) < WINDOW_US
            and int((segment.get("target_timerange") or {}).get("start", 0)) + int((segment.get("target_timerange") or {}).get("duration", 0)) > 0
            for segment in track.get("segments", [])
        )
        if count:
            first_window.append((track.get("type"), track.get("name", ""), count))
    return {
        "output": str(output_dir),
        "duration_us": int(data.get("duration", 0)),
        "first_5s_tracks": first_window,
        "effect_track_count": sum(track.get("type") == "effect" for track in data.get("tracks", [])),
        "audio_track_count": sum(track.get("type") == "audio" for track in data.get("tracks", [])),
        "missing_media_count": len(missing),
        "missing_media_examples": missing[:5],
        "template_source": str(template_dir),
        "full_source": str(full_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--full", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.template, args.full, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
