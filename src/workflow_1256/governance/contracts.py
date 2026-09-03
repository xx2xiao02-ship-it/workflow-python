"""1256 正式治理契约。

这里的对象是跨模块的数据底座，不替换原 Coze 节点输出。原字段通过
provenance 保存，治理对象通过稳定 ID 关联。计划时间线、素材实际时长和
剪辑写入时间线始终分开保存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class GovernanceContractError(ValueError):
    """治理数据不符合主键、时间线、状态或引用契约。"""


TIMELINE_UNIT = "microseconds"
STORY_REVIEW_STATES = {"PENDING_USER_REVIEW", "APPROVED", "REJECTED"}
DIRECTOR_STATES = {
    "DIRECTOR_DRAFT",
    "TTS_RUNNING",
    "TTS_READY",
    "TIMELINE_READY",
    "SHOTS_READY",
    "REQUIREMENTS_READY",
    "DIRECTOR_LOCKED",
    "DIRECTOR_FAILED",
}
ASSET_STATES = {
    "PENDING",
    "CREATING",
    "PROCESSING",
    "SUCCEEDED",
    "VALIDATED",
    "FAILED_RETRYABLE",
    "FAILED_FINAL",
    "BLOCKED",
    "SKIPPED_OPTIONAL",
    "OBSERVED_REDACTED",
    "ASSETS_READY",
    "ASSETS_READY_WITH_WARNINGS",
}
EDIT_STATES = {
    "EDIT_PLAN_READY",
    "COMPILING",
    "DRAFT_WRITTEN",
    "DRAFT_STRUCTURALLY_VALID",
    "DRAFT_VALIDATED",
    "EDIT_FAILED",
    "EDIT_MAPPING_ONLY",
}
ALLOWED_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "director": {
        "DIRECTOR_DRAFT": {"TTS_RUNNING", "DIRECTOR_FAILED"},
        "TTS_RUNNING": {"TTS_READY", "DIRECTOR_FAILED"},
        "TTS_READY": {"TIMELINE_READY", "DIRECTOR_FAILED"},
        "TIMELINE_READY": {"SHOTS_READY", "DIRECTOR_FAILED"},
        "SHOTS_READY": {"REQUIREMENTS_READY", "DIRECTOR_FAILED"},
        "REQUIREMENTS_READY": {"DIRECTOR_LOCKED", "DIRECTOR_FAILED"},
        "DIRECTOR_LOCKED": {"DIRECTOR_DRAFT"},
        "DIRECTOR_FAILED": {"DIRECTOR_DRAFT", "TTS_RUNNING"},
    },
    "asset": {
        "PENDING": {"CREATING", "BLOCKED", "SKIPPED_OPTIONAL"},
        "CREATING": {"PROCESSING", "SUCCEEDED", "FAILED_RETRYABLE", "FAILED_FINAL"},
        "PROCESSING": {"SUCCEEDED", "FAILED_RETRYABLE", "FAILED_FINAL"},
        "SUCCEEDED": {"VALIDATED", "FAILED_RETRYABLE", "FAILED_FINAL"},
        "VALIDATED": {"CREATING"},
        "FAILED_RETRYABLE": {"CREATING", "FAILED_FINAL"},
        "FAILED_FINAL": {"CREATING"},
        "BLOCKED": {"PENDING", "CREATING"},
    "SKIPPED_OPTIONAL": {"PENDING"},
        "OBSERVED_REDACTED": {"SUCCEEDED", "VALIDATED", "BLOCKED"},
    },
    "edit": {
        "EDIT_PLAN_READY": {"COMPILING", "EDIT_FAILED"},
        "COMPILING": {"DRAFT_WRITTEN", "EDIT_FAILED"},
        "DRAFT_WRITTEN": {"DRAFT_STRUCTURALLY_VALID", "EDIT_FAILED"},
        "DRAFT_STRUCTURALLY_VALID": {"DRAFT_VALIDATED", "EDIT_FAILED"},
        "DRAFT_VALIDATED": {"COMPILING"},
    "EDIT_FAILED": {"EDIT_PLAN_READY", "COMPILING"},
        "EDIT_MAPPING_ONLY": {"EDIT_PLAN_READY", "COMPILING"},
    },
}


def _string(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise GovernanceContractError(f"{field_name} 必须是字符串")
    return value


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GovernanceContractError(f"{field_name} 必须是非负整数")
    return value


def assert_transition(entity: str, current: str, target: str) -> None:
    """检查状态机跳转，不修改状态。"""

    if entity not in ALLOWED_TRANSITIONS:
        raise GovernanceContractError(f"未知状态机：{entity}")
    if current not in ALLOWED_TRANSITIONS[entity]:
        raise GovernanceContractError(f"{entity} 当前状态无效：{current}")
    if target not in ALLOWED_TRANSITIONS[entity][current]:
        raise GovernanceContractError(f"禁止状态跳转：{entity} {current} -> {target}")


@dataclass(frozen=True)
class TimeWindow:
    start_us: int
    end_us: int

    def __post_init__(self) -> None:
        start = _nonnegative_int(self.start_us, "start_us")
        end = _nonnegative_int(self.end_us, "end_us")
        if end <= start:
            raise GovernanceContractError("时间线 end_us 必须大于 start_us")

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    def contains(self, other: "TimeWindow") -> bool:
        return self.start_us <= other.start_us and other.end_us <= self.end_us

    def overlaps(self, other: "TimeWindow") -> bool:
        return self.start_us < other.end_us and other.start_us < self.end_us

    def to_dict(self) -> dict[str, int]:
        return {
            "start_us": self.start_us,
            "end_us": self.end_us,
            "duration_us": self.duration_us,
        }


@dataclass(frozen=True)
class CaptionRecord:
    caption_id: str
    group_id: str
    text: str
    timeline: TimeWindow
    source_node: str = "capcut_stt"

    def __post_init__(self) -> None:
        _string(self.caption_id, "caption_id")
        _string(self.group_id, "group_id")
        _string(self.text, "caption.text")

    def to_dict(self) -> dict[str, Any]:
        return {
            "caption_id": self.caption_id,
            "group_id": self.group_id,
            "text": self.text,
            "timeline": self.timeline.to_dict(),
            "source_node": self.source_node,
        }


@dataclass(frozen=True)
class DirectorGroup:
    group_id: str
    group_index: int
    segment_text: str
    timeline: TimeWindow
    shot_ids: list[str]
    caption_ids: list[str]
    narration_requirement_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _string(self.group_id, "group_id")
        _nonnegative_int(self.group_index, "group_index")
        _string(self.segment_text, "segment_text")
        if not self.shot_ids or not all(isinstance(item, str) for item in self.shot_ids):
            raise GovernanceContractError(f"{self.group_id}.shot_ids 必须是非空字符串数组")
        if not all(isinstance(item, str) for item in self.caption_ids):
            raise GovernanceContractError(f"{self.group_id}.caption_ids 必须是字符串数组")
        _string(self.narration_requirement_id, f"{self.group_id}.narration_requirement_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "group_index": self.group_index,
            "segment_text": self.segment_text,
            "timeline": self.timeline.to_dict(),
            "shot_ids": list(self.shot_ids),
            "caption_ids": list(self.caption_ids),
            "narration_requirement_id": self.narration_requirement_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DirectorShot:
    shot_id: str
    group_id: str
    shot_index: int
    source_text: str
    clip_role: str
    story_beat: str
    timeline: TimeWindow
    route_candidates: list[str]
    requirement_ids: list[str]
    production_spec: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in (
            ("shot_id", self.shot_id),
            ("group_id", self.group_id),
            ("source_text", self.source_text),
            ("clip_role", self.clip_role),
            ("story_beat", self.story_beat),
        ):
            _string(value, field_name)
        _nonnegative_int(self.shot_index, "shot_index")
        if not isinstance(self.route_candidates, list) or not all(
            isinstance(item, str) for item in self.route_candidates
        ):
            raise GovernanceContractError(f"{self.shot_id}.route_candidates 必须是字符串数组")
        if not self.requirement_ids or not all(isinstance(item, str) for item in self.requirement_ids):
            raise GovernanceContractError(f"{self.shot_id}.requirement_ids 必须是非空字符串数组")
        if not isinstance(self.production_spec, dict):
            raise GovernanceContractError(f"{self.shot_id}.production_spec 必须是对象")

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "group_id": self.group_id,
            "shot_index": self.shot_index,
            "source_text": self.source_text,
            "clip_role": self.clip_role,
            "story_beat": self.story_beat,
            "timeline": self.timeline.to_dict(),
            "route_candidates": list(self.route_candidates),
            "requirement_ids": list(self.requirement_ids),
            "production_spec": dict(self.production_spec),
        }


@dataclass(frozen=True)
class MaterialRequirement:
    requirement_id: str
    scope: str
    asset_type: str
    role: str
    group_id: str | None
    shot_ids: list[str]
    mandatory: bool
    route: str
    coverage_policy: str
    source_node: str
    timeline: TimeWindow | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _string(self.requirement_id, "requirement_id")
        if self.scope not in {"shot", "group", "project"}:
            raise GovernanceContractError(f"{self.requirement_id}.scope 无效")
        for field_name, value in (("asset_type", self.asset_type), ("role", self.role), ("route", self.route), ("coverage_policy", self.coverage_policy), ("source_node", self.source_node)):
            _string(value, f"{self.requirement_id}.{field_name}")
        if self.scope == "shot" and len(self.shot_ids) != 1:
            raise GovernanceContractError(f"{self.requirement_id} 的 shot 需求必须绑定一个 shot_id")
        if self.scope == "group" and not self.group_id:
            raise GovernanceContractError(f"{self.requirement_id} 的 group 需求缺少 group_id")
        if self.scope == "project" and self.group_id is not None:
            raise GovernanceContractError(f"{self.requirement_id} 的 project 需求不应绑定 group_id")
        if not all(isinstance(item, str) for item in self.shot_ids):
            raise GovernanceContractError(f"{self.requirement_id}.shot_ids 必须是字符串数组")

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "scope": self.scope,
            "asset_type": self.asset_type,
            "role": self.role,
            "group_id": self.group_id,
            "shot_ids": list(self.shot_ids),
            "mandatory": self.mandatory,
            "route": self.route,
            "coverage_policy": self.coverage_policy,
            "source_node": self.source_node,
            "timeline": self.timeline.to_dict() if self.timeline else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DirectorLockedManifest:
    project_id: str
    run_id: str
    plan_version: str
    tts_fingerprint: str
    total_timeline: TimeWindow
    groups: list[DirectorGroup]
    shots: list[DirectorShot]
    captions: list[CaptionRecord]
    requirements: list[MaterialRequirement]
    provenance: list[dict[str, Any]]
    status: str = "DIRECTOR_LOCKED"
    cinematic_story: dict[str, Any] = field(default_factory=dict)
    story_review_status: str = "APPROVED"
    story_revision: int = 1
    narration_assets: list[dict[str, Any]] = field(default_factory=list)
    subtitle_pipeline: dict[str, Any] = field(default_factory=dict)
    digital_human_max_shot_ratio: float = 0.15
    sound_effect_plan: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name, value in (("project_id", self.project_id), ("run_id", self.run_id), ("plan_version", self.plan_version), ("tts_fingerprint", self.tts_fingerprint)):
            _string(value, field_name)
        if self.status != "DIRECTOR_LOCKED":
            raise GovernanceContractError("正式跨模块输入必须是 DIRECTOR_LOCKED")
        if self.story_review_status not in STORY_REVIEW_STATES:
            raise GovernanceContractError("story_review_status 无效")
        if self.story_review_status != "APPROVED":
            raise GovernanceContractError("未经过用户同意的故事不能进入 DirectorLockedManifest")
        if isinstance(self.story_revision, bool) or not isinstance(self.story_revision, int) or self.story_revision < 1:
            raise GovernanceContractError("story_revision 必须是正整数")
        if isinstance(self.digital_human_max_shot_ratio, bool) or not isinstance(self.digital_human_max_shot_ratio, (int, float)) or not 0 <= self.digital_human_max_shot_ratio <= 1:
            raise GovernanceContractError("digital_human_max_shot_ratio 必须在 0 到 1 之间")
        if not isinstance(self.cinematic_story, dict):
            raise GovernanceContractError("cinematic_story 必须是对象")
        if not isinstance(self.subtitle_pipeline, dict):
            raise GovernanceContractError("subtitle_pipeline 必须是对象")
        if not self.groups or not self.shots:
            raise GovernanceContractError("DirectorLockedManifest 不能没有 groups 或 shots")
        self._validate_groups_and_shots()
        self._validate_captions()
        self._validate_requirements()
        self._validate_narration_assets()
        self._validate_sound_effect_plan()

    def _validate_sound_effect_plan(self) -> None:
        shot_map = {shot.shot_id: shot for shot in self.shots}
        seen: set[str] = set()
        for item in self.sound_effect_plan:
            if not isinstance(item, dict):
                raise GovernanceContractError("sound_effect_plan 项必须是对象")
            sid = item.get("shot_id")
            if not isinstance(sid, str) or sid not in shot_map or sid in seen:
                raise GovernanceContractError("sound_effect_plan shot_id 必须唯一且对应冻结镜头")
            start, end = item.get("cue_start_us"), item.get("cue_end_us")
            if not isinstance(start, int) or not isinstance(end, int) or end <= start:
                raise GovernanceContractError(f"{sid} 音效时间线无效")
            if start < shot_map[sid].timeline.start_us or end > shot_map[sid].timeline.end_us:
                raise GovernanceContractError(f"{sid} 音效时间线越界")
            seen.add(sid)

    def _validate_groups_and_shots(self) -> None:
        group_ids = [item.group_id for item in self.groups]
        shot_ids = [item.shot_id for item in self.shots]
        if len(set(group_ids)) != len(group_ids) or len(set(shot_ids)) != len(shot_ids):
            raise GovernanceContractError("group_id 或 shot_id 重复")
        if group_ids != [f"g{index + 1:02d}" for index in range(len(group_ids))]:
            raise GovernanceContractError("groups 必须按 g01、g02 顺序排列")
        by_group = {group.group_id: group for group in self.groups}
        shots_by_group: dict[str, list[DirectorShot]] = {group_id: [] for group_id in group_ids}
        for shot in self.shots:
            if shot.group_id not in by_group:
                raise GovernanceContractError(f"{shot.shot_id} 引用了不存在的 group_id")
            shots_by_group[shot.group_id].append(shot)
        for group in self.groups:
            if group.group_index != group_ids.index(group.group_id):
                raise GovernanceContractError(f"{group.group_id}.group_index 与 groups 顺序不一致")
            group_shots = shots_by_group[group.group_id]
            expected = [f"{group.group_id}_s{index + 1:02d}" for index in range(len(group_shots))]
            if [shot.shot_id for shot in group_shots] != expected:
                raise GovernanceContractError(f"{group.group_id} 的 shot_id 或顺序不连续")
            if group.shot_ids != expected:
                raise GovernanceContractError(f"{group.group_id}.shot_ids 与 shots 不一致")
            if group.timeline.start_us < self.total_timeline.start_us or not self.total_timeline.contains(group.timeline):
                raise GovernanceContractError(f"{group.group_id} 超出总时间线")
            previous_end = group.timeline.start_us
            for expected_index, shot in enumerate(group_shots):
                if shot.shot_index != expected_index:
                    raise GovernanceContractError(f"{shot.shot_id}.shot_index 与 shots 顺序不一致")
                if shot.timeline.start_us != previous_end:
                    raise GovernanceContractError(f"{shot.shot_id} 与前一镜头不连续")
                if not group.timeline.contains(shot.timeline):
                    raise GovernanceContractError(f"{shot.shot_id} 超出所属大镜头")
                previous_end = shot.timeline.end_us
            if previous_end != group.timeline.end_us:
                raise GovernanceContractError(f"{group.group_id} 的小镜头没有覆盖完整大镜头")
        previous_group_end = self.total_timeline.start_us
        for group in self.groups:
            if group.timeline.start_us != previous_group_end:
                raise GovernanceContractError("大镜头时间线存在空洞或重叠")
            previous_group_end = group.timeline.end_us
        if previous_group_end != self.total_timeline.end_us:
            raise GovernanceContractError("大镜头没有覆盖完整总时间线")

    def _validate_captions(self) -> None:
        group_map = {group.group_id: group for group in self.groups}
        caption_ids = [caption.caption_id for caption in self.captions]
        if len(set(caption_ids)) != len(caption_ids):
            raise GovernanceContractError("caption_id 重复")
        for caption in self.captions:
            group = group_map.get(caption.group_id)
            if group is None or not group.timeline.contains(caption.timeline):
                raise GovernanceContractError(f"{caption.caption_id} 超出所属大镜头")
        actual_by_group = {group_id: [] for group_id in group_map}
        for caption in self.captions:
            actual_by_group[caption.group_id].append(caption.caption_id)
        for group in self.groups:
            if group.caption_ids != actual_by_group[group.group_id]:
                raise GovernanceContractError(f"{group.group_id}.caption_ids 与 captions 不一致")

    def _validate_requirements(self) -> None:
        group_ids = {group.group_id for group in self.groups}
        shot_ids = {shot.shot_id for shot in self.shots}
        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(set(requirement_ids)) != len(requirement_ids):
            raise GovernanceContractError("requirement_id 重复")
        for requirement in self.requirements:
            if requirement.group_id and requirement.group_id not in group_ids:
                raise GovernanceContractError(f"{requirement.requirement_id} 引用了不存在的 group_id")
            if not set(requirement.shot_ids).issubset(shot_ids):
                raise GovernanceContractError(f"{requirement.requirement_id} 引用了不存在的 shot_id")
            if requirement.timeline and not self.total_timeline.contains(requirement.timeline):
                raise GovernanceContractError(f"{requirement.requirement_id} 超出总时间线")
        for shot in self.shots:
            if not set(shot.requirement_ids).issubset(set(requirement_ids)):
                raise GovernanceContractError(f"{shot.shot_id} 引用了不存在的 requirement_id")

    def _validate_narration_assets(self) -> None:
        group_map = {group.group_id: group for group in self.groups}
        seen: set[str] = set()
        for index, asset in enumerate(self.narration_assets):
            if not isinstance(asset, dict):
                raise GovernanceContractError(f"narration_assets[{index}] 必须是对象")
            asset_id = asset.get("asset_id")
            group_id = asset.get("group_id")
            timeline = asset.get("timeline")
            if not isinstance(asset_id, str) or not asset_id.strip() or asset_id in seen:
                raise GovernanceContractError("narration_assets.asset_id 必须非空且唯一")
            if not isinstance(group_id, str) or group_id not in group_map:
                raise GovernanceContractError(f"narration_assets[{index}].group_id 无效")
            if not isinstance(timeline, dict):
                raise GovernanceContractError(f"narration_assets[{index}].timeline 必须是对象")
            start = timeline.get("start", timeline.get("start_us"))
            end = timeline.get("end", timeline.get("end_us"))
            if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int):
                raise GovernanceContractError(f"narration_assets[{index}].timeline 必须使用整数微秒")
            window = TimeWindow(start, end)
            if not group_map[group_id].timeline.contains(window):
                raise GovernanceContractError(f"narration_assets[{index}] 超出所属大分段时间线")
            seen.add(asset_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "plan_version": self.plan_version,
            "tts_fingerprint": self.tts_fingerprint,
            "timeline_unit": TIMELINE_UNIT,
            "total_timeline": self.total_timeline.to_dict(),
            "groups": [item.to_dict() for item in self.groups],
            "shots": [item.to_dict() for item in self.shots],
            "captions": [item.to_dict() for item in self.captions],
            "requirements": [item.to_dict() for item in self.requirements],
            "provenance": [dict(item) for item in self.provenance],
            "status": self.status,
            "cinematic_story": dict(self.cinematic_story),
            "story_review_status": self.story_review_status,
            "story_revision": self.story_revision,
            "narration_assets": [dict(item) for item in self.narration_assets],
            "subtitle_pipeline": dict(self.subtitle_pipeline),
            "digital_human_max_shot_ratio": float(self.digital_human_max_shot_ratio),
            "sound_effect_plan": [dict(item) for item in self.sound_effect_plan],
        }


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    requirement_id: str
    group_id: str | None
    shot_ids: list[str]
    asset_type: str
    role: str
    status: str
    source_node: str
    source_index: int
    asset_version: str
    asset_duration_us: int | None = None
    actual_timeline: TimeWindow | None = None
    remote_url: str = ""
    local_path: str = ""
    evidence_level: str = "synthetic"
    error_type: str = ""
    error_message: str = ""
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in (("asset_id", self.asset_id), ("requirement_id", self.requirement_id), ("asset_type", self.asset_type), ("role", self.role), ("status", self.status), ("source_node", self.source_node), ("asset_version", self.asset_version)):
            _string(value, field_name)
        _nonnegative_int(self.source_index, "source_index")
        if self.status not in ASSET_STATES and self.status != "OBSERVED_REDACTED":
            raise GovernanceContractError(f"{self.asset_id}.status 无效")
        if not all(isinstance(item, str) for item in self.shot_ids):
            raise GovernanceContractError(f"{self.asset_id}.shot_ids 必须是字符串数组")
        if self.asset_duration_us is not None:
            _nonnegative_int(self.asset_duration_us, f"{self.asset_id}.asset_duration_us")
        if self.status == "VALIDATED" and not (self.remote_url or self.local_path):
            raise GovernanceContractError(f"{self.asset_id} 已验证但没有 URL 或本地路径")
        if self.status.startswith("FAILED") and not self.error_type:
            raise GovernanceContractError(f"{self.asset_id} 失败状态缺少 error_type")

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "requirement_id": self.requirement_id,
            "group_id": self.group_id,
            "shot_ids": list(self.shot_ids),
            "asset_type": self.asset_type,
            "role": self.role,
            "status": self.status,
            "source_node": self.source_node,
            "source_index": self.source_index,
            "asset_version": self.asset_version,
            "asset_duration_us": self.asset_duration_us,
            "actual_timeline": self.actual_timeline.to_dict() if self.actual_timeline else None,
            "remote_url": self.remote_url,
            "local_path": self.local_path,
            "evidence_level": self.evidence_level,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AssetRegistry:
    project_id: str
    run_id: str
    source_plan_version: str
    asset_version: str
    records: list[AssetRecord]
    status: str
    mapping_notes: list[str] = field(default_factory=list)
    digital_human_policy: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in (("project_id", self.project_id), ("run_id", self.run_id), ("source_plan_version", self.source_plan_version), ("asset_version", self.asset_version), ("status", self.status)):
            _string(value, field_name)
        if self.status not in {"ASSETS_READY", "ASSETS_READY_WITH_WARNINGS", "ASSETS_PARTIAL", "ASSET_MAPPING_ONLY"}:
            raise GovernanceContractError(f"AssetRegistry.status 无效：{self.status}")
        ids = [item.asset_id for item in self.records]
        if len(ids) != len(set(ids)):
            raise GovernanceContractError("AssetRegistry.asset_id 重复")
        if not all(isinstance(item, str) for item in self.mapping_notes):
            raise GovernanceContractError("mapping_notes 必须是字符串数组")
        if not isinstance(self.digital_human_policy, dict):
            raise GovernanceContractError("digital_human_policy 必须是对象")

    def validate_against(self, director: DirectorLockedManifest) -> None:
        requirements = {item.requirement_id: item for item in director.requirements}
        for record in self.records:
            requirement = requirements.get(record.requirement_id)
            if requirement is None:
                raise GovernanceContractError(f"{record.asset_id} 引用了不存在的 requirement_id")
            if record.group_id != requirement.group_id:
                raise GovernanceContractError(f"{record.asset_id} 的 group_id 与 requirement 不一致")
            if set(record.shot_ids) != set(requirement.shot_ids):
                raise GovernanceContractError(f"{record.asset_id} 的 shot_ids 与 requirement 不一致")

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "source_plan_version": self.source_plan_version,
            "asset_version": self.asset_version,
            "records": [item.to_dict() for item in self.records],
            "status": self.status,
            "mapping_notes": list(self.mapping_notes),
            "digital_human_policy": dict(self.digital_human_policy),
        }


@dataclass(frozen=True)
class EditBinding:
    shot_id: str
    requirement_id: str
    asset_id: str
    track_role: str
    track_id: str
    segment_id: str
    planned_timeline: TimeWindow
    asset_duration_us: int | None
    edit_timeline: TimeWindow | None
    fit_policy: str
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in (("shot_id", self.shot_id), ("requirement_id", self.requirement_id), ("asset_id", self.asset_id), ("track_role", self.track_role), ("fit_policy", self.fit_policy), ("status", self.status)):
            _string(value, field_name)
        if not isinstance(self.track_id, str) or not isinstance(self.segment_id, str):
            raise GovernanceContractError("track_id/segment_id 必须是字符串")
        if self.asset_duration_us is not None:
            _nonnegative_int(self.asset_duration_us, "asset_duration_us")

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "requirement_id": self.requirement_id,
            "asset_id": self.asset_id,
            "track_role": self.track_role,
            "track_id": self.track_id,
            "segment_id": self.segment_id,
            "planned_timeline": self.planned_timeline.to_dict(),
            "asset_duration_us": self.asset_duration_us,
            "edit_timeline": self.edit_timeline.to_dict() if self.edit_timeline else None,
            "fit_policy": self.fit_policy,
            "status": self.status,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class EditManifest:
    project_id: str
    run_id: str
    source_plan_version: str
    source_asset_version: str
    edit_version: str
    bindings: list[EditBinding]
    tracks: list[dict[str, Any]]
    draft_url: str
    local_draft_path: str
    status: str
    validation: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in (("project_id", self.project_id), ("run_id", self.run_id), ("source_plan_version", self.source_plan_version), ("source_asset_version", self.source_asset_version), ("edit_version", self.edit_version), ("status", self.status)):
            _string(value, field_name)
        if self.status not in {"EDIT_PLAN_READY", "COMPILING", "DRAFT_WRITTEN", "DRAFT_STRUCTURALLY_VALID", "DRAFT_VALIDATED", "EDIT_FAILED", "EDIT_MAPPING_ONLY"}:
            raise GovernanceContractError(f"EditManifest.status 无效：{self.status}")
        if not isinstance(self.tracks, list) or not all(isinstance(item, dict) for item in self.tracks):
            raise GovernanceContractError("EditManifest.tracks 必须是对象数组")
        if not isinstance(self.validation, dict):
            raise GovernanceContractError("EditManifest.validation 必须是对象")

    def validate_against(self, director: DirectorLockedManifest, assets: AssetRegistry) -> None:
        """验证剪辑绑定是否只引用同一版本底座中的对象。"""

        shots = {shot.shot_id: shot for shot in director.shots}
        requirements = {item.requirement_id: item for item in director.requirements}
        asset_records = {item.asset_id: item for item in assets.records}
        seen_shots: set[str] = set()
        seen_requirements: set[str] = set()
        for binding in self.bindings:
            if binding.shot_id not in shots:
                raise GovernanceContractError(f"{binding.asset_id} 引用了不存在的 shot_id")
            if binding.requirement_id not in requirements:
                raise GovernanceContractError(f"{binding.asset_id} 引用了不存在的 requirement_id")
            requirement = requirements[binding.requirement_id]
            if binding.shot_id not in requirement.shot_ids:
                raise GovernanceContractError(f"{binding.requirement_id} 与 {binding.shot_id} 不匹配")
            asset = asset_records.get(binding.asset_id)
            if asset is None:
                raise GovernanceContractError(f"剪辑绑定引用了不存在的 asset_id：{binding.asset_id}")
            if asset.requirement_id != binding.requirement_id:
                raise GovernanceContractError(f"{binding.asset_id} 的 requirement_id 与剪辑绑定不一致")
            if binding.shot_id in seen_shots:
                raise GovernanceContractError(f"shot_id 重复绑定：{binding.shot_id}")
            if binding.requirement_id in seen_requirements:
                raise GovernanceContractError(f"requirement_id 重复绑定：{binding.requirement_id}")
            seen_shots.add(binding.shot_id)
            seen_requirements.add(binding.requirement_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "source_plan_version": self.source_plan_version,
            "source_asset_version": self.source_asset_version,
            "edit_version": self.edit_version,
            "bindings": [item.to_dict() for item in self.bindings],
            "tracks": [dict(item) for item in self.tracks],
            "draft_url": self.draft_url,
            "local_draft_path": self.local_draft_path,
            "status": self.status,
            "validation": dict(self.validation),
        }


@dataclass(frozen=True)
class ProjectManifest:
    project_id: str
    run_id: str
    director: DirectorLockedManifest
    assets: AssetRegistry
    edit: EditManifest
    evidence_level: str
    mapping_notes: list[str] = field(default_factory=list)
    field_mappings: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.director.project_id != self.project_id or self.assets.project_id != self.project_id or self.edit.project_id != self.project_id:
            raise GovernanceContractError("ProjectManifest 子模块 project_id 不一致")
        if self.director.run_id != self.run_id or self.assets.run_id != self.run_id or self.edit.run_id != self.run_id:
            raise GovernanceContractError("ProjectManifest 子模块 run_id 不一致")
        self.assets.validate_against(self.director)
        self._validate_digital_human_governance()
        if self.assets.source_plan_version != self.director.plan_version:
            raise GovernanceContractError("AssetRegistry.source_plan_version 与 DirectorLockedManifest 不一致")
        if self.edit.source_plan_version != self.director.plan_version:
            raise GovernanceContractError("EditManifest.source_plan_version 与 DirectorLockedManifest 不一致")
        if self.edit.source_asset_version != self.assets.asset_version:
            raise GovernanceContractError("EditManifest.source_asset_version 与 AssetRegistry 不一致")
        self.edit.validate_against(self.director, self.assets)
        if not isinstance(self.mapping_notes, list) or not all(isinstance(item, str) for item in self.mapping_notes):
            raise GovernanceContractError("mapping_notes 必须是字符串数组")
        required_mapping_keys = {
            "ref_node",
            "source_path",
            "target_path",
            "source_type",
            "target_type",
            "cardinality",
            "order_policy",
            "timeline_unit",
            "evidence_status",
        }
        for mapping in self.field_mappings:
            if not isinstance(mapping, dict) or not required_mapping_keys.issubset(mapping):
                raise GovernanceContractError("field_mappings 缺少统一字段映射键")
            if not all(isinstance(mapping[key], str) and mapping[key] for key in required_mapping_keys):
                raise GovernanceContractError("field_mappings 的字段值必须是非空字符串")

    def _validate_digital_human_governance(self) -> None:
        """正式素材入剪辑前，强制数字人精确替换批准的小镜头坑位。"""

        if self.assets.status == "ASSET_MAPPING_ONLY":
            # 历史脱敏观察样本只能保留为基线，不能冒充新链路的验收结果。
            return
        records = [
            item for item in self.assets.records
            if item.role in {"digital_human", "digital_human_alternative"}
        ]
        if not records:
            return
        assignments: list[dict[str, int | str]] = []
        for record in records:
            target_shot_id = record.metadata.get("target_shot_id")
            if not isinstance(target_shot_id, str) or not target_shot_id:
                raise GovernanceContractError(
                    f"{record.asset_id} 缺少 target_shot_id，禁止按大分段模糊替代"
                )
            if record.actual_timeline is None:
                raise GovernanceContractError(f"{record.asset_id} 缺少数字人实际时间线")
            assignments.append({
                "target_shot_id": target_shot_id,
                "start_us": record.actual_timeline.start_us,
                "end_us": record.actual_timeline.end_us,
            })
        from .digital_human_governance import DigitalHumanGovernanceError, validate_digital_human_slots

        try:
            validate_digital_human_slots(
                self.director,
                assignments,
                policy=(self.assets.digital_human_policy or {
                    "max_shot_ratio": self.director.digital_human_max_shot_ratio,
                }),
            )
        except DigitalHumanGovernanceError as exc:
            raise GovernanceContractError(f"数字人专项治理未通过：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "director": self.director.to_dict(),
            "assets": self.assets.to_dict(),
            "edit": self.edit.to_dict(),
            "evidence_level": self.evidence_level,
            "mapping_notes": list(self.mapping_notes),
            "field_mappings": [dict(item) for item in self.field_mappings],
        }


__all__ = [
    "ALLOWED_TRANSITIONS",
    "AssetRecord",
    "AssetRegistry",
    "CaptionRecord",
    "DirectorGroup",
    "DirectorLockedManifest",
    "DirectorShot",
    "EditBinding",
    "EditManifest",
    "GovernanceContractError",
    "MaterialRequirement",
    "ProjectManifest",
    "TimeWindow",
    "assert_transition",
]
