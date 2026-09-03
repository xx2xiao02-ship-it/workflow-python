"""1256 剪辑层：把正式三层数据写入剪映小助手的 Windows 草稿。

本模块有两个明确边界：

* ``compile_editing_layer`` 只读 ``DirectorLockedManifest`` 和 ``AssetRegistry``，
  编译轨道、素材、字幕和剪辑绑定，不产生外部副作用；
* ``write_windows_native_draft`` 只使用剪映小助手项目的
  ``pyJianYingDraft.DraftFolder.create_draft``。它从项目自带的 Windows 模板创建
  草稿，再写入本地素材，拒绝 Mac/iOS 模板、远程 URL 缺少本地文件以及未验收的
  特效/转场。

这里不把 ``CapCut Mate /create_draft`` 当作 Windows 原生创建器。那个 HTTP 路径
属于服务适配器，最终 Windows 草稿必须走本项目的 ``DraftFolder`` 创建方式。
时间单位统一为微秒；规划时间线、素材实际时长和剪辑写入时间线分别保留。
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import time
import uuid
import copy
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TRANSITION_POINT_TOLERANCE_US = 50_000


def _transition_point_enabled(point: Mapping[str, Any]) -> bool:
    """三级镜头默认直切；上游显式 enabled=true 时允许例外转场。"""

    if point.get("enabled") is True:
        return True
    if point.get("enabled") is False:
        return False
    level = str(
        point.get("classification_level")
        or point.get("transition_level")
        or ""
    ).strip().lower()
    if level in {"level_3", "tertiary", "third", "3"}:
        return False
    from_group = str(point.get("from_group_id") or "").strip()
    to_group = str(point.get("to_group_id") or "").strip()
    return not (from_group and to_group and from_group == to_group)


def _select_transition_segment(
    candidates: Sequence[Any],
    point: Mapping[str, Any],
) -> Any | None:
    """按上游接缝时间选择前置镜头片段；镜头 ID 仍是外层主定位。"""

    if not candidates:
        return None
    raw_at_us = point.get("at_us")
    try:
        at_us = int(round(float(raw_at_us)))
    except (TypeError, ValueError):
        return candidates[-1]
    if at_us < 0:
        return candidates[-1]
    return min(
        candidates,
        key=lambda segment: abs(
            int(getattr(getattr(segment, "target_timerange", None), "end", 0) or 0) - at_us
        ),
    )


_VISIBLE_TRACK_PRIORITY = {
    # 主画面轨道必须优先于首帧参考轨道。首帧参考只用于补位/参考，
    # 不应承接镜头转场、镜头级特效或关键帧。
    "AIGC动画": 400,
    "数字人": 390,
    "主角占位": 380,
    "主画面": 370,
    "图片": 360,
    "视频": 350,
    "首帧参考": 100,
}


def _select_primary_visual_segments(
    candidates: Sequence[Any],
    segment_track_names: Mapping[int, str] | None = None,
) -> list[Any]:
    """同一镜头同一时间只保留最上层的可见视觉片段。

    编导交接中可能同时存在首帧参考图和实际 AIGC/数字人视频。
    如果不做选择，按输入顺序会把转场和关键帧写到首帧参考这条底层轨道。
    没有轨道映射时保留原行为，兼容旧调用和单元测试。
    """

    if not candidates or not segment_track_names:
        return list(candidates)

    grouped: dict[tuple[int, int], list[tuple[int, Any]]] = defaultdict(list)
    for index, segment in enumerate(candidates):
        timerange = getattr(segment, "target_timerange", None)
        start = int(getattr(timerange, "start", 0) or 0)
        end = int(getattr(timerange, "end", start) or start)
        grouped[(start, end)].append((index, segment))

    selected: list[tuple[int, Any]] = []
    for entries in grouped.values():
        selected.append(
            max(
                entries,
                key=lambda entry: (
                    _VISIBLE_TRACK_PRIORITY.get(
                        str(segment_track_names.get(id(entry[1])) or ""),
                        200,
                    ),
                    entry[0],
                ),
            )
        )
    selected.sort(key=lambda entry: entry[0])
    return [segment for _, segment in selected]


def _apply_primary_image_zoom_keyframes(
    visual_items: Sequence[tuple[Mapping[str, Any], Any]],
    *,
    segment_track_names: Mapping[int, str] | None,
    keyframe_property: Any,
) -> dict[str, int]:
    """给最上层主画面图片统一写入全片段 1.0 -> 1.2 缩放关键帧。

    首帧参考轨道是占位/参考数据，即使素材类型是 image 也不能参与这条规则。
    包装模板可能已经给片段写过缩放关键帧，因此先清理同一片段原有的 X/Y
    缩放列表，再写入统一缩放的两个端点，避免多个缩放曲线叠加。
    """

    if not visual_items or keyframe_property is None:
        return {"candidate_count": 0, "applied_count": 0, "keyframe_count": 0}

    item_by_segment_id: dict[int, Mapping[str, Any]] = {}
    all_visual_segments: list[Any] = []
    for item, segment in visual_items:
        all_visual_segments.append(segment)
        item_by_segment_id[id(segment)] = item
    candidates: list[Any] = []
    for segment in _select_primary_visual_segments(all_visual_segments, segment_track_names):
        item = item_by_segment_id.get(id(segment), {})
        asset_type = str(item.get("asset_type") or "").strip().lower()
        track_name = str((segment_track_names or {}).get(id(segment)) or "").strip()
        if asset_type != "image" or track_name in {"", "首帧参考"}:
            continue
        candidates.append(segment)

    if not candidates:
        return {"candidate_count": 0, "applied_count": 0, "keyframe_count": 0}

    scale_properties = {
        getattr(keyframe_property, "scale_x", None),
        getattr(keyframe_property, "scale_y", None),
    }
    applied = 0
    for segment in candidates:
        common_keyframes = getattr(segment, "common_keyframes", None)
        if isinstance(common_keyframes, list):
            common_keyframes[:] = [
                keyframe_list
                for keyframe_list in common_keyframes
                if getattr(keyframe_list, "keyframe_property", None) not in scale_properties
            ]
        timerange = getattr(segment, "target_timerange", None)
        duration_us = max(1, int(getattr(timerange, "duration", 0) or 0))
        try:
            segment.add_keyframe(keyframe_property.uniform_scale, 0, 1.0)
            segment.add_keyframe(keyframe_property.uniform_scale, duration_us, 1.2)
        except (AttributeError, TypeError, ValueError):
            continue
        applied += 1
    return {
        "candidate_count": len(candidates),
        "applied_count": applied,
        "keyframe_count": applied * 2,
    }

from .governance.contracts import (
    AssetRecord,
    AssetRegistry,
    DirectorLockedManifest,
    EditBinding,
    EditManifest,
    GovernanceContractError,
    MaterialRequirement,
    TimeWindow,
    TIMELINE_UNIT,
)


class EditingLayerError(ValueError):
    """剪辑层输入、映射或 Windows 原生草稿写入失败。"""


DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30
EDIT_VERSION = "editing-layer-v1"
DEFAULT_CAPTION_ALIGNMENT = 1  # pyJianYingDraft: 1 = 水平居中

_CAPTION_DISPLAY_BREAKS = "\uFF0C\u3002\uFF01\uFF1F\uFF1B\uFF1A\u3001;,.!?;:"

DEFAULT_EDITING_SETTINGS: dict[str, Any] = {
    # pyJianYingDraft 的 Windows 字体枚举没有“微软雅黑”这一项；
    # 使用真实可解析的中文黑体，避免界面默认值进入写入层后静默回退或直接失败。
    # 字幕轨仍然生成，但不再暴露为剪辑交付页的可配置项。
    "font": "经典雅黑",
    "caption_alignment": "bottom_center",
    "caption_size": 10.0,
    "caption_color": "#FFFFFF",
    "border_color": "#000000",
    "border_width": 40.0,
    "line_spacing": 0,
    "narration_volume": 100.0,
    "bgm_volume": 22.0,
    "sfx_volume": 45.0,
}


def _setting_number(value: Any, key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise EditingLayerError(f"剪辑参数 {key} 必须是数字") from exc
    if not minimum <= number <= maximum:
        raise EditingLayerError(f"剪辑参数 {key} 必须在 {minimum:g}~{maximum:g} 范围内")
    return number


def _color_rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[index:index + 2], 16) / 255.0 for index in (1, 3, 5))  # type: ignore[return-value]


def normalize_editing_settings(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """把剪辑页控件归一化为 Windows 草稿写入层实际消费的字段。

    页面提交的值仍保留在 ``editing_handoff.json``；这里的返回值是写入层的
    唯一有效配置，避免出现“界面显示已设置、草稿仍使用硬编码默认值”。
    """

    raw = dict(DEFAULT_EDITING_SETTINGS)
    if settings:
        # 只接受音频混音参数；旧的字幕样式、动画、转场、滤镜和特效
        # 即使由历史客户端提交，也不能重新进入当前交付链路。
        submitted = dict(settings)
        for key in ("narration_volume", "bgm_volume", "sfx_volume"):
            if key in submitted:
                raw[key] = submitted[key]
    normalized = {
        "font": "经典雅黑",
        "caption_alignment": "bottom_center",
        "caption_size": 10.0,
        "caption_color": "#FFFFFF",
        "border_color": "#000000",
        "border_width": 40.0,
        "line_spacing": 0,
        "narration_volume": _setting_number(raw.get("narration_volume"), "narration_volume", 100.0, 0.0, 150.0),
        "bgm_volume": _setting_number(raw.get("bgm_volume"), "bgm_volume", 22.0, 0.0, 100.0),
        "sfx_volume": _setting_number(raw.get("sfx_volume"), "sfx_volume", 45.0, 0.0, 100.0),
    }
    return normalized


def format_caption_display_text(text: str, *, line_break_threshold: int = 14) -> str:
    """仅调整剪映显示换行，不改变字幕语义、时间轴或上游字幕数据。"""
    text = str(text).strip()
    if len(text) < line_break_threshold or "\n" in text:
        return text
    candidates = [
        index + 1
        for index, char in enumerate(text[:-1])
        if char in _CAPTION_DISPLAY_BREAKS and index >= 4 and len(text) - index - 1 >= 4
    ]
    if not candidates:
        return text
    midpoint = len(text) / 2
    split_at = min(candidates, key=lambda point: abs(point - midpoint))
    return f"{text[:split_at]}\n{text[split_at:]}"


@dataclass(frozen=True)
class EditingLayerPlan:
    """剪辑层的可审计中间变量快照。

    ``tracks`` 是写入轨道的最终编排，``native_items`` 是 Windows 本地写入所需
    的媒体项，``capcut_payloads`` 是与 CapCut Mate 节点字段对齐的只读投影。
    两者同时保存，避免把本地路径误当成 HTTP 可访问 URL。
    """

    project_id: str
    run_id: str
    source_plan_version: str
    source_asset_version: str
    edit_version: str
    width: int
    height: int
    fps: int
    platform_os: str
    draft_method: str
    total_timeline: TimeWindow
    tracks: list[dict[str, Any]]
    native_items: list[dict[str, Any]]
    captions: list[dict[str, Any]]
    capcut_payloads: dict[str, Any]
    bindings: list[EditBinding]
    keyframes: list[dict[str, Any]] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)

    @property
    def effects_enabled(self) -> bool:
        return bool(self.effects)

    def to_edit_manifest(
        self,
        *,
        draft_url: str = "",
        local_draft_path: str = "",
        status: str = "EDIT_MAPPING_ONLY",
        validation: Mapping[str, Any] | None = None,
    ) -> EditManifest:
        """把当前编译快照投影为正式治理层 ``EditManifest``。"""

        merged_validation = dict(self.validation)
        if validation:
            merged_validation.update(validation)
        return EditManifest(
            project_id=self.project_id,
            run_id=self.run_id,
            source_plan_version=self.source_plan_version,
            source_asset_version=self.source_asset_version,
            edit_version=self.edit_version,
            bindings=list(self.bindings),
            tracks=[dict(item) for item in self.tracks],
            draft_url=draft_url,
            local_draft_path=local_draft_path,
            status=status,
            validation=merged_validation,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "source_plan_version": self.source_plan_version,
            "source_asset_version": self.source_asset_version,
            "edit_version": self.edit_version,
            "timeline_unit": TIMELINE_UNIT,
            "canvas": {"width": self.width, "height": self.height, "fps": self.fps},
            "platform_os": self.platform_os,
            "draft_method": self.draft_method,
            "total_timeline": self.total_timeline.to_dict(),
            "tracks": [dict(item) for item in self.tracks],
            "native_items": [dict(item) for item in self.native_items],
            "captions": [dict(item) for item in self.captions],
            "capcut_payloads": dict(self.capcut_payloads),
            "bindings": [item.to_dict() for item in self.bindings],
            "keyframes": [dict(item) for item in self.keyframes],
            "effects": [dict(item) for item in self.effects],
            "transitions": [dict(item) for item in self.transitions],
            "validation": dict(self.validation),
        }


@dataclass(frozen=True)
class WindowsNativeDraftResult:
    """Windows 原生草稿落盘后的结构验收结果。"""

    draft_path: str
    platform_os: str
    last_modified_platform_os: str
    duration_us: int
    tracks: list[dict[str, Any]]
    media_paths_exist: bool
    effects_count: int
    transitions_count: int
    edit_manifest: EditManifest
    packaging_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_path": self.draft_path,
            "platform_os": self.platform_os,
            "last_modified_platform_os": self.last_modified_platform_os,
            "duration_us": self.duration_us,
            "tracks": [dict(item) for item in self.tracks],
            "media_paths_exist": self.media_paths_exist,
            "effects_count": self.effects_count,
            "transitions_count": self.transitions_count,
            "edit_manifest": self.edit_manifest.to_dict(),
            "packaging_report": dict(self.packaging_report),
        }


def _require_locked_director(director: DirectorLockedManifest) -> None:
    if not isinstance(director, DirectorLockedManifest):
        raise EditingLayerError("剪辑层必须接收 DirectorLockedManifest，而不是未冻结的编导输出")
    if director.status != "DIRECTOR_LOCKED":
        raise EditingLayerError("剪辑层拒绝读取非 DIRECTOR_LOCKED 编导数据")


def _require_assets(assets: AssetRegistry, director: DirectorLockedManifest) -> None:
    if not isinstance(assets, AssetRegistry):
        raise EditingLayerError("剪辑层必须接收 AssetRegistry")
    try:
        assets.validate_against(director)
    except GovernanceContractError as exc:
        raise EditingLayerError(f"素材登记与编导锁定对象不一致：{exc}") from exc


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EditingLayerError(f"{field_name} 必须是正整数")
    return value


def _require_window(value: TimeWindow | None, field_name: str) -> TimeWindow:
    if value is None:
        raise EditingLayerError(f"{field_name} 缺少微秒时间线")
    return value


def _record_window(
    record: AssetRecord,
    requirement: MaterialRequirement,
    director: DirectorLockedManifest,
) -> TimeWindow:
    """取得剪辑写入窗口；不把实际素材时长覆盖到计划时间线。"""

    # 单镜头主画面必须直接继承 DirectorShot.timeline。不能使用素材实际时长，
    # 也不能因为异步完成顺序或 requirement 的兼容时间字段改变镜头坑位。
    if record.asset_type.lower() in {"video", "image"} and record.shot_ids:
        shot_windows = [
            shot.timeline
            for shot in director.shots
            if shot.shot_id in set(record.shot_ids)
        ]
        if shot_windows:
            return TimeWindow(
                min(item.start_us for item in shot_windows),
                max(item.end_us for item in shot_windows),
            )

    # project.audio.bgm 可以被交接层拆成多个同一 requirement 的连续片段；
    # 此时必须尊重 record.actual_timeline，否则每一段都会错误覆盖整片。
    if record.role.lower() in {"background_music", "bgm", "背景音乐"} and record.actual_timeline is not None:
        return record.actual_timeline
    if requirement.timeline is not None:
        return requirement.timeline
    shot_windows = [
        shot.timeline
        for shot in director.shots
        if shot.shot_id in set(record.shot_ids)
    ]
    if shot_windows:
        return TimeWindow(
            min(item.start_us for item in shot_windows),
            max(item.end_us for item in shot_windows),
        )
    if record.actual_timeline is not None:
        return record.actual_timeline
    if record.role in {"protagonist", "character_placeholder"}:
        return TimeWindow(0, min(2_000_000, director.total_timeline.end_us))
    raise EditingLayerError(f"{record.asset_id} 没有可用于剪辑的时间线")


def _media_path(record: AssetRecord) -> str:
    """返回 Windows 原生写入路径；远程 URL 不在此处伪装成本地文件。"""

    if record.local_path:
        return str(Path(record.local_path).expanduser().resolve())
    return ""


def _remote_url(record: AssetRecord) -> str:
    value = record.remote_url.strip()
    return value if value.startswith(("http://", "https://")) else ""


def _track_role(record: AssetRecord) -> str:
    role = record.role.lower()
    asset_type = record.asset_type.lower()
    if role in {"first_frame", "first_frame_reference", "首帧", "首帧参考"}:
        return "首帧参考"
    if role in {"digital_human", "digital_human_alternative", "digital_human_placeholder", "数字人"}:
        # 数字人是主画面的一种视频来源；与 AIGC 画面共用同一条时间线轨道，
        # 仅在素材角色和镜头绑定中保留其数字人来源身份。
        return "AIGC动画"
    if role in {"protagonist", "character_placeholder", "character_anchor", "主角占位", "角色参考"}:
        return "角色参考（不入剪辑）"
    if role in {"narration", "voiceover", "解说"}:
        return "解说"
    if role in {"sound_effect", "音效", "sfx"}:
        return "音效"
    if role in {"background_music", "bgm", "背景音乐"}:
        return "BGM"
    if asset_type == "audio":
        return "解说"
    if asset_type == "caption":
        return "字幕"
    return "AIGC动画"


def _is_material_only_reference(record: AssetRecord) -> bool:
    """角色一致性参考只服务首帧/视频生成，不能误写入最终时间线。"""

    return record.role.lower() in {
        "protagonist", "character_placeholder", "character_anchor", "主角占位", "角色参考",
    }


def _native_asset_type(record: AssetRecord) -> str:
    asset_type = record.asset_type.lower()
    if asset_type in {"video", "audio", "image"}:
        return asset_type
    raise EditingLayerError(f"{record.asset_id} 的 asset_type 不支持 Windows 原生写入：{record.asset_type}")


def _json_array(items: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps([dict(item) for item in items], ensure_ascii=False, separators=(",", ":"))


def _make_bindings(
    director: DirectorLockedManifest,
    records: Sequence[AssetRecord],
    requirements: Mapping[str, MaterialRequirement],
) -> list[EditBinding]:
    by_shot: dict[str, list[AssetRecord]] = defaultdict(list)
    for record in records:
        for shot_id in record.shot_ids:
            by_shot[shot_id].append(record)

    preferred_roles = {
        "primary_visual": 0,
        # 静态图片也可以是镜头的主画面。它不能被误认为“首帧参考”，
        # 否则 g07_s01 这类低动态镜头会在剪辑层没有可绑定的主素材。
        "primary_visual_image": 0,
        "aigc_video": 1,
        "digital_human_alternative": 2,
        "digital_human": 3,
    }
    bindings: list[EditBinding] = []
    used_requirements: set[str] = set()
    for shot in director.shots:
        candidates = [
            record
            for record in by_shot.get(shot.shot_id, [])
            if record.asset_type.lower() == "video"
            or (
                record.asset_type.lower() == "image"
                and record.role.lower() in {"primary_visual_image", "primary_visual_static", "主画面图片"}
            )
        ]
        candidates.sort(key=lambda item: (preferred_roles.get(item.role, 99), item.source_index))
        if not candidates:
            continue
        record = candidates[0]
        requirement = requirements[record.requirement_id]
        # EditManifest 的绑定是一条 shot -> requirement 的稳定关系；一个
        # group/project 资产可以覆盖多个 shot，但不能被重复伪装成多个独立绑定。
        if requirement.requirement_id in used_requirements:
            continue
        used_requirements.add(requirement.requirement_id)
        bindings.append(
            EditBinding(
                shot_id=shot.shot_id,
                requirement_id=requirement.requirement_id,
                asset_id=record.asset_id,
                track_role=_track_role(record),
                track_id="",
                segment_id="",
                planned_timeline=shot.timeline,
                asset_duration_us=record.asset_duration_us,
                edit_timeline=record.actual_timeline,
                fit_policy="planned_timeline_with_source_trim",
                status="PLANNED",
                metadata={"source_node": record.source_node, "evidence_level": record.evidence_level},
            )
        )
    return bindings


def compile_editing_layer(
    director: DirectorLockedManifest,
    assets: AssetRegistry,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
    edit_version: str = EDIT_VERSION,
) -> EditingLayerPlan:
    """编译三层正式交接数据，不调用模型、网络或剪映写入服务。"""

    _require_locked_director(director)
    _require_assets(assets, director)
    width = _require_positive_int(width, "width")
    height = _require_positive_int(height, "height")
    fps = _require_positive_int(fps, "fps")
    if not isinstance(edit_version, str) or not edit_version.strip():
        raise EditingLayerError("edit_version 不能为空")

    requirements = {item.requirement_id: item for item in director.requirements}
    native_items: list[dict[str, Any]] = []
    remote_videos: list[dict[str, Any]] = []
    remote_audios: dict[str, list[dict[str, Any]]] = {"narration": [], "bgm": [], "sound_effect": []}

    for record in sorted(assets.records, key=lambda item: (item.source_index, item.asset_id)):
        if record.status in {"BLOCKED", "FAILED_FINAL", "SKIPPED_OPTIONAL"}:
            continue
        if _is_material_only_reference(record):
            continue
        requirement = requirements[record.requirement_id]
        asset_type = _native_asset_type(record)
        window = _record_window(record, requirement, director)
        if asset_type in {"video", "image"} and len(record.shot_ids) == 1:
            target_shot_id = str(record.metadata.get("target_shot_id") or "").strip()
            if target_shot_id and target_shot_id != record.shot_ids[0]:
                raise EditingLayerError(
                    f"{record.asset_id} 的 target_shot_id={target_shot_id} "
                    f"与 shot_ids={record.shot_ids} 不一致"
                )
            shot = next((item for item in director.shots if item.shot_id == record.shot_ids[0]), None)
            if shot is None:
                raise EditingLayerError(f"{record.asset_id} 引用了不存在的镜头：{record.shot_ids[0]}")
            if window != shot.timeline:
                raise EditingLayerError(
                    f"{record.asset_id} 未继承镜头 {shot.shot_id} 的时间线："
                    f"实际 {window.to_dict()}，应为 {shot.timeline.to_dict()}"
                )
        track_name = _track_role(record)
        item = {
            "asset_id": record.asset_id,
            "requirement_id": record.requirement_id,
            "asset_type": asset_type,
            "role": record.role,
            "track_name": track_name,
            "source_node": record.source_node,
            "source_index": record.source_index,
            "start_us": window.start_us,
            "end_us": window.end_us,
            "duration_us": window.duration_us,
            "planned_timeline": requirement.timeline.to_dict() if requirement.timeline else None,
            "asset_duration_us": record.asset_duration_us,
            "native_path": _media_path(record),
            "remote_url": _remote_url(record),
            "metadata": dict(record.metadata),
            "shot_ids": list(record.shot_ids),
        }
        native_items.append(item)
        if asset_type == "video" and item["remote_url"]:
            remote_videos.append({
                "video_url": item["remote_url"],
                "start": window.start_us,
                "end": window.end_us,
                "duration": record.asset_duration_us or window.duration_us,
                **{key: item["metadata"][key] for key in ("volume", "transition", "transition_duration") if key in item["metadata"]},
            })
        if asset_type == "audio" and item["remote_url"]:
            audio_item = {
                "audio_url": item["remote_url"],
                "start": window.start_us,
                "end": window.end_us,
                "duration": record.asset_duration_us or window.duration_us,
                "volume": item["metadata"].get("volume", 1.0),
                "audio_effect": item["metadata"].get("audio_effect"),
            }
            if track_name == "BGM":
                remote_audios["bgm"].append(audio_item)
            elif track_name == "音效":
                remote_audios["sound_effect"].append(audio_item)
            else:
                remote_audios["narration"].append(audio_item)

    captions = [
        {
            "caption_id": caption.caption_id,
            "group_id": caption.group_id,
            "text": caption.text,
            "start": caption.timeline.start_us,
            "end": caption.timeline.end_us,
        }
        for caption in director.captions
    ]
    tracks = [
        {"track_name": "首帧参考", "track_type": "video", "item_count": sum(item["track_name"] == "首帧参考" for item in native_items)},
        {"track_name": "AIGC动画", "track_type": "video", "item_count": sum(item["track_name"] == "AIGC动画" for item in native_items)},
        {"track_name": "解说", "track_type": "audio", "item_count": sum(item["track_name"] == "解说" for item in native_items)},
        {"track_name": "BGM", "track_type": "audio", "item_count": sum(item["track_name"] == "BGM" for item in native_items)},
        {"track_name": "字幕", "track_type": "text", "item_count": len(captions)},
    ]
    sound_effect_count = sum(item["track_name"] == "音效" for item in native_items)
    if sound_effect_count:
        tracks.insert(4, {"track_name": "音效", "track_type": "audio", "track_role": "sound_effect", "item_count": sound_effect_count})
    bindings = _make_bindings(director, assets.records, requirements)
    capcut_payloads = {
        "create_draft": {"height": height, "width": width},
        "add_videos": {"video_infos": _json_array(remote_videos)},
        "add_narration": {"audio_infos": _json_array(remote_audios["narration"])},
        "add_bgm": {"audio_infos": _json_array(remote_audios["bgm"])},
        "add_sound_effects": {"audio_infos": _json_array(remote_audios["sound_effect"]), "track_name": "音效", "track_type": "audio", "track_role": "sound_effect"},
        "add_captions": {"captions": _json_array(captions), "alignment": DEFAULT_CAPTION_ALIGNMENT},
        "add_keyframes": {"keyframes": "[]"},
        "add_effects": {"effect_infos": "[]"},
        "platform": "windows_native",
    }
    validation = {
        "timeline_unit": TIMELINE_UNIT,
        "director_status": director.status,
        "asset_status": assets.status,
        "asset_record_count": len(native_items),
        "caption_count": len(captions),
        "binding_count": len(bindings),
        "effects_enabled": False,
        "transitions_enabled": False,
        "local_write_requires_native_path": True,
        "remote_payload_url_count": len(remote_videos) + sum(len(value) for value in remote_audios.values()),
        "selected_digital_human_shot_ids": [
            shot_id
            for record in assets.records
            if record.role.lower() in {"digital_human", "digital_human_alternative"}
            for shot_id in record.shot_ids
        ],
    }
    plan = EditingLayerPlan(
        project_id=director.project_id,
        run_id=director.run_id,
        source_plan_version=director.plan_version,
        source_asset_version=assets.asset_version,
        edit_version=edit_version,
        width=width,
        height=height,
        fps=fps,
        platform_os="windows",
        draft_method="capcut-mate.pyJianYingDraft.DraftFolder.create_draft",
        total_timeline=director.total_timeline,
        tracks=tracks,
        native_items=native_items,
        captions=captions,
        capcut_payloads=capcut_payloads,
        bindings=bindings,
        validation=validation,
    )
    # 在输出前做一次正式版本交接验证，防止编译器只生成“看起来合理”的字段。
    plan.to_edit_manifest().validate_against(director, assets)
    return plan


def validate_windows_native_plan(plan: EditingLayerPlan) -> None:
    """写入前门禁：只能写 Windows 原生、本地素材和无特效基线。"""

    if not isinstance(plan, EditingLayerPlan):
        raise EditingLayerError("需要 EditingLayerPlan")
    if plan.platform_os != "windows":
        raise EditingLayerError("剪辑层只允许 Windows 原生草稿")
    if plan.effects or plan.transitions:
        raise EditingLayerError("当前 Windows 稳定基线禁止写入特效或转场")
    missing = [
        item["asset_id"]
        for item in plan.native_items
        if item["asset_type"] in {"video", "audio", "image"}
        and not Path(item["native_path"]).is_file()
    ]
    if missing:
        raise EditingLayerError(
            "剪辑层不能把 remote_url 当作本地素材；以下素材缺少 native_path 文件："
            + ", ".join(missing)
        )


def _add_capcut_src_to_path(capcut_mate_root: Path) -> None:
    # The console runs in its own venv, while capcut-mate keeps the runtime
    # dependencies for pyJianYingDraft in its own project venv.  Add that
    # site-packages directory before importing the local package so a missing
    # optional dependency cannot be misreported as a missing DraftFolder.
    dependency_site_packages = capcut_mate_root / ".venv" / "Lib" / "site-packages"
    if dependency_site_packages.is_dir() and str(dependency_site_packages) not in sys.path:
        sys.path.insert(0, str(dependency_site_packages))
    source = str(capcut_mate_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)


def _copy_native_assets(plan: EditingLayerPlan, draft_dir: Path) -> dict[str, Path]:
    destinations: dict[str, Path] = {}
    for item in plan.native_items:
        source = Path(item["native_path"])
        if item["asset_type"] == "audio":
            category = "audios"
        elif item["asset_type"] == "image":
            category = "images"
        else:
            category = "videos"
        target_dir = draft_dir / "assets" / category
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_asset_id = "".join(
            "_" if char in '<>:"/\\|?*' else char
            for char in str(item["asset_id"])
        )
        target = target_dir / f"{safe_asset_id}_{source.name}"
        shutil.copy2(source, target)
        destinations[item["asset_id"]] = target
    return destinations


def _write_local_meta(draft_dir: Path, draft_name: str) -> None:
    path = draft_dir / "draft_meta_info.json"
    if not path.is_file():
        return
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(
        {
            "draft_id": draft_name,
            "draft_name": draft_name,
            "draft_fold_path": str(draft_dir).replace("\\", "/"),
            "draft_root_path": str(draft_dir.parent).replace("\\", "/"),
            "draft_cover": "",
        }
    )
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _native_track_map(item: Mapping[str, Any]) -> tuple[str, str]:
    track_name = str(item["track_name"])
    asset_type = str(item["asset_type"])
    if asset_type == "audio":
        return track_name, "audio"
    if asset_type == "image":
        return track_name, "video"
    return track_name, "video"


def _set_source_timerange(segment: Any, duration_us: int) -> Any:
    """尽量把本地媒体实际时长限制在素材可读范围内。"""

    try:
        segment.source_timerange = segment.source_timerange.__class__(0, duration_us)
    except (AttributeError, TypeError):
        # pyJianYingDraft 的不同版本对 source_timerange 暴露略有差异；目标
        # Timerange 已经按规划时间线创建，不能因为可选字段失败而伪造时长。
        pass
    return segment


def _bounded_source_duration_us(item: Mapping[str, Any], actual_duration_us: int) -> int:
    """用本地文件真实时长限制剪映源截取范围。

    ``asset_duration_us`` 是交接层记录的期望/登记时长，不一定等于复制后
    文件被媒体解析器识别出的时长。目标坑位仍使用编导时间线；这里只限制
    源素材读取范围，素材略短时由 ``VideoSegment``/``AudioSegment`` 计算轻微
    变速来补齐目标坑位。
    """

    actual = int(actual_duration_us)
    if actual <= 0:
        raise EditingLayerError(f"{item['asset_id']} 的本地素材时长必须大于 0")
    planned = min(
        int(item.get("asset_duration_us") or item["duration_us"]),
        int(item["duration_us"]),
    )
    return min(planned, actual)


def _jianying_frame_boundary_us(timestamp_us: int, fps: int) -> int:
    """把微秒时间转换为剪映可保存的帧边界（向上取整）。

    剪映 Windows 草稿不是任意微秒时间线：30fps 下每个边界必须落在
    1/30 秒的帧边界上。开始和结束必须使用同一套边界函数，否则逐段
    独立取整会让后续镜头逐渐偏离编导坑位。
    """

    if timestamp_us <= 0:
        return 0
    frame_index = math.ceil(timestamp_us * fps / 1_000_000)
    return (frame_index * 1_000_000) // fps


def _jianying_target_window(start_us: int, end_us: int, fps: int) -> tuple[int, int]:
    start = _jianying_frame_boundary_us(int(start_us), fps)
    end = _jianying_frame_boundary_us(int(end_us), fps)
    if end <= start:
        end = start + max(1, 1_000_000 // fps)
    return start, end - start


def _validate_written_native_windows(
    plan: EditingLayerPlan,
    content: Mapping[str, Any],
) -> None:
    """验证草稿中的媒体顺序和帧化坑位仍与写入计划一致。"""

    expected_by_track: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for item in plan.native_items:
        start, duration = _jianying_target_window(item["start_us"], item["end_us"], plan.fps)
        expected_by_track[str(item["track_name"])].append((start, duration, str(item["asset_id"])))

    tracks = {
        str(track.get("name")): track
        for track in content.get("tracks", [])
        if isinstance(track, Mapping)
    }
    errors: list[str] = []
    for track_name, expected in expected_by_track.items():
        track = tracks.get(track_name)
        actual = list(track.get("segments", [])) if isinstance(track, Mapping) else []
        if len(actual) != len(expected):
            errors.append(f"{track_name} 数量 {len(actual)} != 计划 {len(expected)}")
            continue
        for index, (start, duration, asset_id) in enumerate(expected):
            timerange = actual[index].get("target_timerange", {})
            actual_start = int(timerange.get("start", -1))
            actual_duration = int(timerange.get("duration", -1))
            if actual_start != start or actual_duration != duration:
                errors.append(
                    f"{track_name}[{index}] {asset_id} 坑位不一致："
                    f"实际 ({actual_start},{actual_duration})，期望 ({start},{duration})"
                )
                if len(errors) >= 5:
                    break
        if len(errors) >= 5:
            break
    if errors:
        raise EditingLayerError("Windows 草稿镜头坑位校验失败：" + "；".join(errors))


def _build_manifest_after_write(
    plan: EditingLayerPlan,
    draft_dir: Path,
    content: Mapping[str, Any],
    *,
    status: str,
    validation: Mapping[str, Any] | None = None,
) -> EditManifest:
    track_ids = {
        str(item.get("name")): str(item.get("id", ""))
        for item in content.get("tracks", [])
        if isinstance(item, Mapping)
    }
    bindings: list[EditBinding] = []
    for binding in plan.bindings:
        bindings.append(
            EditBinding(
                shot_id=binding.shot_id,
                requirement_id=binding.requirement_id,
                asset_id=binding.asset_id,
                track_role=binding.track_role,
                track_id=track_ids.get(binding.track_role, ""),
                segment_id="",
                planned_timeline=binding.planned_timeline,
                asset_duration_us=binding.asset_duration_us,
                edit_timeline=binding.edit_timeline,
                fit_policy=binding.fit_policy,
                status="WRITTEN" if status != "EDIT_MAPPING_ONLY" else binding.status,
                metadata=dict(binding.metadata),
            )
        )
    return EditManifest(
        project_id=plan.project_id,
        run_id=plan.run_id,
        source_plan_version=plan.source_plan_version,
        source_asset_version=plan.source_asset_version,
        edit_version=plan.edit_version,
        bindings=bindings,
        tracks=[dict(item) for item in plan.tracks],
        draft_url="",
        local_draft_path=str(draft_dir),
        status=status,
        validation={
            **plan.validation,
            **dict(validation or {}),
            "platform_os": content.get("platform", {}).get("os", ""),
            "last_modified_platform_os": content.get("last_modified_platform", {}).get("os", ""),
            "draft_path": str(draft_dir),
        },
    )


def _load_packaging_template(path_value: object) -> dict[str, Any]:
    path = Path(str(path_value or "")).expanduser()
    # 包装包的完整风格分析保存的是源草稿目录；分类模板保存的通常是
    # raw_snapshot/draft_content.json。两种入口都接受，避免完整风格回退为
    # 分类模板而静默丢失全片音频、转场、字幕样式和全片特效。
    if path.is_dir():
        path = path / "draft_content.json"
    if not path.is_file():
        raise EditingLayerError(f"包装模板数据不存在：{path}")
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EditingLayerError(f"包装模板数据无法读取：{path}") from exc
    if not isinstance(content, Mapping):
        raise EditingLayerError(f"包装模板数据不是对象：{path}")
    return dict(content)


def _template_visual_segments(template: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for raw_track in template.get("tracks", []):
        if not isinstance(raw_track, Mapping):
            continue
        if str(raw_track.get("type") or raw_track.get("track_type") or "") not in {"video", ""}:
            continue
        for raw_segment in raw_track.get("segments", []):
            if isinstance(raw_segment, Mapping):
                result.append(raw_segment)
    return result


def _template_keyframe_groups(segment: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """读取一个模板片段的关键帧组，并保留剪映的等比缩放语义。"""

    groups = [item for item in segment.get("common_keyframes", []) if isinstance(item, Mapping)]
    uniform = segment.get("uniform_scale")
    uniform_on = isinstance(uniform, Mapping) and bool(uniform.get("on"))
    if not uniform_on:
        return groups
    normalized: list[Mapping[str, Any]] = []
    for group in groups:
        if str(group.get("property_type") or "") != "KFTypeScaleX":
            normalized.append(group)
            continue
        copied = dict(group)
        copied["property_type"] = "UNIFORM_SCALE"
        normalized.append(copied)
    return normalized


def _enum_from_template_item(enum_type: Any, item: Mapping[str, Any]) -> Any | None:
    wanted_name = str(item.get("name") or item.get("effect_name") or "").strip()
    wanted_ids = {
        str(item.get(key) or "").strip()
        for key in ("resource_id", "effect_id", "id")
        if str(item.get(key) or "").strip()
    }
    candidates = list(enum_type)
    # 剪映库中允许不同资源使用同一个显示名称；资源 ID 才是模板绑定的
    # 唯一身份，必须优先按 resource_id/effect_id 匹配，名称只能作为旧模板兜底。
    for candidate in candidates:
        metadata = getattr(candidate, "value", None)
        if wanted_ids and wanted_ids.intersection(
            str(getattr(metadata, key, "")).strip() for key in ("resource_id", "effect_id")
        ):
            return candidate
    for candidate in candidates:
        metadata = getattr(candidate, "value", None)
        if wanted_name and str(getattr(metadata, "name", "")).strip().lower() == wanted_name.lower():
            return candidate
    return None


def _template_material_map(template: Mapping[str, Any], collection: str) -> dict[str, Mapping[str, Any]]:
    materials = template.get("materials")
    if not isinstance(materials, Mapping):
        return {}
    items = materials.get(collection)
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return {}
    return {
        str(item.get("id") or ""): item
        for item in items
        if isinstance(item, Mapping) and str(item.get("id") or "").strip()
    }


def _template_effect_specs(
    template: Mapping[str, Any],
    video_scene_effect_type: Any,
) -> list[tuple[Any, str, int, int, int, str]]:
    """读取模板独立特效轨道，并保留原始特效素材 ID。"""

    effect_materials: dict[str, Mapping[str, Any]] = {}
    for collection in ("video_effects", "effects", "plugin_effects"):
        effect_materials.update(_template_material_map(template, collection))
    specs: list[tuple[Any, str, int, int, int, str]] = []
    tracks = template.get("tracks")
    if isinstance(tracks, Sequence) and not isinstance(tracks, (str, bytes, bytearray)):
        for track_index, raw_track in enumerate(tracks):
            if not isinstance(raw_track, Mapping) or str(raw_track.get("type") or "") != "effect":
                continue
            segments = raw_track.get("segments")
            if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes, bytearray)):
                continue
            for raw_segment in segments:
                if not isinstance(raw_segment, Mapping):
                    continue
                material = effect_materials.get(str(raw_segment.get("material_id") or ""))
                if material is None:
                    continue
                effect = _enum_from_template_item(video_scene_effect_type, material)
                if effect is None:
                    continue
                timerange = raw_segment.get("target_timerange")
                if not isinstance(timerange, Mapping):
                    continue
                start = max(0, int(timerange.get("start") or 0))
                duration = max(0, int(timerange.get("duration") or 0))
                if duration:
                    specs.append(
                        (
                            effect,
                            str(material.get("name") or effect.value.name),
                            start,
                            duration,
                            track_index,
                            str(material.get("id") or ""),
                        )
                    )
    if specs:
        return specs

    # 兼容旧模板：旧提取结果可能只有 materials.video_effects，没有 effect 轨道。
    source_duration = max(0, int(template.get("duration") or 0))
    return [
        (
            effect,
            str(item.get("name") or effect.value.name),
            0,
            source_duration,
            0,
            str(item.get("id") or ""),
        )
        for item in effect_materials.values()
        if (effect := _enum_from_template_item(video_scene_effect_type, item)) is not None and source_duration
    ]


def _template_audio_specs(template: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], int, int, Mapping[str, Any]]]:
    """读取模板中明确作为音效使用的本地音频，不复制解说和 BGM。"""

    audio_materials = _template_material_map(template, "audios")
    specs: list[tuple[Mapping[str, Any], int, int, Mapping[str, Any]]] = []
    tracks = template.get("tracks")
    if not isinstance(tracks, Sequence) or isinstance(tracks, (str, bytes, bytearray)):
        return specs
    fade_materials = _template_material_map(template, "audio_fades")
    for raw_track in tracks:
        if not isinstance(raw_track, Mapping) or str(raw_track.get("type") or "") != "audio":
            continue
        # 有明确名称的“解说/BGM”属于源草稿内容；未命名音频轨是模板音效轨。
        if str(raw_track.get("name") or "").strip():
            continue
        segments = raw_track.get("segments")
        if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes, bytearray)):
            continue
        for raw_segment in segments:
            if not isinstance(raw_segment, Mapping):
                continue
            material = audio_materials.get(str(raw_segment.get("material_id") or ""))
            if material is None:
                continue
            name = str(material.get("name") or "")
            if not name or name.lower().startswith(("asset-", "opening_asset-")):
                continue
            timerange = raw_segment.get("target_timerange")
            if not isinstance(timerange, Mapping):
                continue
            start = max(0, int(timerange.get("start") or 0))
            duration = max(0, int(timerange.get("duration") or 0))
            if duration <= 0:
                continue
            fade = next(
                (
                    fade_materials.get(str(ref))
                    for ref in raw_segment.get("extra_material_refs", [])
                    if str(ref) in fade_materials
                ),
                {},
            )
            # 模板音效的音量属于风格变量，不能在应用时统一写成 100%。
            # 放到副本的内部字段中，避免修改包装包清单本身。
            material_with_volume = dict(material)
            try:
                material_with_volume["_packaging_source_volume"] = max(
                    0.0,
                    min(2.0, float(raw_segment.get("volume") if raw_segment.get("volume") is not None else 1.0)),
                )
            except (TypeError, ValueError):
                material_with_volume["_packaging_source_volume"] = 1.0
            specs.append((material_with_volume, start, duration, fade if isinstance(fade, Mapping) else {}))
    return specs


def _template_audio_effect_specs(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    """读取音频片段上的音频特效及其相对时间范围。"""

    materials = template.get("materials")
    if not isinstance(materials, Mapping):
        return []
    effect_items = _template_material_map(template, "audio_effects")
    if not effect_items:
        return []
    tracks = template.get("tracks")
    if not isinstance(tracks, Sequence) or isinstance(tracks, (str, bytes, bytearray)):
        return []
    specs: list[dict[str, Any]] = []
    for raw_track in tracks:
        if not isinstance(raw_track, Mapping) or str(raw_track.get("type") or "") != "audio":
            continue
        track_name = str(raw_track.get("name") or "").strip()
        for raw_segment in raw_track.get("segments", []):
            if not isinstance(raw_segment, Mapping):
                continue
            timerange = raw_segment.get("target_timerange")
            if not isinstance(timerange, Mapping):
                continue
            start = max(0, int(timerange.get("start") or 0))
            duration = max(0, int(timerange.get("duration") or 0))
            if duration <= 0:
                continue
            effects = [
                copy.deepcopy(effect_items[str(ref)])
                for ref in raw_segment.get("extra_material_refs", [])
                if str(ref) in effect_items
            ]
            if effects:
                specs.append({
                    "track_name": track_name,
                    "start_us": start,
                    "duration_us": duration,
                    "effects": effects,
                })
    return specs


def _template_audio_effect_items(template: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    materials = template.get("materials")
    if not isinstance(materials, Mapping):
        return []
    items = materials.get("audio_effects")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def _template_audio_path(template_path: Path, material: Mapping[str, Any]) -> Path | None:
    """优先使用包装包内的音频副本，避免依赖母版草稿的旧绝对路径。"""

    source_path = Path(str(material.get("path") or ""))
    candidates = [
        template_path.parent / "assets" / "external_audio" / source_path.name,
        template_path.parent / "assets" / "audios" / source_path.name,
        source_path,
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _inject_packaging_text_templates(
    content: dict[str, Any],
    template_specs: Sequence[Mapping[str, Any]],
    title: str,
) -> dict[str, int]:
    """把片头文字模板资源挂到专用文字片段，避免被普通字幕写入覆盖。"""

    materials = content.setdefault("materials", {})
    if not isinstance(materials, dict):
        return {"templates": 0, "segments": 0}
    target_texts = materials.setdefault("texts", [])
    target_templates = materials.setdefault("text_templates", [])
    if not isinstance(target_texts, list) or not isinstance(target_templates, list):
        return {"templates": 0, "segments": 0}
    tracks = content.get("tracks", [])
    if not isinstance(tracks, list):
        return {"templates": 0, "segments": 0}
    target_tracks = [
        track
        for track in tracks
        if isinstance(track, dict) and track.get("name") == "包装-片头文字模板"
    ]
    if not target_tracks:
        return {"templates": 0, "segments": 0}

    template_count = 0
    segment_count = 0
    for spec in template_specs:
        template_path = Path(str(spec.get("template_path") or ""))
        if template_path.is_dir():
            template_path = template_path / "draft_content.json"
        if not template_path.is_file():
            continue
        try:
            template_content = _load_packaging_template(template_path)
        except EditingLayerError:
            continue
        source_materials = template_content.get("materials")
        if not isinstance(source_materials, Mapping):
            continue
        source_templates = source_materials.get("text_templates")
        source_texts = source_materials.get("texts")
        if not isinstance(source_templates, Sequence) or isinstance(source_templates, (str, bytes, bytearray)):
            continue
        source_text_map = {
            str(item.get("id") or ""): item
            for item in (source_texts if isinstance(source_texts, Sequence) else [])
            if isinstance(item, Mapping) and str(item.get("id") or "").strip()
        }
        for source_template in source_templates:
            if not isinstance(source_template, Mapping):
                continue
            template_item = copy.deepcopy(dict(source_template))
            new_template_id = str(uuid.uuid4()).upper()
            template_item["id"] = new_template_id
            info_resources = template_item.get("text_info_resources")
            if isinstance(info_resources, list):
                for info in info_resources:
                    if not isinstance(info, dict):
                        continue
                    old_text_id = str(info.get("text_material_id") or "")
                    if old_text_id in source_text_map and not any(
                        str(item.get("id") or "") == old_text_id
                        for item in target_texts
                        if isinstance(item, Mapping)
                    ):
                        source_text = copy.deepcopy(dict(source_text_map[old_text_id]))
                        raw_content = source_text.get("content")
                        if isinstance(raw_content, str):
                            try:
                                text_content = json.loads(raw_content)
                                if isinstance(text_content, dict):
                                    text_content["text"] = title or "标题"
                                    for style in text_content.get("styles", []):
                                        if isinstance(style, dict):
                                            style["range"] = [0, len((title or "标题").encode("utf-16-le"))]
                                    source_text["content"] = json.dumps(text_content, ensure_ascii=False)
                            except json.JSONDecodeError:
                                source_text["content"] = title or "标题"
                        target_texts.append(source_text)
            target_templates.append(template_item)
            template_count += 1
            for track in target_tracks:
                segments = track.get("segments")
                if not isinstance(segments, list) or not segments:
                    continue
                segment = segments[0]
                if not isinstance(segment, dict):
                    continue
                segment["material_id"] = new_template_id
                segment["template_id"] = new_template_id
                refs = []
                for info in template_item.get("text_info_resources", []):
                    if isinstance(info, Mapping):
                        refs.extend(str(ref) for ref in info.get("extra_material_refs", []) if str(ref))
                segment["extra_material_refs"] = refs
                segment_count += 1
                break
            break
        if template_count:
            break
    return {"templates": template_count, "segments": segment_count}


def _suppress_subtitles_in_protected_ranges(
    content: dict[str, Any],
    protected_ranges: Sequence[Mapping[str, Any]],
) -> int:
    """片头/片尾保护区内不显示通用字幕，避免盖住专用文字模板。"""

    tracks = content.get("tracks")
    if not isinstance(tracks, list) or not protected_ranges:
        return 0
    ranges: list[tuple[int, int]] = []
    for item in protected_ranges:
        if not isinstance(item, Mapping):
            continue
        try:
            start = int(item.get("start_us") or 0)
            end = int(item.get("end_us") or 0)
        except (TypeError, ValueError):
            continue
        if end > start:
            ranges.append((start, end))
    if not ranges:
        return 0
    removed = 0
    for track in tracks:
        if not isinstance(track, dict) or str(track.get("name") or "") != "字幕":
            continue
        segments = track.get("segments")
        if not isinstance(segments, list):
            continue
        kept: list[object] = []
        for segment in segments:
            if not isinstance(segment, Mapping):
                kept.append(segment)
                continue
            timerange = segment.get("target_timerange")
            if not isinstance(timerange, Mapping):
                kept.append(segment)
                continue
            try:
                start = int(timerange.get("start") or 0)
                end = start + int(timerange.get("duration") or 0)
            except (TypeError, ValueError):
                kept.append(segment)
                continue
            if any(start < protected_end and end > protected_start for protected_start, protected_end in ranges):
                removed += 1
            else:
                kept.append(segment)
        track["segments"] = kept
    return removed


def _inject_packaging_subtitle_style(
    content: dict[str, Any],
    source_path: Path,
) -> int:
    """把完整目标草稿的主字幕样式套到新字幕文本，保留新文案内容。"""

    try:
        source = _load_packaging_template(source_path)
    except EditingLayerError:
        return 0
    source_materials = source.get("materials")
    target_materials = content.get("materials")
    tracks = content.get("tracks")
    if not isinstance(source_materials, Mapping) or not isinstance(target_materials, dict) or not isinstance(tracks, list):
        return 0
    source_texts = [item for item in source_materials.get("texts", []) if isinstance(item, Mapping)]
    target_texts = [item for item in target_materials.get("texts", []) if isinstance(item, dict)]
    if not source_texts or not target_texts:
        return 0

    def style_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            item.get("font_path"), item.get("font_resource_id"), item.get("font_size"),
            item.get("alignment"), item.get("line_spacing"), item.get("letter_spacing"),
            item.get("border_color"), item.get("border_width"), item.get("has_shadow"),
            item.get("shadow_distance"), item.get("shadow_smoothing"),
        )

    counts: dict[tuple[Any, ...], int] = defaultdict(int)
    for item in source_texts:
        counts[style_key(item)] += 1
    source_style = max(source_texts, key=lambda item: counts[style_key(item)])
    subtitle_ids = {
        str(segment.get("material_id") or "")
        for track in tracks
        if isinstance(track, Mapping) and str(track.get("name") or "") == "字幕"
        for segment in track.get("segments", [])
        if isinstance(segment, Mapping) and str(segment.get("material_id") or "")
    }
    if not subtitle_ids:
        return 0

    def utf16_length(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    source_content = source_style.get("content")
    try:
        source_content_obj = json.loads(source_content) if isinstance(source_content, str) else {}
    except json.JSONDecodeError:
        source_content_obj = {}
    if not isinstance(source_content_obj, dict):
        source_content_obj = {}
    changed = 0
    for target in target_texts:
        if str(target.get("id") or "") not in subtitle_ids:
            continue
        target_text = ""
        try:
            target_content_obj = json.loads(str(target.get("content") or "{}"))
            if isinstance(target_content_obj, dict):
                target_text = str(target_content_obj.get("text") or "")
        except json.JSONDecodeError:
            target_content_obj = {}
        styled = copy.deepcopy(dict(source_style))
        styled["id"] = target.get("id")
        for key in ("content", "group_id", "words", "recognize_task_id", "type", "name"):
            if key in target:
                styled[key] = target[key]
        cloned_content = copy.deepcopy(source_content_obj)
        cloned_content["text"] = target_text
        text_len = utf16_length(target_text)
        for style in cloned_content.get("styles", []):
            if isinstance(style, dict):
                style["range"] = [0, text_len]
        styled["content"] = json.dumps(cloned_content, ensure_ascii=False)
        target.clear()
        target.update(styled)
        changed += 1
    return changed


def _inject_packaging_audio_effects(
    content: dict[str, Any],
    effect_specs: Sequence[Mapping[str, Any]],
) -> int:
    """补齐后挂音频特效的资源表，避免只保存片段引用而丢失素材定义。"""

    materials = content.setdefault("materials", {})
    if not isinstance(materials, dict):
        return 0
    target_items = materials.setdefault("audio_effects", [])
    if not isinstance(target_items, list):
        return 0
    existing_ids = {str(item.get("id") or "") for item in target_items if isinstance(item, Mapping)}
    added = 0
    for spec in effect_specs:
        effect_id = str(spec.get("effect_id") or "").strip()
        template_path = Path(str(spec.get("template_path") or ""))
        if not effect_id or effect_id in existing_ids or not template_path.is_file():
            continue
        try:
            template = _load_packaging_template(template_path)
        except EditingLayerError:
            continue
        source_items = _template_audio_effect_items(template)
        wanted_name = str(spec.get("name") or "").strip()
        source_item = next(
            (
                item
                for item in source_items
                if not wanted_name or str(item.get("name") or "").strip() == wanted_name
            ),
            None,
        )
        if source_item is None:
            continue
        copied = copy.deepcopy(dict(source_item))
        copied["id"] = effect_id
        copied["type"] = "audio_effect"
        target_items.append(copied)
        existing_ids.add(effect_id)
        added += 1
    return added


def _inject_packaging_video_effects(
    content: dict[str, Any],
    effect_bindings: Sequence[Mapping[str, Any]],
    effect_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> int:
    """把模板原始视频特效素材写回运行时特效片段。

    pyJianYingDraft 的枚举可能存在同名但不同 resource_id 的特效，甚至完全没有
    模板使用的资源。轨道可以先用枚举占位创建，保存后这里再按模板素材 ID 替换，
    从而保留原始 effect_id、resource_id、adjust_params 和 path。
    """

    materials = content.setdefault("materials", {})
    if not isinstance(materials, dict):
        return 0
    target_items = materials.setdefault("video_effects", [])
    if not isinstance(target_items, list):
        return 0
    target_index = {
        str(item.get("id") or ""): index
        for index, item in enumerate(target_items)
        if isinstance(item, Mapping) and str(item.get("id") or "").strip()
    }
    replaced = 0
    for binding in effect_bindings:
        runtime_id = str(binding.get("runtime_material_id") or "").strip()
        source_id = str(binding.get("template_material_id") or "").strip()
        template_path = Path(str(binding.get("template_path") or ""))
        if not runtime_id or not source_id or not template_path.is_file():
            continue
        try:
            template = _load_packaging_template(template_path)
        except EditingLayerError:
            continue
        source_materials: dict[str, Mapping[str, Any]] = {}
        for collection in ("video_effects", "effects", "plugin_effects"):
            source_materials.update(_template_material_map(template, collection))
        source_item = source_materials.get(source_id)
        if source_item is None:
            continue
        copied = copy.deepcopy(dict(source_item))
        override = (effect_overrides or {}).get(str(copied.get("name") or "").strip())
        if isinstance(override, Mapping):
            adjust_params = copied.get("adjust_params")
            if isinstance(adjust_params, list):
                for param in adjust_params:
                    if not isinstance(param, dict):
                        continue
                    param_name = str(param.get("name") or "").strip()
                    if param_name in override:
                        param["value"] = override[param_name]
            if "value" in override:
                copied["value"] = override["value"]
        # 轨道片段引用的是运行时生成的全局 ID，模板素材的原始 ID不能直接复用。
        copied["id"] = runtime_id
        copied["type"] = str(copied.get("type") or "video_effect")
        if runtime_id in target_index:
            target_items[target_index[runtime_id]] = copied
        else:
            target_index[runtime_id] = len(target_items)
            target_items.append(copied)
        replaced += 1
    return replaced


def _resolve_packaging_targets(
    packaging_application: Mapping[str, Any],
    *,
    segments_by_shot: Mapping[str, Sequence[Any]],
) -> tuple[list[tuple[str, str, Sequence[Any]]], list[dict[str, str]]]:
    """按镜头边界解析包装目标，片头片尾覆盖普通镜头分类。

    首尾模板是独立的包装区。即使首个镜头本身是图片、视频或数字人，
    也不能把普通分类模板和首尾模板叠加到同一时间范围；否则会出现两套
    特效、关键帧、音效或文字模板互相覆盖。没有对应首尾模板时，才保留
    普通分类作为降级路径。
    """

    category_paths = packaging_application.get("categories")
    shot_categories = packaging_application.get("shot_categories")
    if not isinstance(category_paths, Mapping) or not isinstance(shot_categories, Mapping):
        return [], []

    first_shot_id = str(packaging_application.get("first_shot_id") or "").strip()
    last_shot_id = str(packaging_application.get("last_shot_id") or "").strip()
    fixed_by_shot: dict[str, str] = {}
    for fixed_category, shot_id in (("opening", first_shot_id), ("ending", last_shot_id)):
        if shot_id and shot_id in segments_by_shot and fixed_category in category_paths:
            fixed_by_shot[shot_id] = fixed_category

    targets: list[tuple[str, str, Sequence[Any]]] = []
    overrides: list[dict[str, str]] = []
    for raw_shot_id, raw_category in shot_categories.items():
        shot_id = str(raw_shot_id or "").strip()
        category = str(raw_category or "").strip()
        shot_segments = segments_by_shot.get(shot_id, [])
        if not shot_id or not category or not shot_segments or category not in category_paths:
            continue
        fixed_category = fixed_by_shot.get(shot_id)
        if fixed_category:
            if category != fixed_category:
                overrides.append({"shot_id": shot_id, "from_category": category, "to_category": fixed_category})
            continue
        # opening/ending 只能由 first_shot_id/last_shot_id 绑定，不能通过普通
        # shot_categories 把首尾模板扩散到中间镜头。
        if category in {"opening", "ending"}:
            overrides.append({"shot_id": shot_id, "from_category": category, "to_category": ""})
            continue
        targets.append((shot_id, category, shot_segments))

    # 首尾模板最后加入，但只绑定各自边界镜头，保证它们不会影响中间镜头。
    for fixed_category, shot_id in (("opening", first_shot_id), ("ending", last_shot_id)):
        if fixed_by_shot.get(shot_id) == fixed_category:
            targets.append((shot_id, fixed_category, segments_by_shot[shot_id]))
    return targets, overrides


def _apply_packaging_application(
    packaging_application: Mapping[str, Any] | None,
    *,
    segments_by_shot: Mapping[str, Sequence[Any]],
    ordered_visual_segments: Sequence[Any],
    segment_track_names: Mapping[int, str] | None = None,
    video_scene_effect_type: Any,
    filter_type: Any | None = None,
    transition_type: Any,
    keyframe_property: Any,
    script: Any | None = None,
    effect_track_type: Any | None = None,
    filter_track_type: Any | None = None,
    audio_segments: Sequence[tuple[str, Any]] = (),
    audio_effect_types: Sequence[Any] = (),
    audio_track_type: Any | None = None,
    audio_material_type: Any | None = None,
    audio_segment_type: Any | None = None,
    timerange_type: Any | None = None,
    draft_dir: Path | None = None,
) -> dict[str, Any]:
    """将包装包参数应用到目标片段，并恢复可复用的独立包装轨道。"""

    report: dict[str, Any] = {
        "enabled": bool(packaging_application),
        "bundle_name": "",
        "version": "",
        "opening_title": str(packaging_application.get("opening_title") or "").strip()[:12]
        if isinstance(packaging_application, Mapping)
        else "",
        "applied_categories": [],
        "applied_effects": [],
        "effect_material_bindings": [],
        "effect_overrides": dict(packaging_application.get("effect_overrides") or {})
        if isinstance(packaging_application, Mapping)
        and isinstance(packaging_application.get("effect_overrides"), Mapping)
        else {},
        "effect_tracks": [],
        "overall_effects": [],
        "overall_filters": [],
        "full_draft_style_source": "",
        "subtitle_style": {},
        "applied_keyframes": 0,
        "applied_transitions": 0,
        "replaced_transitions": 0,
        "applied_audio_clips": [],
        "applied_audio_effects": [],
        "applied_audio_fades": [],
        "audio_tracks": [],
        "text_templates": [],
        "opening_duration_us": 0,
        "ending_duration_us": 0,
        "protected_visual_ranges": [],
        "suppressed_caption_segments": 0,
        "transition_source": "none",
        "transition_policy": {
            "level_2": {
                "default_enabled": True,
                "audio_companion": True,
                "audio_source": "from_shot_category_template",
            },
            "level_3": {
                "default_enabled": False,
                "audio_companion": False,
                "audio_source": "none",
            },
        },
        "transition_points": [],
        "transition_warnings": [],
        "transition_audio_companions": [],
        "priority_overrides": [],
        "skipped": [],
    }
    if not packaging_application:
        return report
    report["bundle_name"] = str(packaging_application.get("bundle_name") or "")
    report["version"] = str(packaging_application.get("version") or "")
    category_paths = packaging_application.get("categories")
    if not isinstance(category_paths, Mapping) or not isinstance(packaging_application.get("shot_categories"), Mapping):
        report["skipped"].append("包装包缺少分类映射")
        return report

    transition_items: list[tuple[str, Mapping[str, Any], int, Sequence[Any]]] = []
    targets, priority_overrides = _resolve_packaging_targets(
        packaging_application,
        segments_by_shot=segments_by_shot,
    )
    # 同一镜头如果同时有首帧参考和主画面，只把包装数据投影到主画面。
    # 这一步必须在分类目标解析后做，才能同时覆盖片头、片尾和普通分类。
    targets = [
        (shot_id, category, _select_primary_visual_segments(target_segments, segment_track_names))
        for shot_id, category, target_segments in targets
    ]
    primary_ordered_visual_segments = _select_primary_visual_segments(
        ordered_visual_segments,
        segment_track_names,
    )
    timeline_duration = max(
        [
            int(getattr(getattr(segment, "target_timerange", None), "end", 0) or 0)
            for segment in primary_ordered_visual_segments
        ]
        or [int(getattr(script, "duration", 0) or 0)]
    )

    def category_duration(category: str) -> int:
        raw_spec = category_paths.get(category)
        if not isinstance(raw_spec, Mapping):
            return 0
        try:
            path = Path(str(raw_spec.get("draft_content_path") or "")).expanduser().resolve()
            return max(0, int(_load_packaging_template(path).get("duration") or 0))
        except (EditingLayerError, TypeError, ValueError):
            return 0

    protected_ranges: list[tuple[int, int, str]] = []
    opening_duration = category_duration("opening")
    ending_duration = category_duration("ending")
    if opening_duration > 0 and timeline_duration > 0:
        opening_end = min(timeline_duration, opening_duration)
        protected_ranges.append((0, opening_end, "opening"))
        report["opening_duration_us"] = opening_end
    if ending_duration > 0 and timeline_duration > 0:
        ending_start = max(0, timeline_duration - ending_duration)
        protected_ranges.append((ending_start, timeline_duration, "ending"))
        report["ending_duration_us"] = timeline_duration - ending_start
    report["protected_visual_ranges"] = [
        {"start_us": start, "end_us": end, "category": category}
        for start, end, category in protected_ranges
    ]

    def is_protected(start: int, end: int) -> bool:
        return any(start < protected_end and end > protected_start for protected_start, protected_end, _ in protected_ranges)

    def unprotected_windows(start: int, duration: int) -> list[tuple[int, int]]:
        """从全片范围中扣除片头/片尾保护区。"""

        end = max(start, start + duration)
        windows = [(start, end)]
        for protected_start, protected_end, _ in protected_ranges:
            next_windows: list[tuple[int, int]] = []
            for window_start, window_end in windows:
                if window_end <= protected_start or window_start >= protected_end:
                    next_windows.append((window_start, window_end))
                    continue
                if window_start < protected_start:
                    next_windows.append((window_start, protected_start))
                if protected_end < window_end:
                    next_windows.append((protected_end, window_end))
            windows = next_windows
        return [(window_start, window_end - window_start) for window_start, window_end in windows if window_end > window_start]

    expanded_targets: list[tuple[str, str, Sequence[Any]]] = []
    for shot_id, category, target_segments in targets:
        if category == "opening" and opening_duration > 0:
            target_segments = [
                segment for segment in primary_ordered_visual_segments
                if int(getattr(getattr(segment, "target_timerange", None), "start", 0) or 0) < opening_duration
                and int(getattr(getattr(segment, "target_timerange", None), "end", 0) or 0) > 0
            ]
        elif category == "ending" and ending_duration > 0:
            ending_start = max(0, timeline_duration - ending_duration)
            target_segments = [
                segment for segment in primary_ordered_visual_segments
                if int(getattr(getattr(segment, "target_timerange", None), "end", 0) or 0) > ending_start
                and int(getattr(getattr(segment, "target_timerange", None), "start", 0) or 0) < timeline_duration
            ]
        elif protected_ranges:
            # 普通镜头分类不能进入片头/片尾保护区；片头结束后的下一个
            # 镜头从保护区外重新开始使用自己的分类包装。
            target_segments = [
                segment for segment in target_segments
                if not is_protected(
                    int(getattr(getattr(segment, "target_timerange", None), "start", 0) or 0),
                    int(getattr(getattr(segment, "target_timerange", None), "end", 0) or 0),
                )
            ]
        expanded_targets.append((shot_id, category, target_segments))
    targets = expanded_targets
    report["priority_overrides"].extend(priority_overrides)

    category_labels = {
        "opening": "片头",
        "image": "图片",
        "digital_human": "数字人",
        "aigc": "AIGC",
        "explanation": "说明镜头",
        "mixed_explanation": "混合说明镜头",
        "ending": "片尾",
    }
    effect_track_names: dict[tuple[str, str, int], str] = {}
    audio_track_names: dict[tuple[str, str], str] = {}
    audio_specs_by_category: dict[
        str, tuple[list[tuple[Mapping[str, Any], int, int, Mapping[str, Any]]], int, Path]
    ] = {}
    loaded_templates: dict[str, tuple[dict[str, Any], Path]] = {}
    full_draft_style = packaging_application.get("full_draft_style")
    full_draft_source_value = packaging_application.get("full_draft_source")
    # 兼容服务重启、旧任务交接或中间序列化时没有平铺 source path 的情况。
    # 完整风格分析自身已经保存了只读源草稿路径，这里优先从那里恢复。
    if not str(full_draft_source_value or "").strip() and isinstance(full_draft_style, Mapping):
        source_info = full_draft_style.get("source")
        if isinstance(source_info, Mapping):
            full_draft_source_value = source_info.get("path")
    full_draft_source = Path(str(full_draft_source_value or "")).expanduser()
    full_template: dict[str, Any] | None = None
    full_source_duration = 0
    if isinstance(full_draft_style, Mapping) and full_draft_source.is_dir():
        try:
            full_template = _load_packaging_template(full_draft_source)
            source_info = full_draft_style.get("source")
            if isinstance(source_info, Mapping):
                full_source_duration = int(source_info.get("duration_us") or 0)
            full_source_duration = max(full_source_duration, int(full_template.get("duration") or 0))
            report["full_draft_style_source"] = str(full_draft_source.resolve())
        except (EditingLayerError, TypeError, ValueError):
            full_template = None

    def add_effect_track_segment(
        category: str,
        effect: Any,
        effect_name: str,
        source_track_index: int,
        target_start: int,
        target_duration: int,
    ) -> tuple[bool, str]:
        if not script or effect_track_type is None or timerange_type is None:
            return False, ""
        base_key = (category, effect_name, source_track_index)
        track_name = effect_track_names.get(base_key)
        if not track_name:
            track_name = f"包装-{category_labels.get(category, category)}-{effect_name}"
            suffix = 2
            while track_name in getattr(script, "tracks", {}):
                track_name = f"包装-{category_labels.get(category, category)}-{effect_name}-{suffix}"
                suffix += 1
            try:
                # 剪映原生可见特效轨使用 11000 左右的渲染层，
                # 比默认 effect 轨的 10000 更靠上，避免被底层视觉轨遮住。
                script.add_track(effect_track_type, track_name=track_name, relative_index=1000)
            except TypeError:
                # 测试替身或旧版小助手没有 relative_index 参数时保留兼容。
                script.add_track(effect_track_type, track_name=track_name)
            effect_track_names[base_key] = track_name
            report["effect_tracks"].append(track_name)
        try:
            script.add_effect(
                effect,
                timerange_type(target_start, target_duration),
                track_name=track_name,
            )
            track = getattr(script, "tracks", {}).get(track_name)
            segments = getattr(track, "segments", []) if track is not None else []
            runtime_id = str(getattr(segments[-1], "material_id", "") or "") if segments else ""
            return True, runtime_id
        except (TypeError, ValueError) as exc:
            # 同一特效在不同目标片段上发生重叠时，创建一个带序号的独立轨道，
            # 仍保持分类和特效名称可见，不退回到视频片段内隐式挂载。
            suffix = 2
            while True:
                retry_name = f"{track_name}-{suffix}"
                if retry_name not in getattr(script, "tracks", {}):
                    try:
                        script.add_track(effect_track_type, track_name=retry_name, relative_index=1000 + suffix)
                    except TypeError:
                        script.add_track(effect_track_type, track_name=retry_name)
                    try:
                        script.add_effect(effect, timerange_type(target_start, target_duration), track_name=retry_name)
                        report["effect_tracks"].append(retry_name)
                        track = getattr(script, "tracks", {}).get(retry_name)
                        segments = getattr(track, "segments", []) if track is not None else []
                        runtime_id = str(getattr(segments[-1], "material_id", "") or "") if segments else ""
                        return True, runtime_id
                    except (TypeError, ValueError):
                        pass
                suffix += 1
                if suffix > 32:
                    report["skipped"].append(f"{category}/{effect_name} 独立特效轨道写入失败：{exc}")
                    return False, ""

    def add_filter_track_segment(
        filter_meta: Any,
        filter_name: str,
        target_start: int,
        target_duration: int,
        intensity: float = 100.0,
    ) -> bool:
        if not script or filter_type is None or filter_track_type is None or timerange_type is None:
            return False
        track_name = "包装-全片滤镜"
        if track_name not in getattr(script, "tracks", {}):
            script.add_track(filter_track_type, track_name=track_name)
            report["effect_tracks"].append(track_name)
        try:
            script.add_filter(
                filter_meta,
                timerange_type(target_start, target_duration),
                track_name=track_name,
                intensity=float(intensity),
            )
            report["overall_filters"].append({"name": filter_name, "target_start": target_start, "target_duration": target_duration})
            return True
        except (TypeError, ValueError) as exc:
            report["skipped"].append(f"全片滤镜/{filter_name} 写入失败：{exc}")
            return False

    def add_audio_clip(
        category: str,
        material: Mapping[str, Any],
        source_start: int,
        source_clip_duration: int,
        template_duration: int,
        fade: Mapping[str, Any],
        target_start: int,
        target_duration: int,
        template_path: Path,
    ) -> None:
        if not script or audio_track_type is None or audio_material_type is None or audio_segment_type is None or timerange_type is None:
            return
        source_path = _template_audio_path(template_path, material)
        if source_path is None:
            report["skipped"].append(f"{category} 音效文件不存在：{material.get('name')}")
            return
        try:
            copied_path = (draft_dir or template_path.parent) / "assets" / "packaging_audio" / category / source_path.name
            copied_path.parent.mkdir(parents=True, exist_ok=True)
            if not copied_path.is_file():
                shutil.copy2(source_path, copied_path)
            audio_material = audio_material_type(str(copied_path))
            clip_start = target_start + min(
                target_duration,
                round(source_start / max(1, template_duration) * target_duration),
            )
            clip_duration = max(1, round(source_clip_duration / max(1, template_duration) * target_duration))
            clip_duration = min(target_start + target_duration - clip_start, max(1, clip_duration))
            if clip_duration <= 0:
                return
            track_key = (category, str(material.get("name") or source_path.stem))
            track_name = audio_track_names.get(track_key)
            if not track_name:
                track_name = f"包装-{category_labels.get(category, category)}音频-{material.get('name') or source_path.stem}"
                suffix = 2
                while track_name in getattr(script, "tracks", {}):
                    track_name = f"包装-{category_labels.get(category, category)}音频-{material.get('name') or source_path.stem}-{suffix}"
                    suffix += 1
                script.add_track(audio_track_type, track_name=track_name)
                audio_track_names[track_key] = track_name
                report["audio_tracks"].append(track_name)
            source_duration_us = max(1, min(int(audio_material.duration), clip_duration))
            source_volume = material.get("_packaging_source_volume")
            try:
                source_volume = 1.0 if source_volume is None else max(0.0, min(2.0, float(source_volume)))
            except (TypeError, ValueError):
                source_volume = 1.0
            audio_segment = audio_segment_type(
                audio_material,
                timerange_type(clip_start, clip_duration),
                source_timerange=timerange_type(0, source_duration_us),
                volume=source_volume,
            )
            if fade:
                fade_in = max(0, round(int(fade.get("fade_in_duration") or 0) / max(1, source_clip_duration) * clip_duration))
                fade_out = max(0, round(int(fade.get("fade_out_duration") or 0) / max(1, source_clip_duration) * clip_duration))
                if fade_in or fade_out:
                    audio_segment.add_fade(fade_in, fade_out)
                    report["applied_audio_fades"].append({"category": category, "name": material.get("name")})
            script.add_segment(audio_segment, track_name=track_name)
            report["applied_audio_clips"].append({"category": category, "name": material.get("name"), "track": track_name})
        except (OSError, TypeError, ValueError) as exc:
            report["skipped"].append(f"{category}/{material.get('name')} 音效写入失败：{exc}")

    def attach_audio_effects(
        audio_track_name: str,
        audio_segment: Any,
        effect_items: Sequence[Mapping[str, Any]],
        category: str,
        template_path: Path,
    ) -> None:
        """把源音频片段的音频特效挂到目标片段，并登记运行时素材。"""

        if not script or not audio_effect_types or not hasattr(audio_segment, "add_effect"):
            return
        existing_refs = {
            str(ref)
            for ref in (getattr(audio_segment, "extra_material_refs", []) or [])
            if str(ref)
        }
        for item in effect_items:
            effect_name = str(item.get("name") or item.get("resource_id") or "").strip()
            if not effect_name:
                continue
            if "人声" in effect_name and audio_track_name != "解说":
                continue
            audio_effect = next(
                (
                    _enum_from_template_item(enum_type, item)
                    for enum_type in audio_effect_types
                    if _enum_from_template_item(enum_type, item) is not None
                ),
                None,
            )
            if audio_effect is None:
                report["skipped"].append(f"{category} 未支持音频特效：{effect_name}")
                continue
            effect_id = str(getattr(audio_effect, "effect_id", "") or "")
            if effect_id and effect_id in existing_refs:
                continue
            wanted_ids = {
                str(item.get(key) or "").strip()
                for key in ("resource_id", "effect_id", "id")
                if str(item.get(key) or "").strip()
            }
            if any(
                str(getattr(existing, "name", "") or "").strip().lower() == effect_name.lower()
                or str(getattr(existing, "effect_id", "") or "").strip() in wanted_ids
                for existing in getattr(audio_segment, "effects", [])
            ):
                report["applied_audio_effects"].append({
                    "category": category,
                    "name": effect_name,
                    "track": audio_track_name,
                    "effect_id": effect_id,
                    "template_path": str(template_path),
                    "status": "already_present",
                })
                continue
            try:
                audio_segment.add_effect(audio_effect)
                effect_instance = next(
                    (
                        effect
                        for effect in getattr(audio_segment, "effects", [])
                        if str(getattr(effect, "effect_id", ""))
                        and str(getattr(effect, "effect_id", "")) in getattr(audio_segment, "extra_material_refs", [])
                    ),
                    None,
                )
                registered_effects = getattr(getattr(script, "materials", None), "audio_effects", None)
                if effect_instance is not None and isinstance(registered_effects, list):
                    if effect_instance not in registered_effects:
                        registered_effects.append(effect_instance)
                existing_refs.add(str(getattr(effect_instance or audio_effect, "effect_id", "") or ""))
                report["applied_audio_effects"].append({
                    "category": category,
                    "name": effect_name,
                    "track": audio_track_name,
                    "effect_id": str(getattr(effect_instance or audio_effect, "effect_id", "") or ""),
                    "template_path": str(template_path),
                })
            except (TypeError, ValueError, AttributeError) as exc:
                report["skipped"].append(f"{category}/{audio_track_name}/{effect_name} 音频特效写入失败：{exc}")

    def add_transition_audio(
        category: str,
        target_segment: Any,
        template_duration: int,
        audio_specs: Sequence[tuple[Mapping[str, Any], int, int, Mapping[str, Any]]],
        template_path: Path,
        point: Mapping[str, Any] | None = None,
    ) -> None:
        """只把分类模板的音频片段挂到实际应用的转场接缝。"""

        target_timerange = getattr(target_segment, "target_timerange", None)
        target_start = int(getattr(target_timerange, "start", 0) or 0)
        target_duration = int(getattr(target_timerange, "duration", 0) or 0)
        if target_duration <= 0 or not audio_specs:
            return
        before = len(report["applied_audio_clips"])
        for material, audio_start, audio_duration, fade in audio_specs:
            add_audio_clip(
                category,
                material,
                audio_start,
                audio_duration,
                template_duration,
                fade,
                target_start,
                target_duration,
                template_path,
            )
        companions = report["applied_audio_clips"][before:]
        if not companions:
            return
        for companion in companions:
            companion.update({
                "transition_index": point.get("transition_index") if point else None,
                "transition_at_us": point.get("at_us") if point else target_start + target_duration,
                "classification_level": str(point.get("classification_level") or "level_2") if point else "fixed_boundary",
            })
        report["transition_audio_companions"].extend(companions)

    for shot_id, category, target_segments in targets:
        raw_spec = category_paths.get(category)
        if not isinstance(raw_spec, Mapping):
            continue
        try:
            template_path = Path(str(raw_spec.get("draft_content_path") or "")).expanduser().resolve()
            if category in loaded_templates:
                template, template_path = loaded_templates[category]
            else:
                template = _load_packaging_template(template_path)
                loaded_templates[category] = (template, template_path)
        except EditingLayerError as exc:
            report["skipped"].append(str(exc))
            continue
        template_segments = _template_visual_segments(template)
        source_segment = next(
            (item for item in template_segments if _template_keyframe_groups(item)),
            template_segments[0] if template_segments else {},
        )
        source_range = source_segment.get("target_timerange") if isinstance(source_segment, Mapping) else {}
        source_duration = int(source_range.get("duration") or template.get("duration") or 0) if isinstance(source_range, Mapping) else int(template.get("duration") or 0)
        if source_duration <= 0:
            report["skipped"].append(f"{category} 模板缺少有效片段时长")
            continue
        if category not in report["applied_categories"]:
            report["applied_categories"].append(category)

        materials = template.get("materials") if isinstance(template.get("materials"), Mapping) else {}
        transition_items.extend(
            (category, item, source_duration, target_segments)
            for item in materials.get("transitions", [])
            if isinstance(item, Mapping)
        )
        effect_specs = _template_effect_specs(template, video_scene_effect_type)
        audio_specs = _template_audio_specs(template)
        audio_effect_items = _template_audio_effect_items(template)
        if audio_specs:
            audio_specs_by_category[category] = (audio_specs, source_duration, template_path)

        keyframe_groups = _template_keyframe_groups(source_segment) if isinstance(source_segment, Mapping) else []
        if category == "opening" and opening_duration > 0:
            effect_windows = [(0, min(opening_duration, timeline_duration))]
        elif category == "ending" and ending_duration > 0:
            effect_windows = [(max(0, timeline_duration - ending_duration), min(ending_duration, timeline_duration))]
        else:
            effect_windows = [
                (
                    int(getattr(getattr(target, "target_timerange", None), "start", 0) or 0),
                    int(getattr(getattr(target, "target_timerange", None), "duration", 0) or 0),
                )
                for target in target_segments
            ]
        for effect_window_start, effect_window_duration in effect_windows:
            if effect_window_duration <= 0:
                continue
            for effect, effect_name, effect_start, effect_duration, source_track_index, template_material_id in effect_specs:
                relative_start = min(effect_window_duration, round(effect_start / source_duration * effect_window_duration))
                relative_duration = min(
                    effect_window_duration - relative_start,
                    max(1, round(effect_duration / source_duration * effect_window_duration)),
                )
                effect_added, runtime_material_id = add_effect_track_segment(
                    category,
                    effect,
                    effect_name,
                    source_track_index,
                    effect_window_start + relative_start,
                    relative_duration,
                )
                if effect_added:
                    report["applied_effects"].append({"category": category, "shot_id": shot_id, "name": effect_name})
                    if runtime_material_id and template_material_id:
                        report["effect_material_bindings"].append(
                            {
                                "runtime_material_id": runtime_material_id,
                                "template_material_id": template_material_id,
                                "template_path": str(template_path),
                            }
                        )
        for target in target_segments:
            target_duration = int(getattr(getattr(target, "target_timerange", None), "duration", 0) or 0)
            if target_duration <= 0:
                continue
            target_start = int(getattr(getattr(target, "target_timerange", None), "start", 0) or 0)
            for group in keyframe_groups:
                if not isinstance(group, Mapping):
                    continue
                property_name = str(group.get("property_type") or "").strip()
                property_value = next((item for item in keyframe_property if item.value == property_name), None)
                if property_value is None:
                    report["skipped"].append(f"{category} 未支持关键帧属性：{property_name}")
                    continue
                for point in group.get("keyframe_list", []):
                    if not isinstance(point, Mapping):
                        continue
                    values = point.get("values")
                    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)) or not values:
                        continue
                    source_offset = max(0, int(point.get("time_offset") or 0))
                    target_offset = min(target_duration, round(source_offset / source_duration * target_duration))
                    try:
                        target.add_keyframe(property_value, target_offset, float(values[0]))
                        report["applied_keyframes"] += 1
                    except (TypeError, ValueError) as exc:
                        report["skipped"].append(f"{category}/{shot_id} 关键帧写入失败：{exc}")

            if audio_segments and audio_effect_types and timerange_type is not None:
                target_end = target_start + target_duration
                for audio_track_name, audio_segment in audio_segments:
                    audio_start = int(getattr(getattr(audio_segment, "target_timerange", None), "start", 0) or 0)
                    audio_end = int(getattr(getattr(audio_segment, "target_timerange", None), "end", 0) or 0)
                    if audio_end <= target_start or audio_start >= target_end:
                        continue
                    for item in audio_effect_items:
                        effect_name = str(item.get("name") or item.get("resource_id") or "")
                        if "人声" in effect_name and audio_track_name != "解说":
                            continue
                        attach_audio_effects(
                            audio_track_name,
                            audio_segment,
                            [item],
                            category,
                            template_path,
                        )

            template_texts = materials.get("text_templates")
            if isinstance(template_texts, Sequence) and not isinstance(template_texts, (str, bytes, bytearray)):
                for text_template in template_texts:
                    if isinstance(text_template, Mapping):
                        report["text_templates"].append({
                            "category": category,
                            "name": text_template.get("name") or text_template.get("resource_id"),
                            "template_path": str(template_path),
                            "target_start": target_start,
                            "target_duration": target_duration,
                        })

    # 完整目标草稿记录的是全片级信息，不能只停留在治理页面的摘要里。
    # 分类母版负责镜头级效果；这里把整体效果、音效、文字模板和转场的
    # 相对时间真正投影到新脚本时间线上。
    current_duration = max(
        [
            int(getattr(getattr(segment, "target_timerange", None), "end", 0) or 0)
            for segment in primary_ordered_visual_segments
        ]
        or [int(getattr(script, "duration", 0) or 0)]
    )
    if full_template is not None and full_source_duration > 0 and current_duration > 0:
        full_materials = full_template.get("materials") if isinstance(full_template.get("materials"), Mapping) else {}
        full_visual = full_draft_style.get("visual") if isinstance(full_draft_style, Mapping) else {}
        full_subtitles = full_draft_style.get("subtitles") if isinstance(full_draft_style, Mapping) else {}
        profiles = full_subtitles.get("profiles") if isinstance(full_subtitles, Mapping) else []
        if isinstance(profiles, Sequence) and not isinstance(profiles, (str, bytes, bytearray)):
            dominant = max(
                (item for item in profiles if isinstance(item, Mapping)),
                key=lambda item: int(item.get("count") or 0),
                default=None,
            )
            if isinstance(dominant, Mapping) and isinstance(dominant.get("profile"), Mapping):
                report["subtitle_style"] = dict(dominant["profile"])
        overall_effects = (
            full_visual.get("overall_effects", [])
            if isinstance(full_visual, Mapping)
            else []
        )
        if not isinstance(overall_effects, Sequence) or isinstance(overall_effects, (str, bytes, bytearray)):
            overall_effects = []
        overall_ids = {
            str((item.get("material") or {}).get(key) or "")
            for item in overall_effects
            if isinstance(item, Mapping) and isinstance(item.get("material"), Mapping)
            for key in ("id", "effect_id", "resource_id")
            if str((item.get("material") or {}).get(key) or "")
        }
        overall_names = {
            str((item.get("material") or {}).get("name") or "").strip().lower()
            for item in overall_effects
            if isinstance(item, Mapping) and isinstance(item.get("material"), Mapping)
            and str((item.get("material") or {}).get("name") or "").strip()
        }
        full_effect_specs = _template_effect_specs(full_template, video_scene_effect_type)
        for effect, effect_name, source_start, effect_duration, source_track_index, template_material_id in full_effect_specs:
            if overall_ids and not (
                str(template_material_id or "") in overall_ids
                or str(effect_name or "").strip().lower() in overall_names
            ):
                continue
            target_start = min(current_duration, round(source_start / full_source_duration * current_duration))
            target_duration = min(
                current_duration - target_start,
                max(1, round(effect_duration / full_source_duration * current_duration)),
            )
            for window_start, window_duration in unprotected_windows(target_start, target_duration):
                effect_added, runtime_material_id = add_effect_track_segment(
                    "全片", effect, effect_name, source_track_index, window_start, window_duration
                )
                if effect_added:
                    report["overall_effects"].append({
                        "name": effect_name,
                        "target_start": window_start,
                        "target_duration": window_duration,
                    })
                    if runtime_material_id and template_material_id:
                        report["effect_material_bindings"].append({
                            "runtime_material_id": runtime_material_id,
                            "template_material_id": template_material_id,
                            "template_path": str(full_draft_source),
                        })

        filter_materials = _template_material_map(full_template, "filters")
        overall_filters = full_visual.get("overall_filters") if isinstance(full_visual, Mapping) else []
        for descriptor in overall_filters if isinstance(overall_filters, Sequence) else []:
            if not isinstance(descriptor, Mapping) or not isinstance(descriptor.get("material"), Mapping):
                continue
            material_descriptor = descriptor["material"]
            source_id = next(
                (
                    str(material_descriptor.get(key) or "")
                    for key in ("id", "effect_id", "resource_id")
                    if str(material_descriptor.get(key) or "") in filter_materials
                ),
                "",
            )
            source_filter = filter_materials.get(source_id)
            if source_filter is None:
                wanted_name = str(material_descriptor.get("name") or "").strip().lower()
                source_filter = next(
                    (item for item in filter_materials.values() if str(item.get("name") or "").strip().lower() == wanted_name),
                    None,
                )
            if source_filter is None:
                continue
            filter_meta = _enum_from_template_item(filter_type, source_filter)
            if filter_meta is None:
                report["skipped"].append(f"全片滤镜未在剪映小助手枚举中找到：{source_filter.get('name')}")
                continue
            for window_start, window_duration in unprotected_windows(0, current_duration):
                if add_filter_track_segment(filter_meta, str(source_filter.get("name") or ""), window_start, window_duration):
                    report["overall_filters"][-1]["source"] = str(full_draft_source)

        full_audio_specs = _template_audio_specs(full_template)
        for material, audio_start, audio_duration, fade in full_audio_specs:
            add_audio_clip(
                "全片", material, audio_start, audio_duration, full_source_duration, fade,
                0, current_duration, full_draft_source,
            )
        # 完整草稿中的音频特效通常挂在“解说”等有名称的音频轨上，
        # _template_audio_specs 会刻意排除这些内容素材轨，因此这里单独按
        # 全片相对时间投影，不能只复制独立音效文件。
        for effect_spec in _template_audio_effect_specs(full_template):
            source_start = int(effect_spec.get("start_us") or 0)
            source_duration = int(effect_spec.get("duration_us") or 0)
            projected_start = min(
                current_duration,
                round(source_start / max(1, full_source_duration) * current_duration),
            )
            projected_duration = min(
                current_duration - projected_start,
                max(1, round(source_duration / max(1, full_source_duration) * current_duration)),
            )
            projected_end = projected_start + projected_duration
            source_track_name = str(effect_spec.get("track_name") or "").strip()
            for target_track_name, target_audio_segment in audio_segments:
                if source_track_name and str(target_track_name or "").strip() != source_track_name:
                    continue
                target_timerange = getattr(target_audio_segment, "target_timerange", None)
                target_start = int(getattr(target_timerange, "start", 0) or 0)
                target_end = int(getattr(target_timerange, "end", 0) or 0)
                if target_end <= projected_start or target_start >= projected_end:
                    continue
                attach_audio_effects(
                    str(target_track_name or source_track_name),
                    target_audio_segment,
                    effect_spec.get("effects", []),
                    "全片",
                    full_draft_source,
                )
        full_transition_style = full_draft_style.get("transitions") if isinstance(full_draft_style, Mapping) else {}
        full_audio_companions = (
            full_transition_style.get("audio_companions", [])
            if isinstance(full_transition_style, Mapping)
            else []
        )
        # 完整草稿的音效已经按全片相对时间写入独立音频轨；这里把源草稿
        # 对应的转场音效关系同步到报告，便于交付页和后续校检追踪。
        for companion in full_audio_companions:
            if isinstance(companion, Mapping):
                report["transition_audio_companions"].append({
                    **dict(companion),
                    "category": "全片",
                    "source": "full_draft_style",
                })

        # 完整草稿的片头文字模板优先于某个分类母版的文字模板；标题内容
        # 仍由新脚本 opening_title 注入，不复制旧视频文字内容。
        full_text_templates = full_materials.get("text_templates") if isinstance(full_materials, Mapping) else []
        if isinstance(full_text_templates, Sequence) and not isinstance(full_text_templates, (str, bytes, bytearray)) and full_text_templates:
            target_duration = min(2_000_000, current_duration)
            report["text_templates"] = [{
                "category": "opening",
                "name": item.get("name") or item.get("resource_id"),
                "template_path": str(full_draft_source),
                "target_start": 0,
                "target_duration": target_duration,
            } for item in full_text_templates if isinstance(item, Mapping)]

        full_transition_items = full_materials.get("transitions") if isinstance(full_materials, Mapping) else []
        transition_items.extend(
            ("全片", item, full_source_duration, primary_ordered_visual_segments)
            for item in full_transition_items
            if isinstance(item, Mapping)
        )

    selected_transitions: list[tuple[str, Mapping[str, Any], int, Sequence[Any]]] = []
    for fixed_category in ("opening", "ending"):
        fixed_transition = next(
            (item for item in transition_items if item[0] == fixed_category),
            None,
        )
        if fixed_transition is not None:
            selected_transitions.append(fixed_transition)

    upstream_points = packaging_application.get("transition_points")
    raw_transition_points = [
        item for item in upstream_points
        if isinstance(item, Mapping)
        and str(item.get("from_shot_id") or "").strip()
        and str(item.get("to_shot_id") or "").strip()
    ] if isinstance(upstream_points, Sequence) and not isinstance(upstream_points, (str, bytes, bytearray)) else []
    if not raw_transition_points and full_template is not None and full_source_duration > 0 and current_duration > 0:
        full_transitions = full_draft_style.get("transitions") if isinstance(full_draft_style, Mapping) else {}
        for index, item in enumerate(
            full_transitions.get("applied", []) if isinstance(full_transitions, Mapping) else []
        ):
            if not isinstance(item, Mapping):
                continue
            try:
                source_at = int(item.get("transition_at_us") or 0)
            except (TypeError, ValueError):
                continue
            projected_at = min(current_duration, round(source_at / full_source_duration * current_duration))
            # 只在主画面轨道上匹配，首帧参考轨道不能参与转场定位。
            candidates = [
                segment for segment in primary_ordered_visual_segments
                if 0 < int(getattr(getattr(segment, "target_timerange", None), "end", 0) or 0) < current_duration
            ]
            if not candidates:
                continue
            target_segment = min(
                candidates,
                key=lambda segment: abs(
                    int(getattr(getattr(segment, "target_timerange", None), "end", 0) or 0) - projected_at
                ),
            )
            target_index = next((idx for idx, segment in enumerate(primary_ordered_visual_segments) if id(segment) == id(target_segment)), -1)
            if target_index < 0 or target_index + 1 >= len(primary_ordered_visual_segments):
                continue
            from_shot_id = next(
                (shot_id for shot_id, segments in segments_by_shot.items() if any(id(segment) == id(target_segment) for segment in segments)),
                "",
            )
            to_segment = primary_ordered_visual_segments[target_index + 1]
            to_shot_id = next(
                (shot_id for shot_id, segments in segments_by_shot.items() if any(id(segment) == id(to_segment) for segment in segments)),
                "",
            )
            if from_shot_id and to_shot_id:
                raw_transition_points.append({
                    "transition_index": index,
                    "at_us": projected_at,
                    "from_shot_id": from_shot_id,
                    "to_shot_id": to_shot_id,
                    "classification_level": "level_2",
                    "default_enabled": True,
                    "transition_duration_us": max(0, int(item.get("transition_duration_us") or 0)),
                    # 内部对象只用于本次写入，不进入 JSON 报告，避免再次按
                    # shot_id 在重复轨道中找错目标。
                    "_target_segment": target_segment,
                })
        if raw_transition_points:
            report["transition_source"] = "full_draft_style_relative_fallback"
    transition_points = []
    for point in raw_transition_points:
        if _transition_point_enabled(point):
            transition_points.append(point)
        else:
            report["skipped"].append(
                f"三级镜头分类接缝 {point.get('transition_index', '')} 默认无转场"
            )
    has_upstream_transition_points = bool(raw_transition_points)
    ordinary_transitions_by_category: dict[str, tuple[str, Mapping[str, Any], int, Sequence[Any]]] = {}
    for item in transition_items:
        if item[0] not in {"opening", "ending"} and item[0] not in ordinary_transitions_by_category:
            ordinary_transitions_by_category[item[0]] = item
    ordinary_transition = next(iter(ordinary_transitions_by_category.values()), None)
    if has_upstream_transition_points and ordinary_transition is not None:
        declared_transition_source = str(
            packaging_application.get("transition_timeline_source") or ""
        ).strip()
        report["transition_source"] = (
            declared_transition_source
            if declared_transition_source and declared_transition_source != "none"
            else str(report.get("transition_source") or "upstream.transition_points")
        )
    elif ordinary_transition is not None:
        # 历史任务没有上游接缝信息时保留旧兜底，但明确记录为 fallback，
        # 便于交付页识别这不是精确的镜头接缝定位。
        report["transition_source"] = "legacy_first_visual_segment_fallback"

    def apply_transition(
        category: str,
        transition_item: Mapping[str, Any],
        source_duration: int,
        target_segment: Any,
        point: Mapping[str, Any] | None = None,
    ) -> bool:
        transition = _enum_from_template_item(transition_type, transition_item)
        if transition is not None:
            source_transition_duration = int(transition_item.get("duration") or 0)
            target_segment_duration = int(
                getattr(getattr(target_segment, "target_timerange", None), "duration", 0) or 0
            )
            if target_segment is None or target_segment_duration <= 0:
                report["skipped"].append(f"{category} 转场缺少有效目标片段")
                return False
            explicit_duration = 0
            if point is not None:
                try:
                    explicit_duration = int(point.get("transition_duration_us") or 0)
                except (TypeError, ValueError):
                    explicit_duration = 0
            if explicit_duration > 0:
                # 上游/完整草稿已经给出微秒时长时，按该绝对时长写入；
                # 不再拿整部视频时长做分母，避免 1 秒转场被压成几十毫秒。
                target_duration = min(explicit_duration, max(1, target_segment_duration // 2))
            else:
                target_duration = min(
                    max(100_000, round(source_transition_duration / source_duration * target_segment_duration))
                    if source_duration > 0 and source_transition_duration > 0
                    else 500_000,
                    max(1, target_segment_duration // 2),
                )
            try:
                existing_transition = getattr(target_segment, "transition", None)
                if existing_transition is not None:
                    # 分类模板和完整视频可能同时命中同一接缝。完整视频的
                    # 全片转场应覆盖普通分类转场；但片头/片尾固定转场优先，
                    # 不能被全片规则反向覆盖。
                    protects_boundary = (
                        category == "全片"
                        and point is not None
                        and str(point.get("from_shot_id") or "")
                        in {
                            str(packaging_application.get("first_shot_id") or ""),
                            str(packaging_application.get("last_shot_id") or ""),
                        }
                        and any(item[0] in {"opening", "ending"} for item in transition_items)
                    )
                    if protects_boundary:
                        report["skipped"].append(
                            f"{category} 转场接缝属于片头/片尾固定范围，保留优先级更高的固定转场"
                        )
                        return False
                    global_id = str(getattr(existing_transition, "global_id", "") or "")
                    refs = getattr(target_segment, "extra_material_refs", None)
                    if isinstance(refs, list) and global_id:
                        refs[:] = [ref for ref in refs if str(ref) != global_id]
                    target_segment.transition = None
                    report.setdefault("replaced_transitions", 0)
                    report["replaced_transitions"] += 1
                target_segment.add_transition(transition, duration=max(1, target_duration))
                report["applied_transitions"] += 1
                target_timerange = getattr(target_segment, "target_timerange", None)
                actual_boundary_us = int(getattr(target_timerange, "end", 0) or 0)
                expected_boundary_us = None
                boundary_delta_us = None
                boundary_status = "not_applicable"
                if point is not None:
                    try:
                        expected_boundary_us = int(round(float(point.get("at_us"))))
                    except (TypeError, ValueError):
                        expected_boundary_us = None
                    if expected_boundary_us is not None and expected_boundary_us >= 0:
                        boundary_delta_us = abs(actual_boundary_us - expected_boundary_us)
                        tolerance_us = int(
                            packaging_application.get("transition_tolerance_us")
                            or TRANSITION_POINT_TOLERANCE_US
                        )
                        boundary_status = (
                            "exact"
                            if boundary_delta_us == 0
                            else "within_tolerance"
                            if boundary_delta_us <= tolerance_us
                            else "shot_pair_authoritative"
                        )
                        if boundary_status == "shot_pair_authoritative":
                            report["transition_warnings"].append(
                                f"转场点 {point.get('transition_index', '')} 的 at_us 与前置镜头边界相差 "
                                f"{boundary_delta_us} 微秒，已按 from_shot_id/to_shot_id 继续应用"
                            )
                record = {
                    "transition_index": point.get("transition_index") if point else None,
                    "at_us": point.get("at_us") if point else None,
                    "from_shot_id": point.get("from_shot_id") if point else None,
                    "to_shot_id": point.get("to_shot_id") if point else None,
                    "category": category,
                    "name": str(transition_item.get("name") or transition_item.get("resource_id") or ""),
                    "target_track_name": str(
                        (segment_track_names or {}).get(id(target_segment)) or ""
                    ),
                    "target_start_us": int(getattr(target_timerange, "start", 0) or 0),
                    "expected_boundary_us": expected_boundary_us,
                    "actual_boundary_us": actual_boundary_us,
                    "boundary_delta_us": boundary_delta_us,
                    "boundary_status": boundary_status,
                    "tolerance_us": int(
                        packaging_application.get("transition_tolerance_us")
                        or TRANSITION_POINT_TOLERANCE_US
                    ) if point is not None else None,
                    "target_duration_us": target_segment_duration,
                    "applied_duration_us": max(1, target_duration),
                    "position_source": "upstream_transition_point" if point else "boundary_or_legacy_fallback",
                }
                report["transition_points"].append(record)
            except (TypeError, ValueError) as exc:
                report["skipped"].append(f"转场写入失败：{exc}")
                return False
        else:
            report["skipped"].append("包装包转场未在剪映小助手枚举中找到")
            return False
        return True

    for category, transition_item, source_duration, target_segments in selected_transitions:
        if category == "ending":
            target_segment = target_segments[-1] if target_segments else None
        elif category == "opening":
            target_segment = target_segments[0] if target_segments else None
        else:
            target_segment = primary_ordered_visual_segments[0] if primary_ordered_visual_segments else None
        applied = apply_transition(category, transition_item, source_duration, target_segment)
        audio_context = audio_specs_by_category.get(category)
        if applied and audio_context:
            audio_specs, template_duration, template_path = audio_context
            add_transition_audio(
                category,
                target_segment,
                template_duration,
                audio_specs,
                template_path,
            )

    if ordinary_transition is not None:
        if transition_points:
            for point in transition_points:
                from_shot_id = str(point.get("from_shot_id") or "").strip()
                target_segments = _select_primary_visual_segments(
                    segments_by_shot.get(from_shot_id, ()),
                    segment_track_names,
                )
                target_segment = point.get("_target_segment") if isinstance(point, Mapping) else None
                if target_segment is None:
                    target_segment = _select_transition_segment(target_segments, point)
                if target_segment is None:
                    report["skipped"].append(
                        f"上游转场点 {point.get('transition_index', '')} 找不到前置镜头：{from_shot_id}"
                    )
                    continue
                category = str(
                    (packaging_application.get("shot_categories") or {}).get(from_shot_id) or ""
                )
                selected_transition = ordinary_transitions_by_category.get(category) or ordinary_transition
                selected_category, transition_item, source_duration, _ordinary_targets = selected_transition
                if apply_transition(selected_category, transition_item, source_duration, target_segment, point):
                    audio_context = audio_specs_by_category.get(selected_category)
                    if audio_context:
                        audio_specs, template_duration, template_path = audio_context
                        add_transition_audio(
                            selected_category,
                            target_segment,
                            template_duration,
                            audio_specs,
                            template_path,
                            point,
                        )
        elif not selected_transitions and not has_upstream_transition_points:
            # 没有首尾模板且没有上游接缝信息时，保留历史行为。
            category, transition_item, source_duration, _ordinary_targets = ordinary_transition
            target_segment = primary_ordered_visual_segments[0] if primary_ordered_visual_segments else None
            if apply_transition(category, transition_item, source_duration, target_segment):
                audio_context = audio_specs_by_category.get(category)
                if audio_context:
                    audio_specs, template_duration, template_path = audio_context
                    add_transition_audio(
                        category,
                        target_segment,
                        template_duration,
                        audio_specs,
                        template_path,
                    )
    return report


def write_windows_native_draft(
    plan: EditingLayerPlan,
    *,
    capcut_mate_root: str | Path,
    draft_root: str | Path,
    draft_name: str,
    replace: bool = False,
    settings: Mapping[str, Any] | None = None,
    packaging_application: Mapping[str, Any] | None = None,
    draft_folder_factory: Callable[[str], Any] | None = None,
) -> WindowsNativeDraftResult:
    """使用剪映小助手项目创建并写入 Windows 原生草稿。

    ``draft_folder_factory`` 只用于测试注入；生产调用不注入它，直接从
    ``capcut_mate_root/src`` 导入项目自己的 ``DraftFolder``。
    """

    validate_windows_native_plan(plan)
    effective_settings = normalize_editing_settings(settings)
    capcut_root = Path(capcut_mate_root).expanduser().resolve()
    target_root = Path(draft_root).expanduser().resolve()
    if not capcut_root.is_dir():
        raise EditingLayerError(f"找不到剪映小助手项目：{capcut_root}")
    if not target_root.is_dir():
        raise EditingLayerError(f"找不到剪映草稿目录：{target_root}")
    if not draft_name.strip() or any(char in draft_name for char in '<>:"/\\|?*'):
        raise EditingLayerError("草稿名称为空或含 Windows 不允许的字符")

    _add_capcut_src_to_path(capcut_root)
    if draft_folder_factory is None:
        try:
            from pyJianYingDraft import DraftFolder  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - 取决于本机安装环境
            raise EditingLayerError(
                f"剪映小助手无法加载 pyJianYingDraft.DraftFolder：{type(exc).__name__}: {exc}"
            ) from exc
        draft_folder_factory = DraftFolder

    draft_dir = target_root / draft_name
    if draft_dir.exists() and not replace:
        raise EditingLayerError(f"草稿已存在：{draft_dir}；需要覆盖时显式传入 replace=True")

    try:
        from pyJianYingDraft import (  # type: ignore[import-not-found]
        AudioSegment,
        AudioMaterial,
        ClipSettings,
        TextSegment,
        TextStyle,
        TextBorder,
        Timerange,
        TrackType,
        VideoMaterial,
        VideoSegment,
    )
        from pyJianYingDraft import FontType
        from pyJianYingDraft import (
            AudioSceneEffectType,
            FilterType,
            KeyframeProperty,
            TransitionType,
            VideoSceneEffectType,
        )
        from pyJianYingDraft.metadata import SpeechToSongType, ToneEffectType
    except ImportError as exc:  # pragma: no cover - 取决于本机安装环境
        raise EditingLayerError(
            f"剪映小助手无法加载 pyJianYingDraft 组件：{type(exc).__name__}: {exc}"
        ) from exc

    folder = draft_folder_factory(str(target_root))
    script = folder.create_draft(
        draft_name,
        width=plan.width,
        height=plan.height,
        fps=plan.fps,
        maintrack_adsorb=False,
        allow_replace=replace,
    )
    if script.content.get("platform", {}).get("os") != "windows":
        raise EditingLayerError("DraftFolder.create_draft 返回的不是 Windows 草稿，已停止写入")
    if script.content.get("last_modified_platform", {}).get("os") != "windows":
        raise EditingLayerError("DraftFolder.create_draft 的 last_modified_platform 不是 Windows")

    # 只更新草稿身份，不覆盖项目模板提供的平台字段。
    script.content["id"] = str(uuid.uuid4()).upper()
    script.content["name"] = draft_name
    script.content["source"] = "default"
    script.content["create_time"] = int(time.time())
    script.content["update_time"] = int(time.time())

    copied = _copy_native_assets(plan, draft_dir)
    track_types = {
        "首帧参考": TrackType.video,
        "AIGC动画": TrackType.video,
        "主角占位": TrackType.video,
        "解说": TrackType.audio,
        "BGM": TrackType.audio,
        "音效": TrackType.audio,
        "字幕": TrackType.text,
    }
    for track in plan.tracks:
        script.add_track(track_types[track["track_name"]], track_name=track["track_name"])

    video_segments: dict[str, list[Any]] = defaultdict(list)
    segments_by_shot: dict[str, list[Any]] = defaultdict(list)
    segment_track_names: dict[int, str] = {}
    ordered_visual_segments: list[Any] = []
    visual_items: list[tuple[Mapping[str, Any], Any]] = []
    audio_segments: list[tuple[str, Any]] = []
    for item in plan.native_items:
        path = copied[item["asset_id"]]
        original_start_us = int(item["start_us"])
        original_end_us = int(item["end_us"])
        start_us, duration_us = _jianying_target_window(original_start_us, original_end_us, plan.fps)
        if duration_us <= 0:
            raise EditingLayerError(f"{item['asset_id']} 的剪辑时长必须大于 0")
        track_name = item["track_name"]
        if item["asset_type"] == "audio":
            # asset_duration_us 来自素材交接层，可能是生成时长的估计值；
            # 剪映小助手会按本地文件再次解析媒体时长，必须使用同一个真实值
            # 计算 source_timerange，否则稍长的 BGM/音频会直接越界失败。
            material = AudioMaterial(str(path))
            source_duration_us = _bounded_source_duration_us(item, int(material.duration))
            if track_name == "解说":
                volume = effective_settings["narration_volume"] / 100.0
            elif track_name == "BGM":
                volume = effective_settings["bgm_volume"] / 100.0
            else:
                volume = effective_settings["sfx_volume"] / 100.0
            segment = AudioSegment(
                material,
                Timerange(start_us, duration_us),
                source_timerange=Timerange(0, source_duration_us),
                volume=volume,
            )
            script.add_segment(segment, track_name=track_name)
            audio_segments.append((track_name, segment))
        else:
            material = VideoMaterial(str(path))
            source_duration_us = _bounded_source_duration_us(item, int(material.duration))
            segment = VideoSegment(
                material,
                Timerange(start_us, duration_us),
                source_timerange=Timerange(0, source_duration_us),
            )
            video_segments.setdefault(track_name, []).append(segment)
            segment_track_names[id(segment)] = track_name
            ordered_visual_segments.append(segment)
            visual_items.append((item, segment))
            for shot_id in item.get("shot_ids", []):
                segments_by_shot[str(shot_id)].append(segment)

    # 所有素材片段都完成后，只应用一次包装包。若放在上面的素材循环中，
    # 每个镜头都会重复创建整套音频、特效、关键帧和转场轨道。
    packaging_report = _apply_packaging_application(
        packaging_application,
        segments_by_shot=segments_by_shot,
        ordered_visual_segments=ordered_visual_segments,
        segment_track_names=segment_track_names,
        video_scene_effect_type=VideoSceneEffectType,
        filter_type=FilterType,
        transition_type=TransitionType,
        keyframe_property=KeyframeProperty,
        script=script,
        effect_track_type=TrackType.effect,
        filter_track_type=TrackType.filter,
        audio_segments=audio_segments,
        audio_effect_types=(AudioSceneEffectType, ToneEffectType, SpeechToSongType),
        audio_track_type=TrackType.audio,
        audio_material_type=AudioMaterial,
        audio_segment_type=AudioSegment,
        timerange_type=Timerange,
        draft_dir=draft_dir,
    )
    automatic_image_zoom = _apply_primary_image_zoom_keyframes(
        visual_items,
        segment_track_names=segment_track_names,
        keyframe_property=KeyframeProperty,
    )
    packaging_report["automatic_image_zoom"] = automatic_image_zoom
    packaging_report["applied_keyframes"] = int(packaging_report.get("applied_keyframes") or 0) + int(
        automatic_image_zoom.get("keyframe_count") or 0
    )
    # 基础音频片段在包装应用前已经写入轨道；包装音效属于后补属性，
    # 这里按 effect_id 做一次最终登记，确保 draft_content.json 的 materials.audio_effects 不丢失。
    registered_audio_effect_ids = {
        str(getattr(effect, "effect_id", ""))
        for effect in getattr(script.materials, "audio_effects", [])
    }
    for _track_name, audio_segment in audio_segments:
        for effect in getattr(audio_segment, "effects", []):
            effect_id = str(getattr(effect, "effect_id", ""))
            if effect_id and effect_id not in registered_audio_effect_ids:
                script.materials.audio_effects.append(effect)
                registered_audio_effect_ids.add(effect_id)

    for track_name, segments in video_segments.items():
        for index, segment in enumerate(segments):
            script.add_segment(segment, track_name=track_name)

    try:
        font_type = FontType.from_name(effective_settings["font"])
    except ValueError as exc:
        raise EditingLayerError(f"无法解析字幕字体：{effective_settings['font']}") from exc
    alignment_y = -0.78
    text_style = TextStyle(
        size=effective_settings["caption_size"],
        bold=True,
        color=_color_rgb(effective_settings["caption_color"]),
        align=DEFAULT_CAPTION_ALIGNMENT,
        line_spacing=effective_settings["line_spacing"],
        auto_wrapping=True,
        max_line_width=0.84,
    )
    text_border = None
    if effective_settings["border_width"] > 0:
        text_border = TextBorder(
            color=_color_rgb(effective_settings["border_color"]),
            width=effective_settings["border_width"],
        )
    caption_settings = ClipSettings(transform_y=alignment_y)
    if packaging_report.get("text_templates"):
        text_track_name = "包装-片头文字模板"
        if text_track_name not in script.tracks:
            script.add_track(TrackType.text, track_name=text_track_name)
        opening_title = packaging_report.get("opening_title") or next(
            (
                format_caption_display_text(str(caption.get("text") or "")).replace("\n", " ")
                for caption in plan.captions
                if str(caption.get("text") or "").strip()
            ),
            "标题",
        )
        first_template = packaging_report["text_templates"][0]
        template_start = max(0, int(first_template.get("target_start") or 0))
        template_duration = min(2_000_000, max(1, int(first_template.get("target_duration") or 1)))
        script.add_segment(
            TextSegment(
                opening_title,
                Timerange(template_start, template_duration),
                font=font_type,
                style=text_style,
                border=text_border,
                clip_settings=ClipSettings(transform_y=0.0),
            ),
            track_name=text_track_name,
        )
    for caption in plan.captions:
        caption_start, caption_duration = _jianying_target_window(
            int(caption["start"]), int(caption["end"]), plan.fps
        )
        text_segment = TextSegment(
            format_caption_display_text(caption["text"]),
            Timerange(caption_start, caption_duration),
            font=font_type,
            style=text_style,
            border=text_border,
            clip_settings=caption_settings,
        )
        script.add_segment(text_segment, track_name="字幕")

    script.save()
    _write_local_meta(draft_dir, draft_name)
    content_path = draft_dir / "draft_content.json"
    if not content_path.is_file():
        raise EditingLayerError("剪映小助手保存后没有生成 draft_content.json")
    content = json.loads(content_path.read_text(encoding="utf-8"))
    injected_subtitle_style = _inject_packaging_subtitle_style(
        content,
        Path(str(packaging_report.get("full_draft_style_source") or "")),
    ) if packaging_report.get("full_draft_style_source") else 0
    injected_video_effects = _inject_packaging_video_effects(
        content,
        packaging_report.get("effect_material_bindings", []),
        packaging_report.get("effect_overrides", {}),
    )
    injected_audio_effects = _inject_packaging_audio_effects(
        content,
        packaging_report.get("applied_audio_effects", []),
    )
    injected_text_templates = _inject_packaging_text_templates(
        content,
        packaging_report.get("text_templates", []),
        packaging_report.get("opening_title")
        or next(
            (
                str(caption.get("text") or "").strip()
                for caption in plan.captions
                if str(caption.get("text") or "").strip()
            ),
            "标题",
        ),
    )
    suppressed_caption_segments = _suppress_subtitles_in_protected_ranges(
        content,
        packaging_report.get("protected_visual_ranges", []),
    )
    packaging_report["suppressed_caption_segments"] = suppressed_caption_segments
    if injected_subtitle_style or injected_video_effects or injected_audio_effects or injected_text_templates["templates"] or suppressed_caption_segments:
        content_path.write_text(json.dumps(content, ensure_ascii=False, indent=4), encoding="utf-8")
        draft_info_path = draft_dir / "draft_info.json"
        if draft_info_path.is_file():
            draft_info_path.write_text(json.dumps(content, ensure_ascii=False, indent=4), encoding="utf-8")
        if injected_video_effects:
            packaging_report["applied_video_effect_materials"] = injected_video_effects
        if injected_subtitle_style:
            packaging_report["applied_subtitle_style_materials"] = injected_subtitle_style
        if injected_audio_effects:
            packaging_report["applied_audio_effect_materials"] = injected_audio_effects
        if injected_text_templates["templates"]:
            packaging_report["applied_text_templates"] = injected_text_templates
    platform_os = content.get("platform", {}).get("os", "")
    last_modified_os = content.get("last_modified_platform", {}).get("os", "")
    if platform_os != "windows" or last_modified_os != "windows":
        raise EditingLayerError("最终草稿平台标记不是 Windows")
    all_paths = [
        Path(item.get("path", ""))
        for kind in ("videos", "images", "audios")
        for item in content.get("materials", {}).get(kind, [])
        if isinstance(item, Mapping)
    ]
    media_paths_exist = bool(all_paths) and all(item.is_file() for item in all_paths)
    if not media_paths_exist:
        raise EditingLayerError("最终草稿存在媒体路径缺失")
    _validate_written_native_windows(plan, content)
    tracks = [
        {"track_type": item.get("type"), "track_name": item.get("name"), "item_count": len(item.get("segments", []))}
        for item in content.get("tracks", [])
        if isinstance(item, Mapping)
    ]
    materials = content.get("materials", {}) if isinstance(content.get("materials"), Mapping) else {}
    effects_count = sum(
        len(materials.get(key, []))
        for key in ("video_effects", "filters", "material_animations")
        if isinstance(materials.get(key), list)
    )
    transitions_count = len(materials.get("transitions", [])) if isinstance(materials.get("transitions"), list) else 0
    edit_manifest = _build_manifest_after_write(
        plan,
        draft_dir,
        content,
        status="DRAFT_STRUCTURALLY_VALID",
        validation={
            "effects_enabled": effects_count > 0,
            "transitions_enabled": transitions_count > 0,
            "packaging_enabled": bool(packaging_report.get("enabled")),
            "packaging_applied_effects": len(packaging_report.get("applied_effects", [])),
            "packaging_applied_keyframes": int(packaging_report.get("applied_keyframes") or 0),
            "packaging_applied_transitions": int(packaging_report.get("applied_transitions") or 0),
        },
    )
    return WindowsNativeDraftResult(
        draft_path=str(draft_dir),
        platform_os=platform_os,
        last_modified_platform_os=last_modified_os,
        duration_us=int(content.get("duration", 0)),
        tracks=tracks,
        media_paths_exist=media_paths_exist,
        effects_count=effects_count,
        transitions_count=transitions_count,
        edit_manifest=edit_manifest,
        packaging_report=packaging_report,
    )


__all__ = [
    "DEFAULT_FPS",
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "DEFAULT_EDITING_SETTINGS",
    "EDIT_VERSION",
    "EditingLayerError",
    "EditingLayerPlan",
    "WindowsNativeDraftResult",
    "compile_editing_layer",
    "normalize_editing_settings",
    "validate_windows_native_plan",
    "write_windows_native_draft",
]
