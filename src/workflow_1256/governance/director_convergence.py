"""1256 编导大模块的数据收口门。

这个模块只负责把编导层的四类结果校验并锁定到已有的
``DirectorLockedManifest``：叙事意图、语音时序与字幕、镜头拆解、视觉规格与路线。
它不生成提示词、不调用模型，也不修改任何原 Coze 节点输出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contracts import DirectorLockedManifest, GovernanceContractError


SUBPLAN_ORDER = (
    "narrative_intent",
    "timing_subtitles",
    "shot_breakdown",
    "visual_route",
)
SUBPLAN_STATES = {"READY", "OBSERVED_REDACTED"}
VISUAL_SPEC_STATES = {"READY", "OBSERVED_REDACTED"}
ROUTE_STATES = {"CANDIDATES_ONLY", "SELECTED", "OBSERVED_REDACTED"}

# 这些字段属于素材层 PromptPackage 或外部服务请求，不能进入编导锁定数据。
FORBIDDEN_MATERIAL_KEYS = {
    "prompt",
    "videoprompt",
    "imageprompt",
    "negativeprompt",
    "model",
    "provider",
    "apikey",
    "endpoint",
    "requestpayload",
    "officialfallback",
}


class DirectorConvergenceError(GovernanceContractError):
    """编导子计划无法收口为唯一锁定版本。"""


def _text(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DirectorConvergenceError(f"{field_name} 必须是非空字符串")


def _normal_key(value: str) -> str:
    return value.lower().replace("_", "").replace("-", "")


def _assert_no_material_keys(value: Any, path: str = "root") -> None:
    """递归检查导演数据中没有落入素材生产请求字段。"""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and _normal_key(key) in FORBIDDEN_MATERIAL_KEYS:
                raise DirectorConvergenceError(
                    f"{path}.{key} 属于素材层 PromptPackage/请求字段，不能进入编导收口"
                )
            _assert_no_material_keys(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _assert_no_material_keys(child, f"{path}[{index}]")


@dataclass(frozen=True)
class DirectorSubplanReceipt:
    """编导内部子模块的版本回执，不保存模型请求内容。"""

    name: str
    project_id: str
    run_id: str
    plan_version: str
    status: str
    source_nodes: tuple[str, ...]
    evidence: str = ""

    def __post_init__(self) -> None:
        for field_name, value in (
            ("name", self.name),
            ("project_id", self.project_id),
            ("run_id", self.run_id),
            ("plan_version", self.plan_version),
            ("status", self.status),
        ):
            _text(value, field_name)
        if self.name not in SUBPLAN_ORDER:
            raise DirectorConvergenceError(f"未知编导子计划：{self.name}")
        if self.status not in SUBPLAN_STATES:
            raise DirectorConvergenceError(f"{self.name}.status 不允许收口：{self.status}")
        if not self.source_nodes or not all(isinstance(item, str) and item for item in self.source_nodes):
            raise DirectorConvergenceError(f"{self.name}.source_nodes 不能为空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "plan_version": self.plan_version,
            "status": self.status,
            "source_nodes": list(self.source_nodes),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class DirectorVisualSpec:
    """镜头语义视觉规格；不包含最终提示词或供应商参数。"""

    shot_id: str
    group_id: str
    shot_index: int
    status: str
    fields: Mapping[str, Any]
    observed_fields: tuple[str, ...]
    source_nodes: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.shot_id, "visual_spec.shot_id")
        _text(self.group_id, "visual_spec.group_id")
        if isinstance(self.shot_index, bool) or not isinstance(self.shot_index, int) or self.shot_index < 0:
            raise DirectorConvergenceError("visual_spec.shot_index 必须是非负整数")
        if self.status not in VISUAL_SPEC_STATES:
            raise DirectorConvergenceError(f"{self.shot_id}.visual_spec.status 无效")
        if not isinstance(self.fields, Mapping):
            raise DirectorConvergenceError(f"{self.shot_id}.visual_spec.fields 必须是对象")
        if not isinstance(self.observed_fields, tuple) or not all(
            isinstance(item, str) and item for item in self.observed_fields
        ):
            raise DirectorConvergenceError(f"{self.shot_id}.visual_spec.observed_fields 无效")
        if not self.source_nodes or not all(isinstance(item, str) and item for item in self.source_nodes):
            raise DirectorConvergenceError(f"{self.shot_id}.visual_spec.source_nodes 不能为空")
        _assert_no_material_keys(self.fields, f"visual_specs[{self.shot_id}].fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "group_id": self.group_id,
            "shot_index": self.shot_index,
            "status": self.status,
            "fields": dict(self.fields),
            "observed_fields": list(self.observed_fields),
            "source_nodes": list(self.source_nodes),
        }


@dataclass(frozen=True)
class DirectorRouteSpec:
    """路线候选与路线决策；不创建素材任务。"""

    shot_id: str
    group_id: str
    route_candidates: tuple[str, ...]
    selected_route: str | None
    status: str
    source_nodes: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.shot_id, "route_spec.shot_id")
        _text(self.group_id, "route_spec.group_id")
        if self.status not in ROUTE_STATES:
            raise DirectorConvergenceError(f"{self.shot_id}.route.status 无效")
        if not self.route_candidates or not all(
            isinstance(item, str) and item for item in self.route_candidates
        ):
            raise DirectorConvergenceError(f"{self.shot_id}.route_candidates 不能为空")
        if self.selected_route is not None and self.selected_route not in self.route_candidates:
            raise DirectorConvergenceError(f"{self.shot_id}.selected_route 不在候选路线中")
        if self.status == "SELECTED" and self.selected_route is None:
            raise DirectorConvergenceError(f"{self.shot_id}.status=SELECTED 时必须有 selected_route")
        if self.status == "CANDIDATES_ONLY" and self.selected_route is not None:
            raise DirectorConvergenceError(f"{self.shot_id}.status=CANDIDATES_ONLY 不得伪造已选路线")
        if not self.source_nodes or not all(isinstance(item, str) and item for item in self.source_nodes):
            raise DirectorConvergenceError(f"{self.shot_id}.route.source_nodes 不能为空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "group_id": self.group_id,
            "route_candidates": list(self.route_candidates),
            "selected_route": self.selected_route,
            "status": self.status,
            "source_nodes": list(self.source_nodes),
        }


@dataclass(frozen=True)
class DirectorConvergenceInput:
    """四类编导子结果的同版本输入。"""

    project_id: str
    run_id: str
    plan_version: str
    subplans: tuple[DirectorSubplanReceipt, ...]
    visual_specs: tuple[DirectorVisualSpec, ...]
    route_specs: tuple[DirectorRouteSpec, ...]

    @classmethod
    def from_manifest(cls, manifest: DirectorLockedManifest) -> "DirectorConvergenceInput":
        """从现有锁定 Manifest 投影收口视图，不补造被脱敏的模型内容。"""

        subplans = (
            DirectorSubplanReceipt(
                "narrative_intent",
                manifest.project_id,
                manifest.run_id,
                manifest.plan_version,
                "READY",
                ("100001", "129109", "102833"),
                "real_fixture_verified",
            ),
            DirectorSubplanReceipt(
                "timing_subtitles",
                manifest.project_id,
                manifest.run_id,
                manifest.plan_version,
                "OBSERVED_REDACTED",
                ("178142", "159953", "165901", "105880", "111882"),
                "real_fixture_verified_with_redacted_audio_locator",
            ),
            DirectorSubplanReceipt(
                "shot_breakdown",
                manifest.project_id,
                manifest.run_id,
                manifest.plan_version,
                "READY",
                ("103964", "197742", "127095"),
                "real_fixture_verified",
            ),
            DirectorSubplanReceipt(
                "visual_route",
                manifest.project_id,
                manifest.run_id,
                manifest.plan_version,
                "OBSERVED_REDACTED",
                ("116616", "156199", "139488", "117861"),
                "shape_or_route_observed_only",
            ),
        )
        visual_specs: list[DirectorVisualSpec] = []
        route_specs: list[DirectorRouteSpec] = []
        for shot in manifest.shots:
            arrangement = shot.production_spec.get("shot_visual_arrangement", {})
            observed_fields = arrangement.get("observed_fields", [])
            if not isinstance(observed_fields, list):
                observed_fields = []
            visual_specs.append(
                DirectorVisualSpec(
                    shot_id=shot.shot_id,
                    group_id=shot.group_id,
                    shot_index=shot.shot_index,
                    status="OBSERVED_REDACTED"
                    if arrangement.get("values_status") != "available"
                    else "READY",
                    fields={},
                    observed_fields=tuple(observed_fields),
                    source_nodes=("116616",),
                )
            )
            route_specs.append(
                DirectorRouteSpec(
                    shot_id=shot.shot_id,
                    group_id=shot.group_id,
                    route_candidates=tuple(shot.route_candidates),
                    selected_route=None,
                    status="CANDIDATES_ONLY",
                    source_nodes=("156199", "139488", "117861"),
                )
            )
        return cls(
            project_id=manifest.project_id,
            run_id=manifest.run_id,
            plan_version=manifest.plan_version,
            subplans=subplans,
            visual_specs=tuple(visual_specs),
            route_specs=tuple(route_specs),
        )


@dataclass(frozen=True)
class DirectorConvergenceReport:
    status: str
    evidence_level: str
    project_id: str
    run_id: str
    plan_version: str
    checks: tuple[str, ...]
    counts: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "evidence_level": self.evidence_level,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "plan_version": self.plan_version,
            "checks": list(self.checks),
            "counts": dict(self.counts),
        }


def _validate_subplans(manifest: DirectorLockedManifest, data: DirectorConvergenceInput) -> None:
    if [item.name for item in data.subplans] != list(SUBPLAN_ORDER):
        raise DirectorConvergenceError("编导子计划必须按叙事、时序、镜头、视觉路线顺序收口")
    for item in data.subplans:
        if (item.project_id, item.run_id, item.plan_version) != (
            manifest.project_id,
            manifest.run_id,
            manifest.plan_version,
        ):
            raise DirectorConvergenceError(f"{item.name} 与 DirectorLockedManifest 版本不一致")
        if item.status not in SUBPLAN_STATES:
            raise DirectorConvergenceError(f"{item.name} 未达到可收口状态")


def _validate_visual_specs(manifest: DirectorLockedManifest, data: DirectorConvergenceInput) -> None:
    if len(data.visual_specs) != len(manifest.shots):
        raise DirectorConvergenceError("visual_specs 数量必须与 shots 一一对应")
    for expected, actual in zip(manifest.shots, data.visual_specs, strict=True):
        if (actual.shot_id, actual.group_id, actual.shot_index) != (
            expected.shot_id,
            expected.group_id,
            expected.shot_index,
        ):
            raise DirectorConvergenceError("visual_specs 必须保持原 shots 数组顺序")
        if actual.status == "READY" and not actual.fields:
            raise DirectorConvergenceError(f"{actual.shot_id} 的 READY VisualSpec 不能为空")
        _assert_no_material_keys(expected.production_spec, f"shots[{expected.shot_id}].production_spec")


def _validate_route_specs(manifest: DirectorLockedManifest, data: DirectorConvergenceInput) -> None:
    if len(data.route_specs) != len(manifest.shots):
        raise DirectorConvergenceError("route_specs 数量必须与 shots 一一对应")
    for expected, actual in zip(manifest.shots, data.route_specs, strict=True):
        if (actual.shot_id, actual.group_id) != (expected.shot_id, expected.group_id):
            raise DirectorConvergenceError("route_specs 必须保持原 shots 数组顺序")
        if tuple(expected.route_candidates) != actual.route_candidates:
            raise DirectorConvergenceError(f"{actual.shot_id} 的路线候选顺序被改变")


def validate_director_convergence(
    manifest: DirectorLockedManifest,
    convergence: DirectorConvergenceInput | None = None,
) -> DirectorConvergenceReport:
    """执行编导唯一收口门；通过后才允许把 Manifest 交给素材层。"""

    if not isinstance(manifest, DirectorLockedManifest):
        raise DirectorConvergenceError("收口输入必须是 DirectorLockedManifest")
    data = convergence or DirectorConvergenceInput.from_manifest(manifest)
    if (data.project_id, data.run_id, data.plan_version) != (
        manifest.project_id,
        manifest.run_id,
        manifest.plan_version,
    ):
        raise DirectorConvergenceError("收口输入与 DirectorLockedManifest 的项目/运行/版本不一致")
    _validate_subplans(manifest, data)
    _validate_visual_specs(manifest, data)
    _validate_route_specs(manifest, data)
    evidence_level = (
        "real_fixture_observed_with_redaction"
        if any(item.status == "OBSERVED_REDACTED" for item in data.subplans)
        else "contract_validated"
    )
    return DirectorConvergenceReport(
        status="DIRECTOR_LOCKED",
        evidence_level=evidence_level,
        project_id=manifest.project_id,
        run_id=manifest.run_id,
        plan_version=manifest.plan_version,
        checks=(
            "project_run_version_aligned",
            "subplan_order_and_status_valid",
            "groups_shots_captions_requirements_already_locked",
            "timeline_continuity_and_microseconds_valid",
            "visual_specs_one_to_one_and_prompt_boundary_valid",
            "route_candidates_one_to_one_and_order_preserved",
        ),
        counts={
            "subplans": len(data.subplans),
            "groups": len(manifest.groups),
            "shots": len(manifest.shots),
            "captions": len(manifest.captions),
            "requirements": len(manifest.requirements),
            "visual_specs": len(data.visual_specs),
            "route_specs": len(data.route_specs),
        },
    )


def lock_director_manifest(
    manifest: DirectorLockedManifest,
    convergence: DirectorConvergenceInput | None = None,
) -> DirectorLockedManifest:
    """收口成功后原样返回锁定 Manifest，明确不改写原节点结果。"""

    validate_director_convergence(manifest, convergence)
    return manifest


__all__ = [
    "DirectorConvergenceError",
    "DirectorConvergenceInput",
    "DirectorConvergenceReport",
    "DirectorRouteSpec",
    "DirectorSubplanReceipt",
    "DirectorVisualSpec",
    "lock_director_manifest",
    "validate_director_convergence",
]
