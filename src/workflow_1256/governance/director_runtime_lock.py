"""将一次编导执行的已完成子结果锁定为素材层可消费的清单。

本模块位于原“具体画面导演”之后：只读取该节点、TTS、字幕细分和镜头
细化的输出，不调用模型，也不修改这些原始输出。全部时间使用整数微秒。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import (
    CaptionRecord,
    DirectorGroup,
    DirectorLockedManifest,
    DirectorShot,
    GovernanceContractError,
    MaterialRequirement,
    TimeWindow,
)
from .director_convergence import (
    DirectorConvergenceError,
    DirectorVisualSpec,
    _assert_no_material_keys,
)
from ..story_writer import normalize_cinematic_story


class DirectorRuntimeLockError(GovernanceContractError):
    """一次新运行的编导子结果无法收口时抛出。"""


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise DirectorRuntimeLockError(f"{field} 必须是数组")
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DirectorRuntimeLockError(f"{field} 必须是对象")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DirectorRuntimeLockError(f"{field} 必须是非空字符串")
    return value


def _window(value: Any, field: str) -> TimeWindow:
    item = _mapping(value, field)
    start, end = item.get("start"), item.get("end")
    if isinstance(start, bool) or not isinstance(start, int):
        raise DirectorRuntimeLockError(f"{field}.start 必须是整数微秒")
    if isinstance(end, bool) or not isinstance(end, int):
        raise DirectorRuntimeLockError(f"{field}.end 必须是整数微秒")
    return TimeWindow(start, end)


def _routes(beat: Mapping[str, Any], field: str) -> list[str]:
    routes: list[str] = []
    for beat_index, item in enumerate(_list(beat.get("beats", []), f"{field}.beats")):
        detail = _mapping(item, f"{field}.beats[{beat_index}]")
        for route in _list(detail.get("route_candidates", []), f"{field}.beats[{beat_index}].route_candidates"):
            routes.append(_text(route, f"{field}.beats[{beat_index}].route_candidates"))
    return list(dict.fromkeys(routes)) or ["scene"]


def _selected_route(shot: Mapping[str, Any], routes: Sequence[str], field: str) -> str | None:
    """候选路线不改变生产；只有明确选中的数字人路线才独占镜头坑位。"""

    value = shot.get("selected_route")
    if value is None or value == "":
        return None
    raw = _text(value, f"{field}.selected_route").strip().lower()
    aliases = {"host": "digital_human", "digitalhuman": "digital_human", "infinitetalk": "digital_human", "数字人": "digital_human"}
    selected = aliases.get(raw, raw)
    if selected not in routes:
        raise DirectorRuntimeLockError(
            f"{field}.selected_route={selected} 不在 route_candidates 中"
        )
    return selected


def _group_for_window(groups: Sequence[DirectorGroup], window: TimeWindow) -> DirectorGroup:
    matches = [group for group in groups if group.timeline.contains(window)]
    if len(matches) != 1:
        raise DirectorRuntimeLockError("字幕时间线必须且只能归属一个大镜头")
    return matches[0]


def _duration_guidance(window: TimeWindow) -> dict[str, Any]:
    """记录软约束命中情况，不改写已由 TTS 锁定的真实时间线。"""

    if window.duration_us < 2_000_000:
        status = "shorter_than_target"
    elif window.duration_us > 5_000_000:
        status = "longer_than_target"
    else:
        status = "within_target"
    return {
        "policy": "soft_prefer_2_to_5_seconds",
        "preferred_min_us": 2_000_000,
        "preferred_max_us": 5_000_000,
        "short_shot_static_image_threshold_us": 3_000_000,
        "actual_duration_us": window.duration_us,
        "status": status,
    }


def _canonical_narration_assets(value: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for index, raw in enumerate(value or []):
        item = _mapping(raw, f"narration_assets[{index}]")
        timeline = _mapping(item.get("timeline"), f"narration_assets[{index}].timeline")
        start = timeline.get("start_us", timeline.get("start"))
        end = timeline.get("end_us", timeline.get("end"))
        if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int):
            raise DirectorRuntimeLockError(f"narration_assets[{index}].timeline 必须使用整数微秒")
        if end <= start:
            raise DirectorRuntimeLockError(f"narration_assets[{index}].timeline 必须满足 end > start")
        canonical = dict(item)
        canonical["timeline"] = {
            "start_us": start,
            "end_us": end,
            "duration_us": end - start,
        }
        assets.append(canonical)
    return assets


def build_director_locked_manifest(
    *,
    director_output: Mapping[str, Any],
    tts_group_timelines: Sequence[Mapping[str, Any]],
    total_timeline: Mapping[str, Any],
    shot_groups: Sequence[Mapping[str, Any]],
    caption_segments: Sequence[str],
    caption_timelines: Sequence[Mapping[str, Any]],
    project_id: str,
    run_id: str,
    plan_version: str,
    tts_fingerprint: str,
    visual_specs: Sequence[Mapping[str, Any] | None] | None = None,
    cinematic_story: Mapping[str, Any] | None = None,
    story_review_status: str = "PENDING_REVIEW",
    story_revision: int = 1,
    narration_assets: Sequence[Mapping[str, Any]] | None = None,
    subtitle_pipeline: Mapping[str, Any] | None = None,
    sound_effect_plan: Sequence[Mapping[str, Any]] | None = None,
    digital_human_max_shot_ratio: float = 0.15,
) -> DirectorLockedManifest:
    """收口新运行的编导数据，保留上游数组顺序并拒绝不完整关联。"""

    output = _mapping(director_output, "director_output")
    segments = _list(output.get("segments"), "director_output.segments")
    beats = _list(output.get("segment_beats"), "director_output.segment_beats")
    timelines = _list(list(tts_group_timelines), "tts_group_timelines")
    code_groups = _list(list(shot_groups), "shot_groups")
    captions_text = _list(list(caption_segments), "caption_segments")
    captions_timing = _list(list(caption_timelines), "caption_timelines")
    if not segments or len(segments) != len(beats) or len(segments) != len(timelines) or len(segments) != len(code_groups):
        raise DirectorRuntimeLockError("编导分段、segment_beats、TTS 时间线和镜头细化分组必须一一对应")
    if len(captions_text) != len(captions_timing):
        raise DirectorRuntimeLockError("字幕文本和字幕时间线必须一一对应")
    if story_review_status != "APPROVED":
        raise DirectorRuntimeLockError("只有用户明确同意故事后，才能生成 DirectorLockedManifest")
    if isinstance(story_revision, bool) or not isinstance(story_revision, int) or story_revision < 1:
        raise DirectorRuntimeLockError("story_revision 必须是正整数")
    if cinematic_story is None:
        raise DirectorRuntimeLockError("必须先生成完整电影故事并经用户审核，才能生成 DirectorLockedManifest")
    story = None
    story_by_group: dict[str, Mapping[str, Any]] = {}
    scene_by_group: dict[str, Mapping[str, Any]] = {}
    if cinematic_story is not None:
        try:
            story = normalize_cinematic_story(cinematic_story, segments)
        except ValueError as exc:
            raise DirectorRuntimeLockError(str(exc)) from exc
        story_by_group = {item["group_id"]: item for item in story["narration_mappings"]}
        for scene in story["scene_groups"]:
            for group_id in scene["group_ids"]:
                scene_by_group[group_id] = scene

    total = _window(total_timeline, "total_timeline")
    group_windows = [_window(item, f"tts_group_timelines[{index}]") for index, item in enumerate(timelines)]
    all_shots: list[DirectorShot] = []
    group_shot_ids: dict[str, list[str]] = {}
    visual_values = list(visual_specs) if visual_specs is not None else []
    visual_index = 0

    for group_index, (segment, raw_beat, raw_group, group_window) in enumerate(
        zip(segments, beats, code_groups, group_windows, strict=True)
    ):
        segment_text = _text(segment, f"director_output.segments[{group_index}]")
        beat = _mapping(raw_beat, f"director_output.segment_beats[{group_index}]")
        if beat.get("segment_index") != group_index:
            raise DirectorRuntimeLockError(f"segment_beats[{group_index}].segment_index 必须等于数组下标")
        if beat.get("segment_text") != segment_text:
            raise DirectorRuntimeLockError(f"segment_beats[{group_index}].segment_text 必须与 segments 同位一致")
        group_id = f"g{group_index + 1:02d}"
        group = _mapping(raw_group, f"shot_groups[{group_index}]")
        raw_shots = _list(group.get("shots"), f"shot_groups[{group_index}].shots")
        raw_timelines = _list(group.get("timelines"), f"shot_groups[{group_index}].timelines")
        if not raw_shots or len(raw_shots) != len(raw_timelines):
            raise DirectorRuntimeLockError(f"{group_id} 的小镜头和时间线必须非空且一一对应")
        group_shot_ids[group_id] = []
        routes = _routes(beat, f"director_output.segment_beats[{group_index}]")
        for shot_index, (raw_shot, raw_timeline) in enumerate(zip(raw_shots, raw_timelines, strict=True)):
            shot = _mapping(raw_shot, f"shot_groups[{group_index}].shots[{shot_index}]")
            shot_id = f"{group_id}_s{shot_index + 1:02d}"
            group_shot_ids[group_id].append(shot_id)
            shot_window = _window(raw_timeline, f"{shot_id}.timeline")
            production_spec: dict[str, Any] = {"duration_guidance": _duration_guidance(shot_window)}
            # 全片首镜是编排锚点：无论 STT 坑位多短，都必须进入 AIGC 路线。
            # 连续静态纠偏可把少数短坑位提升为 AIGC；其余短镜仍走静态首帧。
            is_first_shot = len(all_shots) == 0
            pacing_value = shot.get("media_pacing")
            pacing = dict(pacing_value) if isinstance(pacing_value, Mapping) else {}
            force_aigc = bool(pacing.get("force_aigc"))
            short_shot_static_image = (
                shot_window.duration_us < 3_000_000 and not is_first_shot
            )
            production_spec["short_shot_static_image"] = short_shot_static_image
            production_spec["first_shot_must_be_aigc"] = is_first_shot
            if pacing:
                production_spec["media_pacing"] = pacing
            narration_text = shot.get("narration_text")
            if narration_text is not None:
                production_spec["narration_text"] = _text(narration_text, f"{shot_id}.narration_text")
            # v3.2 音效主体是上游明确提供的可选元数据；只透传非空值，
            # 不从画面文字或媒体类型猜测，避免未经确认触发生成。
            sound_effect_subject = shot.get("sound_effect_subject")
            if isinstance(sound_effect_subject, str) and sound_effect_subject.strip():
                production_spec["sound_effect_subject"] = sound_effect_subject.strip()
            if group_id in story_by_group:
                production_spec["story_mapping"] = dict(story_by_group[group_id])
            if visual_index < len(visual_values) and visual_values[visual_index] is not None:
                visual = _mapping(visual_values[visual_index], f"visual_specs[{visual_index}]")
                try:
                    _assert_no_material_keys(visual, f"visual_specs[{visual_index}]")
                except DirectorConvergenceError as exc:
                    raise DirectorRuntimeLockError(str(exc)) from exc
                production_spec["visual_brief"] = dict(visual)
            visual_index += 1
            selected_route = _selected_route(shot, routes, shot_id)
            if is_first_shot:
                requirement_ids = [f"{shot_id}.video.aigc", f"{shot_id}.image.first_frame", f"{shot_id}.prompt.video"]
            elif selected_route == "digital_human" and not short_shot_static_image:
                requirement_ids = [f"{shot_id}.video.digital_human"]
                production_spec["selected_route"] = "digital_human"
            elif short_shot_static_image:
                # 低于 3 秒的镜头统一由静态首帧承载，不创建 AIGC 或数字人视频任务。
                requirement_ids = [f"{shot_id}.image.first_frame"]
            else:
                requirement_ids = [f"{shot_id}.video.aigc", f"{shot_id}.image.first_frame", f"{shot_id}.prompt.video"]
            all_shots.append(DirectorShot(
                shot_id=shot_id, group_id=group_id, shot_index=shot_index,
                source_text=_text(shot.get("source_text"), f"{shot_id}.source_text"),
                clip_role=_text(shot.get("clip_role"), f"{shot_id}.clip_role"),
                story_beat=_text(shot.get("story_beat"), f"{shot_id}.story_beat"),
                timeline=shot_window,
                route_candidates=routes, requirement_ids=requirement_ids,
                production_spec=production_spec,
            ))
    if visual_values and visual_index != len(visual_values):
        raise DirectorRuntimeLockError("visual_specs 数量必须与小镜头数量一一对应")

    provisional_groups = [DirectorGroup(
        group_id=f"g{index + 1:02d}", group_index=index,
        segment_text=_text(segments[index], f"director_output.segments[{index}]"),
        timeline=group_windows[index], shot_ids=group_shot_ids[f"g{index + 1:02d}"],
        caption_ids=[], narration_requirement_id=f"g{index + 1:02d}.audio.narration",
    ) for index in range(len(segments))]
    captions: list[CaptionRecord] = []
    captions_by_group = {group.group_id: [] for group in provisional_groups}
    for index, (text, raw_timeline) in enumerate(zip(captions_text, captions_timing, strict=True)):
        window = _window(raw_timeline, f"caption_timelines[{index}]")
        group = _group_for_window(provisional_groups, window)
        caption_id = f"caption_{index + 1:03d}"
        captions_by_group[group.group_id].append(caption_id)
        captions.append(CaptionRecord(
            caption_id,
            group.group_id,
            _text(text, f"caption_segments[{index}]"),
            window,
            source_node="capcut_stt",
        ))

    groups = [DirectorGroup(
        group_id=group.group_id, group_index=group.group_index, segment_text=group.segment_text,
        timeline=group.timeline, shot_ids=group.shot_ids, caption_ids=captions_by_group[group.group_id],
        narration_requirement_id=group.narration_requirement_id,
        metadata={"rhythm": _mapping(beats[group.group_index], "segment_beat").get("rhythm", ""),
                  "segment_goal": _mapping(beats[group.group_index], "segment_beat").get("segment_goal", ""),
                  **({"story_scene": dict(scene_by_group[group.group_id]), "story_mapping": dict(story_by_group[group.group_id])}
                     if group.group_id in story_by_group else {})},
    ) for group in provisional_groups]
    requirements: list[MaterialRequirement] = [MaterialRequirement(
        "project.audio.bgm", "project", "audio", "background_music", None, [], False,
        "background_music", "cover_project", "背景音乐*", total,
    )]
    if story:
        requirements.append(MaterialRequirement(
            "project.image.character_anchor", "project", "image", "character_anchor", None,
            [], True, "角色锚定图", "style_and_identity_reference", "角色锚定图", total,
            {"character_anchor": story["movie_outline"]["protagonist"], "generate_count": 1},
        ))
    for group in groups:
        requirements.extend([
            MaterialRequirement(f"{group.group_id}.audio.narration", "group", "audio", "narration", group.group_id, group.shot_ids, True, "speech_synthesis", "cover_group", "speech_synthesis", group.timeline),
            MaterialRequirement(f"{group.group_id}.caption.track", "group", "caption", "subtitle_track", group.group_id, group.shot_ids, True, "capcut_stt", "overlap_by_timeline", "capcut_stt", group.timeline, {"caption_count": len(group.caption_ids), "label": "剪映 STT 字幕"}),
        ])
    for shot in all_shots:
        short_shot_static_image = bool(shot.production_spec.get("short_shot_static_image"))
        if shot.production_spec.get("first_shot_must_be_aigc"):
            requirements.extend([
                MaterialRequirement(f"{shot.shot_id}.video.aigc", "shot", "video", "primary_visual", shot.group_id, [shot.shot_id], True, "AIGC动画", "fit_to_shot", "AIGC动画*", shot.timeline),
                MaterialRequirement(f"{shot.shot_id}.image.first_frame", "shot", "image", "first_frame", shot.group_id, [shot.shot_id], True, "首帧生成", "reference_for_shot", "首帧生成", shot.timeline),
                MaterialRequirement(f"{shot.shot_id}.prompt.video", "shot", "prompt", "video_generation_prompt", shot.group_id, [shot.shot_id], True, "提示词生成", "one_prompt_plan_per_shot", "提示词生成", shot.timeline),
            ])
            continue
        if shot.production_spec.get("selected_route") == "digital_human" and not short_shot_static_image:
            requirements.append(MaterialRequirement(
                f"{shot.shot_id}.video.digital_human", "shot", "video", "digital_human",
                shot.group_id, [shot.shot_id], True, "数字人", "fit_to_shot", "数字人*", shot.timeline,
                {"exclusive_shot_route": True, "skip_first_frame": True, "skip_aigc_video": True},
            ))
            continue
        if short_shot_static_image:
            requirements.append(MaterialRequirement(
                f"{shot.shot_id}.image.first_frame", "shot", "image", "first_frame",
                shot.group_id, [shot.shot_id], True, "首帧生成", "fit_to_shot", "首帧生成", shot.timeline,
                {"short_shot_static_image": True, "skip_aigc_video": True, "skip_video_prompt": True},
            ))
            continue
        requirements.extend([
            MaterialRequirement(f"{shot.shot_id}.video.aigc", "shot", "video", "primary_visual", shot.group_id, [shot.shot_id], True, "AIGC动画", "fit_to_shot", "AIGC动画*", shot.timeline),
            MaterialRequirement(f"{shot.shot_id}.image.first_frame", "shot", "image", "first_frame", shot.group_id, [shot.shot_id], True, "首帧生成", "reference_for_shot", "首帧生成", shot.timeline),
            MaterialRequirement(f"{shot.shot_id}.prompt.video", "shot", "prompt", "video_generation_prompt", shot.group_id, [shot.shot_id], True, "提示词生成", "one_prompt_plan_per_shot", "提示词生成", shot.timeline),
        ])
    return DirectorLockedManifest(
        project_id=_text(project_id, "project_id"), run_id=_text(run_id, "run_id"),
        plan_version=_text(plan_version, "plan_version"), tts_fingerprint=_text(tts_fingerprint, "tts_fingerprint"),
        total_timeline=total, groups=groups, shots=all_shots, captions=captions,
        requirements=requirements,
        provenance=[
            {"ref_node": "129109", "path": "director_plan/segment_beats/segments"},
            {"ref_node": "165901", "path": "timelines/all_timelines"},
            {"source_node": "capcut_stt", "path": "TTS 音频 -> new_segments/new_timelines"},
            {"ref_node": "103964", "path": "Code_list"},
            *([{"source_node": "故事编写器", "path": "movie_outline/scene_groups/narration_mappings"}] if story else []),
        ],
        cinematic_story=dict(story or {}),
        story_review_status=story_review_status,
        story_revision=story_revision,
        narration_assets=_canonical_narration_assets(narration_assets),
        subtitle_pipeline=dict(subtitle_pipeline or {}),
        sound_effect_plan=[dict(item) for item in (sound_effect_plan or [])],
        digital_human_max_shot_ratio=digital_human_max_shot_ratio,
    )


__all__ = ["DirectorRuntimeLockError", "build_director_locked_manifest"]
