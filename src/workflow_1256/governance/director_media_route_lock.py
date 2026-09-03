"""把电影化编导结果冻结为逐镜素材路由。

这个模块位于 ``DirectorLockedManifest`` 与素材生产之间。它不重新分镜、不
修改 TTS 微秒时间线，也不写入供应商 prompt 或 URL；唯一职责是为每个已经
锁定的镜头选择唯一媒体类型，并明确下游必须创建或跳过的素材。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from workflow_1256.cinematic_storyboard_governance import (
    EMPTY_SHOT_DESIGN_TYPES,
    STATIC_IMAGE_LOW_MOTION_MAX_DURATION_US,
    _is_low_motion_shot,
    resolve_cinematic_media_routes,
)


MEDIA_TYPES = ("digital_human_video", "aigc_video", "static_image")
MEDIA_TYPE_LABELS = {
    "digital_human_video": "数字人视频",
    "aigc_video": "AIGC 视频",
    "static_image": "静态图片",
}
SHORT_SHOT_STATIC_IMAGE_THRESHOLD_US = 3_000_000
# 保留旧常量名，避免已有调用方导入失败；语义已升级为“低于 3 秒”。
STATIC_IMAGE_MAX_SHORT_DURATION_US = SHORT_SHOT_STATIC_IMAGE_THRESHOLD_US


class DirectorMediaRouteLockError(ValueError):
    """逐镜媒体分类不能安全进入素材生产。"""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DirectorMediaRouteLockError(f"{field} 必须是对象")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise DirectorMediaRouteLockError(f"{field} 必须是数组")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DirectorMediaRouteLockError(f"{field} 必须是非空字符串")
    return value.strip()


def _window(value: Any, field: str) -> dict[str, int]:
    raw = _mapping(value, field)
    start = raw.get("start_us", raw.get("start"))
    end = raw.get("end_us", raw.get("end"))
    if isinstance(start, bool) or not isinstance(start, int):
        raise DirectorMediaRouteLockError(f"{field}.start_us 必须是整数微秒")
    if isinstance(end, bool) or not isinstance(end, int) or end <= start:
        raise DirectorMediaRouteLockError(f"{field}.end_us 必须大于 start_us")
    return {"start_us": start, "end_us": end, "duration_us": end - start}


def _static_image_allowed(context: Mapping[str, Any], timeline: Mapping[str, int]) -> bool:
    """图片镜头只能承载低动态表达，短切镜头可优先采用静态图片。"""

    duration_us = timeline["duration_us"]
    # 短于 3 秒是硬路由规则：静态图片不执行实际运镜，因此即使模型残留了
    # “推近/跟拍”等视觉字段，也不能因为这个字段把镜头退回 AIGC 或使整条链路失败。
    if duration_us < SHORT_SHOT_STATIC_IMAGE_THRESHOLD_US:
        return True
    visual = _mapping(context.get("visual_direction"), "shot_context.visual_direction")
    motion = _text(visual.get("camera_motion"), "shot_context.visual_direction.camera_motion")
    if motion != "固定":
        return False
    # 必须与 resolve_cinematic_media_routes 使用同一套低动势标准：图片镜头由
    # 固定机位、无连续运动信号和叙事职责共同决定，不能只看时长。
    return duration_us <= STATIC_IMAGE_LOW_MOTION_MAX_DURATION_US and _is_low_motion_shot(
        {"clip_role": context.get("narrative_job")}, {}, context,
    )


def _route_requirements(media_type: str) -> list[dict[str, str]]:
    if media_type == "digital_human_video":
        return [{"asset": "digital_human_video", "action": "create", "source": "数字人"}]
    if media_type == "static_image":
        return [{"asset": "first_frame_image", "action": "create", "source": "首帧生成"}]
    return [
        {"asset": "first_frame_image", "action": "create", "source": "首帧生成"},
        {"asset": "aigc_video", "action": "create", "source": "AIGC动画"},
    ]


def _skip_actions(media_type: str) -> dict[str, bool]:
    return {
        "skip_first_frame": media_type == "digital_human_video",
        "skip_aigc_video": media_type in {"digital_human_video", "static_image"},
        "skip_video_prompt": media_type in {"digital_human_video", "static_image"},
    }


def build_director_media_route_lock(
    director_lock: Mapping[str, Any], governance: Mapping[str, Any],
) -> dict[str, Any]:
    """构建只读的逐镜媒体路由锁。

    ``media_type`` 由电影化编导治理输出；本函数只校验类型选择是否符合数字人
    坑位和静态图片低动态约束，然后声明下游应创建、跳过和写入哪条剪辑轨道。
    """

    lock = _mapping(director_lock, "director_lock")
    if lock.get("status") != "DIRECTOR_LOCKED":
        raise DirectorMediaRouteLockError("媒体路由只接受 DIRECTOR_LOCKED 的编导清单")
    shots = _list(lock.get("shots"), "director_lock.shots")
    contexts = _list(governance.get("shot_contexts"), "director_governance.shot_contexts")
    if not shots or len(shots) != len(contexts):
        raise DirectorMediaRouteLockError("媒体路由必须与冻结镜头逐条一一对应")
    planned_routes = resolve_cinematic_media_routes(lock, governance)["by_shot"]

    routes: list[dict[str, Any]] = []
    for index, (raw_shot, raw_context) in enumerate(zip(shots, contexts, strict=True)):
        shot = _mapping(raw_shot, f"director_lock.shots[{index}]")
        context = _mapping(raw_context, f"director_governance.shot_contexts[{index}]")
        shot_id = _text(shot.get("shot_id"), f"director_lock.shots[{index}].shot_id")
        if _text(context.get("shot_id"), f"shot_contexts[{index}].shot_id") != shot_id:
            raise DirectorMediaRouteLockError("媒体路由上下文 shot_id 与冻结镜头顺序不一致")
        timeline = _window(shot.get("timeline"), f"{shot_id}.timeline")
        context_timeline = _window(context.get("timeline"), f"{shot_id}.context.timeline")
        if timeline != context_timeline:
            raise DirectorMediaRouteLockError(f"{shot_id} 的媒体路由不得改写冻结时间线")
        media_type = _text(context.get("media_type"), f"{shot_id}.media_type")
        if media_type not in MEDIA_TYPES:
            raise DirectorMediaRouteLockError(f"{shot_id}.media_type 只能是 {', '.join(MEDIA_TYPES)}")
        if context.get("shot_design_type") in EMPTY_SHOT_DESIGN_TYPES and media_type == "digital_human_video":
            raise DirectorMediaRouteLockError(f"{shot_id} 空镜设计不得使用数字人视频")
        media_reason = _text(context.get("media_reason"), f"{shot_id}.media_reason")
        if index == 0 and media_type != "aigc_video":
            raise DirectorMediaRouteLockError("全片第一个镜头必须使用 AIGC 视频，不得使用数字人或静态图片")
        if media_type == "static_image" and not _static_image_allowed(context, timeline):
            raise DirectorMediaRouteLockError(
                f"{shot_id} 静态图片仅允许固定机位的低动势镜头；"
                "低于 3 秒的短切镜头必须使用图片"
            )
        planned_media_type = planned_routes[shot_id]["media_type"]
        if media_type != planned_media_type:
            if planned_media_type == "digital_human_video":
                raise DirectorMediaRouteLockError(f"{shot_id} 已锁定为数字人坑位，不能改走 {MEDIA_TYPE_LABELS[media_type]}")
            raise DirectorMediaRouteLockError(f"{shot_id} 已锁定为 {MEDIA_TYPE_LABELS[planned_media_type]}，不能改走 {MEDIA_TYPE_LABELS[media_type]}")
        routes.append({
            "shot_id": shot_id,
            "group_id": _text(shot.get("group_id"), f"{shot_id}.group_id"),
            "timeline": timeline,
            "media_type": media_type,
            "media_label": MEDIA_TYPE_LABELS[media_type],
            "media_reason": media_reason,
            "media_selection_score": dict(planned_routes[shot_id].get("media_selection_score") or {}),
            "required_assets": _route_requirements(media_type),
            "skip": _skip_actions(media_type),
            # 三种媒体都必须占用本镜完整坑位；数字人与 AIGC 视频在剪映共用主视觉轨。
            "editing_track": "main_visual",
            "coverage_policy": "fit_to_locked_shot_timeline",
        })

    counts = Counter(route["media_type"] for route in routes)
    return {
        "schema_version": "director-media-route-lock-v1",
        "status": "DIRECTOR_MEDIA_LOCKED",
        "source_contract": {
            "director_lock_status": "DIRECTOR_LOCKED",
            "director_lock_mutated": False,
            "timelines_preserved": True,
            "shot_ids": [route["shot_id"] for route in routes],
        },
        "routes": routes,
        "summary": {
            "total_shot_count": len(routes),
            "digital_human_video_count": counts["digital_human_video"],
            "aigc_video_count": counts["aigc_video"],
            "static_image_count": counts["static_image"],
            "static_short_shot_threshold_us": STATIC_IMAGE_MAX_SHORT_DURATION_US,
            "short_shot_static_image_threshold_us": SHORT_SHOT_STATIC_IMAGE_THRESHOLD_US,
        },
    }


__all__ = [
    "DirectorMediaRouteLockError",
    "MEDIA_TYPE_LABELS",
    "MEDIA_TYPES",
    "STATIC_IMAGE_MAX_SHORT_DURATION_US",
    "SHORT_SHOT_STATIC_IMAGE_THRESHOLD_US",
    "build_director_media_route_lock",
]
