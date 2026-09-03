"""把真实素材生产结果转换为剪辑层的 ``AssetRegistry``。

这个适配层只做数据交接，不调用模型，也不修改编导层已经冻结的
``director_lock``。当历史 requirement 的 route 与逐镜脚本的媒体路由不一致时，
以 ``shot_video_script.media_type`` 为实际生产结果的来源，并把原 requirement
保留为稳定关联键，避免静态图片或数字人被误路由到 AIGC。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .editing_layer import EditingLayerError
from .governance.contracts import (
    AssetRecord,
    AssetRegistry,
    CaptionRecord,
    DirectorGroup,
    DirectorLockedManifest,
    DirectorShot,
    MaterialRequirement,
    TimeWindow,
)


def _window(value: Mapping[str, Any]) -> TimeWindow:
    start = value.get("start_us", value.get("start"))
    end = value.get("end_us", value.get("end"))
    if start is None or end is None:
        raise EditingLayerError("交接数据缺少 start_us/end_us 时间线")
    return TimeWindow(int(start), int(end))


def director_from_lock(raw: Mapping[str, Any]) -> DirectorLockedManifest:
    """从已冻结的 ``director_lock`` 重建治理对象。"""

    groups = [
        DirectorGroup(
            group_id=item["group_id"],
            group_index=int(item["group_index"]),
            segment_text=item["segment_text"],
            timeline=_window(item["timeline"]),
            shot_ids=list(item["shot_ids"]),
            caption_ids=list(item.get("caption_ids") or []),
            narration_requirement_id=item["narration_requirement_id"],
            metadata=dict(item.get("metadata") or {}),
        )
        for item in raw["groups"]
    ]
    shots = [
        DirectorShot(
            shot_id=item["shot_id"],
            group_id=item["group_id"],
            shot_index=int(item["shot_index"]),
            source_text=item["source_text"],
            clip_role=item["clip_role"],
            story_beat=item["story_beat"],
            timeline=_window(item["timeline"]),
            route_candidates=list(item.get("route_candidates") or []),
            requirement_ids=list(item.get("requirement_ids") or []),
            production_spec=dict(item.get("production_spec") or {}),
        )
        for item in raw["shots"]
    ]
    captions = [
        CaptionRecord(
            item["caption_id"],
            item["group_id"],
            item["text"],
            _window(item["timeline"]),
            item.get("source_node", "capcut_stt"),
        )
        for item in raw.get("captions") or []
    ]
    requirements = [
        MaterialRequirement(
            requirement_id=item["requirement_id"],
            scope=item["scope"],
            asset_type=item["asset_type"],
            role=item["role"],
            group_id=item.get("group_id"),
            shot_ids=list(item.get("shot_ids") or []),
            mandatory=bool(item["mandatory"]),
            route=item["route"],
            coverage_policy=item["coverage_policy"],
            source_node=item["source_node"],
            timeline=_window(item["timeline"]) if item.get("timeline") else None,
            metadata=dict(item.get("metadata") or {}),
        )
        for item in raw["requirements"]
    ]
    return DirectorLockedManifest(
        project_id=raw["project_id"],
        run_id=raw["run_id"],
        plan_version=raw["plan_version"],
        tts_fingerprint=raw["tts_fingerprint"],
        total_timeline=_window(raw["total_timeline"]),
        groups=groups,
        shots=shots,
        captions=captions,
        requirements=requirements,
        provenance=list(raw.get("provenance") or []),
        status=raw["status"],
        cinematic_story=dict(raw.get("cinematic_story") or {}),
        story_review_status=raw.get("story_review_status", "APPROVED"),
        story_revision=int(raw.get("story_revision", 1)),
        narration_assets=list(raw.get("narration_assets") or []),
        digital_human_max_shot_ratio=float(raw.get("digital_human_max_shot_ratio", 0.15)),
    )


def _duration_us(path: Path) -> int:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise EditingLayerError("未找到 ffprobe，不能验证真实素材时长")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return round(float(result.stdout.strip()) * 1_000_000)
    except ValueError as exc:
        raise EditingLayerError(f"无法读取素材时长：{path}") from exc


def _prepared_video(source: Path, target_dir: Path, duration_us: int) -> tuple[Path, int, bool]:
    if not source.is_file():
        raise EditingLayerError(f"素材文件不存在：{source}")
    actual = _duration_us(source)
    if actual >= duration_us:
        return source, actual, False
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise EditingLayerError(f"{source.name} 短于镜头坑位且未找到 ffmpeg，无法补齐")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source.stem}_padded.mp4"
    if not target.is_file() or _duration_us(target) < duration_us:
        extra_s = (duration_us - actual) / 1_000_000 + 0.08
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-vf",
                f"tpad=stop_mode=clone:stop_duration={extra_s:.3f}",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(target),
            ],
            check=True,
            capture_output=True,
        )
    padded = _duration_us(target)
    if padded < duration_us:
        raise EditingLayerError(f"{source.name} 补帧后仍短于镜头坑位")
    return target, padded, True


def _prepared_audio(source: Path, target_dir: Path, duration_us: int) -> tuple[Path, int, bool]:
    if not source.is_file():
        raise EditingLayerError(f"音频文件不存在：{source}")
    actual = _duration_us(source)
    if actual >= duration_us:
        return source, actual, False
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise EditingLayerError(f"{source.name} 短于总时间线且未找到 ffmpeg，无法补齐")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source.stem}_padded.mp3"
    if not target.is_file() or _duration_us(target) < duration_us:
        seconds = duration_us / 1_000_000
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-af",
                "apad",
                "-t",
                f"{seconds:.6f}",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(target),
            ],
            check=True,
            capture_output=True,
        )
    padded = _duration_us(target)
    if padded < duration_us:
        raise EditingLayerError(f"{source.name} 补齐后仍短于总时间线")
    return target, padded, True


# BGM 不应该用 apad 静音补齐。视频超过约一分钟时，按编导大分段边界
# 拆成两个非静音播放段，既保留可审计的拼接点，也避免剪映在后半段播放静音。
BGM_SEGMENT_SPLIT_THRESHOLD_US = 60_000_000
BGM_DURATION_TOLERANCE_US = 100_000


def _bgm_segment_windows(director: DirectorLockedManifest) -> list[TimeWindow]:
    """按编导大分段选择一个语义边界，返回 BGM 的连续播放窗口。"""

    total = director.total_timeline
    if total.duration_us < BGM_SEGMENT_SPLIT_THRESHOLD_US:
        return [total]

    midpoint = total.start_us + total.duration_us // 2
    candidates = sorted({
        group.timeline.end_us
        for group in director.groups
        if total.start_us < group.timeline.end_us < total.end_us
    })
    split_at = min(candidates, key=lambda value: abs(value - midpoint)) if candidates else midpoint
    return [
        TimeWindow(total.start_us, split_at),
        TimeWindow(split_at, total.end_us),
    ]


def _prepared_bgm_segments(
    source: Path,
    target_dir: Path,
    windows: list[TimeWindow],
) -> tuple[list[tuple[Path, int, TimeWindow, bool]], int]:
    """把 BGM 制作为覆盖时间线的非静音分段。

    当源音频短于整片时，从源音频的不同偏移开始循环读取，避免 ``apad``
    产生的尾部静音。返回值中的最后一项是源音频微秒时长，供审计报告使用。
    """

    if not source.is_file():
        raise EditingLayerError(f"BGM 文件不存在：{source}")
    source_duration_us = _duration_us(source)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise EditingLayerError("未找到 ffmpeg，无法生成连续 BGM 分段")
    target_dir.mkdir(parents=True, exist_ok=True)
    # 先去掉模型输出常见的尾部静音，再进行循环/分段；否则第二段在源文件
    # 尾部会继承一段静音，用户会误以为 BGM 又提前结束。
    cleaned_source = target_dir / "bgm_source_clean.mp3"
    source_changed = not cleaned_source.is_file() or cleaned_source.stat().st_mtime < source.stat().st_mtime
    if source_changed:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-af",
                "silenceremove=stop_periods=1:stop_duration=0.5:stop_threshold=-50dB",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(cleaned_source),
            ],
            check=True,
            capture_output=True,
        )
    cleaned_duration_us = _duration_us(cleaned_source)
    if cleaned_duration_us >= 1_000_000:
        input_source = cleaned_source
        source_duration_us = cleaned_duration_us
    else:
        input_source = source
    prepared: list[tuple[Path, int, TimeWindow, bool]] = []
    for index, window in enumerate(windows, start=1):
        target = target_dir / f"bgm_segment_{index:02d}.mp3"
        required_seconds = window.duration_us / 1_000_000
        # 第二段从对应时间线偏移开始，若源音频不够则由 stream_loop 无缝继续读。
        offset_us = min(window.start_us, max(0, source_duration_us - 1_000_000))
        looped = offset_us + window.duration_us > source_duration_us
        # 给 MP3 编码器留出尾部余量，确保实际媒体时长不短于剪辑坑位，
        # 不把编码器的几十毫秒取整误认为已经覆盖。
        if source_changed or not target.is_file() or _duration_us(target) < window.duration_us + BGM_DURATION_TOLERANCE_US:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-loglevel",
                    "error",
                    "-stream_loop",
                    "-1",
                    "-i",
                    str(input_source),
                    "-ss",
                    f"{offset_us / 1_000_000:.6f}",
                    "-t",
                    f"{required_seconds + 0.25:.6f}",
                    "-vn",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(target),
                ],
                check=True,
                capture_output=True,
            )
        actual = _duration_us(target)
        if actual < window.duration_us:
            raise EditingLayerError(f"BGM 分段 {index} 生成后仍短于时间线")
        prepared.append((target, actual, window, looped))
    return prepared, source_duration_us


def _requirement_for_shot(
    shot: DirectorShot,
    requirements: Mapping[str, MaterialRequirement],
    suffix: str,
) -> MaterialRequirement:
    for requirement_id in shot.requirement_ids:
        if requirement_id.endswith(suffix) and requirement_id in requirements:
            return requirements[requirement_id]
    candidates = [
        item
        for item in requirements.values()
        if item.scope == "shot" and shot.shot_id in item.shot_ids and item.asset_type == suffix.split(".")[0]
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise EditingLayerError(f"{shot.shot_id} 找不到 {suffix} requirement")


def _record(
    *,
    asset_id: str,
    requirement: MaterialRequirement,
    asset_type: str,
    role: str,
    source_index: int,
    path: Path | None,
    remote_url: str = "",
    duration_us: int | None,
    actual_timeline: TimeWindow | None,
    asset_version: str,
    metadata: Mapping[str, Any],
) -> AssetRecord:
    return AssetRecord(
        asset_id=asset_id,
        requirement_id=requirement.requirement_id,
        group_id=requirement.group_id,
        shot_ids=list(requirement.shot_ids),
        asset_type=asset_type,
        role=role,
        status="VALIDATED",
        source_node="real_asset_handoff",
        source_index=source_index,
        asset_version=asset_version,
        asset_duration_us=duration_us,
        actual_timeline=actual_timeline,
        local_path=str(path.resolve()) if path else "",
        remote_url=str(remote_url or "").strip(),
        evidence_level="live_external_service",
        metadata=dict(metadata),
    )


def build_live_asset_registry(
    director: DirectorLockedManifest,
    shot_video_script: list[Mapping[str, Any]],
    asset_result: Mapping[str, Any],
    *,
    asset_task_id: str,
    prepared_root: str | Path,
) -> tuple[AssetRegistry, dict[str, Any]]:
    """把本次真实素材任务映射成可供剪辑层消费的统一登记表。"""

    if director.status != "DIRECTOR_LOCKED":
        raise EditingLayerError("素材交接拒绝读取非 DIRECTOR_LOCKED 编导数据")
    if not shot_video_script:
        raise EditingLayerError("逐镜脚本为空，不能建立素材交接")
    requirements = {item.requirement_id: item for item in director.requirements}
    script_by_shot = {str(item.get("shot_id")): item for item in shot_video_script}
    if set(script_by_shot) != {shot.shot_id for shot in director.shots}:
        raise EditingLayerError("逐镜脚本与编导锁定镜头集合不一致")
    prepared_root = Path(prepared_root).expanduser().resolve()
    version = f"live-{asset_task_id}"
    records: list[AssetRecord] = []
    route_trace: list[dict[str, Any]] = []
    source_index = 0

    aigc_results = {
        str(item.get("shot_id")): item
        for item in (asset_result.get("aigc_video_assets", {}).get("shot_results") or [])
        if isinstance(item, Mapping)
    }
    explanation_assets = asset_result.get("explanation_assets")
    explanation_results = {
        str(item.get("shot_id")): item
        for item in ((explanation_assets or {}).get("shot_results") or [])
        if isinstance(item, Mapping)
    } if isinstance(explanation_assets, Mapping) else {}
    digital_results = {
        str(item.get("shot_id")): item
        for item in (asset_result.get("digital_human_assets", {}).get("shot_results") or [])
        if isinstance(item, Mapping)
    }
    media_type_overrides = asset_result.get("media_type_overrides")
    media_type_overrides = media_type_overrides if isinstance(media_type_overrides, Mapping) else {}
    digital_fallbacks = asset_result.get("digital_human_fallbacks")
    digital_fallbacks = digital_fallbacks if isinstance(digital_fallbacks, Mapping) else {}
    first_frame_paths = [
        Path(str(item))
        for item in (asset_result.get("first_frame_assets", {}).get("image_paths") or [])
    ]
    first_frame_trace = asset_result.get("first_frame_prompt_trace")
    first_frame_by_shot = {}
    if isinstance(first_frame_trace, list):
        for index, item in enumerate(first_frame_trace):
            if isinstance(item, Mapping) and index < len(first_frame_paths):
                shot_id = str(item.get("shot_id") or "")
                if shot_id:
                    first_frame_by_shot[shot_id] = first_frame_paths[index]
    for shot_id, item in digital_fallbacks.items():
        if isinstance(item, Mapping) and item.get("image_path"):
            first_frame_by_shot[str(shot_id)] = Path(str(item["image_path"]))
    non_digital_shot_ids = [
        shot.shot_id
        for shot in director.shots
        if str(media_type_overrides.get(shot.shot_id) or script_by_shot[shot.shot_id].get("media_type"))
        not in {"digital_human_video", "explanation", "mixed_explanation"}
    ]
    if any(shot_id not in first_frame_by_shot for shot_id in non_digital_shot_ids):
        raise EditingLayerError(
            "首帧文件与非数字人镜头不一致："
            + "、".join(shot_id for shot_id in non_digital_shot_ids if shot_id not in first_frame_by_shot)
        )

    for shot in director.shots:
        script_item = script_by_shot[shot.shot_id]
        original_media_type = str(script_item.get("media_type") or "").strip()
        media_type = str(media_type_overrides.get(shot.shot_id) or original_media_type).strip()
        route_trace.append({
            "shot_id": shot.shot_id,
            "media_type": media_type,
            "original_media_type": original_media_type,
            "director_requirement_ids": list(shot.requirement_ids),
            "source_of_truth": "asset_result.media_type_overrides" if shot.shot_id in media_type_overrides else "shot_video_script.media_type",
        })
        if media_type == "digital_human_video":
            result = digital_results.get(shot.shot_id)
            if not result or result.get("status") != "succeeded":
                raise EditingLayerError(f"数字人素材未成功：{shot.shot_id}")
            path = Path(str(result.get("video_path") or ""))
            prepared, actual, padded = _prepared_video(path, prepared_root / "videos", shot.timeline.duration_us)
            requirement = _requirement_for_shot(shot, requirements, ".video.aigc")
            records.append(_record(
                asset_id=f"asset-{shot.shot_id}-digital-human",
                requirement=requirement,
                asset_type="video",
                role="digital_human",
                source_index=source_index,
                path=prepared,
                duration_us=actual,
                actual_timeline=shot.timeline,
                asset_version=version,
                metadata={
                    "target_shot_id": shot.shot_id,
                    "resolved_media_type": media_type,
                    "route_source": "shot_video_script",
                    "reused_paid_asset": bool(result.get("reused_paid_asset")),
                    "provider_task_id": result.get("task_id", ""),
                    "padded_to_shot": padded,
                },
            ))
            source_index += 1
            continue

        if media_type in {"explanation", "mixed_explanation"}:
            result = explanation_results.get(shot.shot_id)
            if not result or result.get("status") != "succeeded":
                raise EditingLayerError(f"说明镜头 Remotion 素材未成功：{shot.shot_id}")
            path = Path(str(result.get("output_path") or result.get("video_path") or ""))
            prepared, actual, padded = _prepared_video(path, prepared_root / "videos", shot.timeline.duration_us)
            requirement = _requirement_for_shot(shot, requirements, ".video.aigc")
            records.append(_record(
                asset_id=f"asset-{shot.shot_id}-explanation-video",
                requirement=requirement,
                asset_type="video",
                role="explanation_video",
                source_index=source_index,
                path=prepared,
                duration_us=actual,
                actual_timeline=shot.timeline,
                asset_version=version,
                metadata={
                    "target_shot_id": shot.shot_id,
                    "resolved_media_type": media_type,
                    "original_media_type": original_media_type,
                    "route_source": "asset_result.explanation_assets",
                    "renderer": result.get("renderer", "remotion"),
                    "template_id": result.get("template_id", ""),
                    "style_lock_id": result.get("style_lock_id", ""),
                    "padded_to_shot": padded,
                },
            ))
            source_index += 1
            continue

        frame_path = first_frame_by_shot[shot.shot_id]
        if not frame_path.is_file():
            raise EditingLayerError(f"首帧文件不存在：{frame_path}")
        try:
            image_requirement = _requirement_for_shot(shot, requirements, ".image.first_frame")
        except EditingLayerError:
            # 数字人鉴权失败后，编导锁定的 requirement 仍是数字人视频；
            # 这里保留原 requirement_id 作为稳定关联键，只把实际媒体改为图片。
            image_requirement = _requirement_for_shot(shot, requirements, ".video.digital_human")
        if media_type == "static_image":
            # 低动态镜头的首帧就是最终主画面，不再重复写入“首帧参考”轨。
            records.append(_record(
                asset_id=f"asset-{shot.shot_id}-static-image",
                requirement=image_requirement,
                asset_type="image",
                role="primary_visual_image",
                source_index=source_index,
                path=frame_path,
                duration_us=shot.timeline.duration_us,
                actual_timeline=shot.timeline,
                asset_version=version,
                metadata={
                    "target_shot_id": shot.shot_id,
                    "resolved_media_type": media_type,
                    "original_media_type": original_media_type,
                    "route_source": "asset_result.media_type_overrides" if shot.shot_id in media_type_overrides else "shot_video_script",
                    "director_requirement_route": image_requirement.route,
                    "digital_human_fallback": shot.shot_id in digital_fallbacks,
                    "fallback_source_grid_id": (digital_fallbacks.get(shot.shot_id) or {}).get("source_grid_id", "") if isinstance(digital_fallbacks.get(shot.shot_id), Mapping) else "",
                },
            ))
            source_index += 1
            continue

        records.append(_record(
            asset_id=f"asset-{shot.shot_id}-first-frame",
            requirement=image_requirement,
            asset_type="image",
            role="first_frame",
            source_index=source_index,
            path=frame_path,
            duration_us=shot.timeline.duration_us,
            actual_timeline=shot.timeline,
            asset_version=version,
            metadata={
                "target_shot_id": shot.shot_id,
                "resolved_media_type": media_type,
            "route_source": "asset_result.media_type_overrides" if shot.shot_id in media_type_overrides else "shot_video_script",
            },
        ))
        source_index += 1

        result = aigc_results.get(shot.shot_id)
        if not result or result.get("status") != "succeeded":
            raise EditingLayerError(f"AIGC 视频素材未成功：{shot.shot_id}")
        path = Path(str(result.get("video_path") or ""))
        prepared, actual, padded = _prepared_video(path, prepared_root / "videos", shot.timeline.duration_us)
        video_requirement = _requirement_for_shot(shot, requirements, ".video.aigc")
        records.append(_record(
            asset_id=f"asset-{shot.shot_id}-aigc-video",
            requirement=video_requirement,
            asset_type="video",
            role="primary_visual",
            source_index=source_index,
            path=prepared,
            duration_us=actual,
            actual_timeline=shot.timeline,
            asset_version=version,
            metadata={
                "target_shot_id": shot.shot_id,
                "resolved_media_type": media_type,
                "route_source": "shot_video_script",
                "provider_task_id": result.get("task_id", ""),
                "padded_to_shot": padded,
            },
        ))
        source_index += 1

    narration_by_group = {
        str(item.get("group_id")): item
        for item in director.narration_assets
        if isinstance(item, Mapping)
    }
    narration_paths: dict[str, Path] = {}
    narration_padding: dict[str, bool] = {}
    for group in director.groups:
        narration = narration_by_group.get(group.group_id)
        if not narration:
            raise EditingLayerError(f"缺少 {group.group_id} 的 TTS 音频登记")
        path = Path(str(narration.get("local_path") or ""))
        prepared, actual, padded = _prepared_audio(path, prepared_root / "audios", group.timeline.duration_us)
        requirement = requirements[group.narration_requirement_id]
        records.append(_record(
            asset_id=f"asset-{group.group_id}-narration",
            requirement=requirement,
            asset_type="audio",
            role="narration",
            source_index=source_index,
            path=prepared,
            duration_us=actual,
            actual_timeline=group.timeline,
            asset_version=version,
            metadata={"resolved_media_type": "narration", "route_source": "director_lock.narration_assets", "padded_to_group": padded},
        ))
        narration_paths[group.group_id] = prepared
        narration_padding[group.group_id] = padded
        source_index += 1

    bgm = asset_result.get("bgm_assets") or {}
    bgm_source = Path(str(bgm.get("audio_path") or ""))
    bgm_requirement = requirements.get("project.audio.bgm")
    if not bgm_requirement:
        raise EditingLayerError("编导锁定数据缺少 project.audio.bgm requirement")
    bgm_windows = _bgm_segment_windows(director)
    bgm_segments, bgm_source_duration_us = _prepared_bgm_segments(
        bgm_source, prepared_root / "audios", bgm_windows
    )
    bgm_segment_report: list[dict[str, Any]] = []
    for segment_index, (bgm_path, bgm_duration, bgm_window, looped) in enumerate(bgm_segments, start=1):
        records.append(_record(
            asset_id=f"asset-project-bgm-{segment_index:02d}",
            requirement=bgm_requirement,
            asset_type="audio",
            role="background_music",
            source_index=source_index,
            path=bgm_path,
            duration_us=bgm_duration,
            actual_timeline=bgm_window,
            asset_version=version,
            metadata={
                "resolved_media_type": "background_music",
                "route_source": "asset_production.bgm_assets",
                "source_type": bgm.get("source_type", ""),
                "segment_index": segment_index,
                "segment_count": len(bgm_segments),
                "splice_boundary": segment_index < len(bgm_segments),
                "looped_to_timeline": looped,
                "padded_to_timeline": False,
                "source_duration_us": bgm_source_duration_us,
            },
        ))
        bgm_segment_report.append({
            "segment_index": segment_index,
            "start_us": bgm_window.start_us,
            "end_us": bgm_window.end_us,
            "duration_us": bgm_window.duration_us,
            "actual_duration_us": bgm_duration,
            "path": str(bgm_path),
            "looped_to_timeline": looped,
        })
        source_index += 1

    # v3.2 独立音效资产：仅登记 sound_effect_production 输出，不触碰视觉/TTS/BGM。
    sound_effects = asset_result.get("sound_effect_assets") or asset_result.get("sound_effect_results") or {}
    if isinstance(sound_effects, Mapping):
        sound_rows = sound_effects.get("shot_results") or []
    else:
        sound_rows = sound_effects if isinstance(sound_effects, list) else []
    req_by_shot = {sid: req for req in requirements.values() if req.role in {"sound_effect", "音效"} for sid in req.shot_ids}
    shot_map = {shot.shot_id: shot for shot in director.shots}
    for row in sound_rows:
        if not isinstance(row, Mapping):
            continue
        sid = str(row.get("shot_id") or "")
        req = req_by_shot.get(sid)
        raw_path = str(row.get("local_path") or "").strip()
        path = Path(raw_path) if raw_path else None
        remote_url = str(row.get("audio_url") or row.get("url") or "").strip()
        has_local = bool(path and path.is_file())
        has_remote = remote_url.startswith(("http://", "https://"))
        if not req or str(row.get("status") or "").lower() != "succeeded" or (not has_local and not has_remote):
            continue
        cue = TimeWindow(int(row.get("cue_start_us", shot_map[sid].timeline.start_us)), int(row.get("cue_end_us", shot_map[sid].timeline.end_us)))
        actual = int(row.get("duration_us") or (_duration_us(path) if path else 0))
        if actual <= 0:
            continue
        records.append(_record(asset_id=f"asset-{sid}-sound-effect", requirement=req, asset_type="audio", role="sound_effect", source_index=source_index, path=path, remote_url=remote_url, duration_us=actual, actual_timeline=cue, asset_version=version, metadata={"resolved_media_type": "sound_effect", "source": "sound_effect_production", "volume": row.get("volume", 1.0), "content_hash": row.get("content_hash", "")}))
        source_index += 1

    registry = AssetRegistry(
        project_id=director.project_id,
        run_id=director.run_id,
        source_plan_version=director.plan_version,
        asset_version=version,
        records=records,
        status="ASSETS_READY",
        mapping_notes=[
            "实际媒体路由以 shot_video_script.media_type 为准；未改写 director_lock。",
            "数字人视频与 AIGC 视频共用 AIGC动画主画面轨，不单独创建数字人轨。",
            "static_image 作为低动态镜头主画面写入，不重复生成视频。",
            "数字人镜头不写入首帧参考；AIGC 镜头写入首帧参考；静态图片首帧直接作为主画面。",
            "所有剪辑时间线均使用微秒；BGM 按编导大分段边界写入连续非静音分段，不使用静音 apad 补齐。",
        ],
        digital_human_policy={
            "selected_shot_ids": [shot.shot_id for shot in director.shots if script_by_shot[shot.shot_id].get("media_type") == "digital_human_video"],
            "max_ratio": director.digital_human_max_shot_ratio,
            "execution_policy": "serial_with_reuse",
        },
    )
    report = {
        "source_of_truth": "shot_video_script.media_type",
        "timeline_unit": "microseconds",
        "route_trace": route_trace,
        "narration_padding": narration_padding,
        "bgm_padded_to_timeline": False,
        "bgm_looped_to_timeline": any(item["looped_to_timeline"] for item in bgm_segment_report),
        "bgm_segment_count": len(bgm_segment_report),
        "bgm_segments": bgm_segment_report,
        "asset_record_counts": {
            "total": len(records),
            "video": sum(item.asset_type == "video" for item in records),
            "image": sum(item.asset_type == "image" for item in records),
            "audio": sum(item.asset_type == "audio" for item in records),
            "first_frame_reference": sum(item.role == "first_frame" for item in records),
            "primary_visual": sum(item.role in {"primary_visual", "primary_visual_image", "digital_human"} for item in records),
        },
        "resolved_media_type_counts": {
            media_type: sum(1 for item in shot_video_script if item.get("media_type") == media_type)
            for media_type in sorted({str(item.get("media_type")) for item in shot_video_script})
        },
        "digital_human_shot_ids": [shot.shot_id for shot in director.shots if script_by_shot[shot.shot_id].get("media_type") == "digital_human_video"],
        "static_image_shot_ids": [shot.shot_id for shot in director.shots if script_by_shot[shot.shot_id].get("media_type") == "static_image"],
    }
    return registry, report


__all__ = ["build_live_asset_registry", "director_from_lock"]
