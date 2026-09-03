"""1256 编导层内部子模块的逐节点闭环治理。

本模块只建立节点级审计和门禁，不改变四类子计划以及
``DirectorLockedManifest`` 的总收口结构。所有未接入真实外部 transport 的节点
都会保留为 ``transport_pending``，不会被伪装成生产完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import DirectorLockedManifest, GovernanceContractError
from .director_convergence import validate_director_convergence
from .node_registry import NODE_CONTRACTS, NodeContract


DIRECTOR_MODULE = "编导层"
MATERIAL_MODULE = "素材层"

DIRECTOR_SUBMODULE_NODE_IDS: dict[str, tuple[str, ...]] = {
    "narrative_intent": ("100001", "129109", "102833"),
    "timing_subtitles": ("178142", "159953", "165901", "105880", "111882"),
    "shot_breakdown": ("103964", "197742", "127095"),
    "visual_route": ("116616", "156199", "139488", "117861"),
}

# 这是节点级证据快照，不是对外部服务的重新调用。
NODE_EVIDENCE: dict[str, dict[str, Any]] = {
    "100001": {
        "status": "contract_verified",
        "evidence": "workflow_mapping_verified",
        "transport_status": "not_required",
        "source_refs": ("workflow-map.md", "node_registry.py"),
        "note": "读取 text、host_image、ref_images 并交给编导主线",
    },
    "129109": {
        "status": "transport_pending",
        "evidence": "real_fixture_verified",
        "transport_status": "transport_pending",
        "source_refs": ("run-7668196429877870619-129109-directors-v2-output.json", "tests/test_directors_v2.py"),
        "note": "真实输出已保存；当前模型 transport 尚未做 live 验收",
    },
    "102833": {
        "status": "transport_pending",
        "evidence": "contract_verified",
        "transport_status": "transport_pending",
        "source_refs": ("src/workflow_1256/visual_intent.py", "tests/test_visual_intent.py"),
        "note": "输入输出适配器已验证；缺少真实 LLM transport",
    },
    "178142": {
        "status": "transport_pending",
        "evidence": "contract_verified",
        "transport_status": "transport_pending",
        "source_refs": ("workflow-map.md", "src/workflow_1256/orchestrator.py"),
        "note": "循环顺序已接入；内部 TTS 结果仍依赖 159953",
    },
    "159953": {
        "status": "transport_pending",
        "evidence": "contract_verified",
        "transport_status": "transport_pending",
        "source_refs": ("src/workflow_1256/speech_synthesis.py", "schemas/speech_synthesis.schema.json"),
        "note": "TTS 字段契约已验证；真实 TTS transport 未验收",
    },
    "165901": {
        "status": "real_fixture_verified",
        "evidence": "real_fixture_verified",
        "transport_status": "verified_live_route",
        "source_refs": ("workflow-map.md", "tests/test_capcut_node_adapters.py"),
        "note": "audio_timelines 路由和时间线字段已对照",
    },
    "105880": {
        "status": "transport_pending",
        "evidence": "offline_contract_verified",
        "transport_status": "transport_pending",
        "source_refs": ("src/workflow_1256/capcut_stt_subtitle_pipeline.py", "tests/test_subtitle_pipeline.py", "tests/test_shot_slot_planning.py"),
        "note": "唯一生产路径为 TTS -> CapCut STT -> Mini -> 2.5–5 秒坑位；离线契约已验证，真实 STT/Mini 尚待验收",
    },
    "111882": {
        "status": "real_fixture_verified",
        "evidence": "real_fixture_verified",
        "transport_status": "verified_live_route",
        "source_refs": ("workflow-map.md", "src/workflow_1256/capcut_node_adapters.py"),
        "note": "字幕输入字段和 caption_infos 路由已验证",
    },
    "103964": {
        "status": "transport_pending",
        "evidence": "real_fixture_verified",
        "transport_status": "transport_pending",
        "source_refs": ("samples/real/run-7668196429877870619-103964-shot-refinement-output.json", "tests/test_shot_refinement.py"),
        "note": "批处理和时间线投影已对照；内部 197742 LLM 未 live 验收",
    },
    "197742": {
        "status": "transport_pending",
        "evidence": "contract_verified",
        "transport_status": "transport_pending",
        "source_refs": ("src/workflow_1256/shot_refinement.py", "src/workflow_1256/shot_refinement_transport.py"),
        "note": "每组输入输出契约已建立；镜头精细化模型 transport 未验收",
    },
    "127095": {
        "status": "real_fixture_verified",
        "evidence": "real_fixture_verified",
        "transport_status": "not_required",
        "source_refs": ("src/workflow_1256/timeline_planning.py", "tests/test_timeline_planning.py"),
        "note": "9 组真实 fixture 对照通过，时间单位为微秒",
    },
    "116616": {
        "status": "transport_pending",
        "evidence": "contract_verified_shape_observed",
        "transport_status": "transport_pending",
        "source_refs": ("samples/real/run-7668196429877870619-116616-coze-output-observation.json", "tests/test_shot_visual_arrangement.py"),
        "note": "视觉编排结构和字段形状已对照；模型/素材 transport 未验收",
    },
    "156199": {
        "status": "contract_verified",
        "evidence": "contract_verified",
        "transport_status": "not_required",
        "source_refs": ("src/workflow_1256/scene_type_recognition.py", "tests/test_scene_type_recognition.py"),
        "note": "Host/普通画面路线输入输出和异常字段已验证",
    },
    "139488": {
        "status": "transport_pending",
        "evidence": "contract_verified",
        "transport_status": "transport_pending",
        "source_refs": ("src/workflow_1256/host_shot_recognition.py", "tests/test_host_shot_recognition.py"),
        "note": "候选 idx 约束已验证；Host LLM transport 未验收",
    },
    "117861": {
        "status": "contract_verified",
        "evidence": "contract_verified",
        "transport_status": "not_required",
        "source_refs": ("src/workflow_1256/host_task_assembly.py", "tests/test_host_task_assembly.py"),
        "note": "Host 音频任务组装、合并顺序和失败字段已验证",
    },
}

NODE_STATUSES = {"contract_verified", "real_fixture_verified", "transport_pending", "blocked"}
TRANSPORT_STATUSES = {"not_required", "verified_live_route", "transport_pending"}


class DirectorSubmoduleClosureError(GovernanceContractError):
    """编导子模块节点注册或证据闭环不一致。"""


def _contract_map() -> dict[str, NodeContract]:
    return {item.node_id: item for item in NODE_CONTRACTS}


@dataclass(frozen=True)
class DirectorNodeClosure:
    node_id: str
    name: str
    submodule: str
    node_kind: str
    parent_node_id: str | None
    input_fields: tuple[str, ...]
    output_fields: tuple[str, ...]
    data_role: str
    external_dependency: str
    status: str
    evidence: str
    transport_status: str
    source_refs: tuple[str, ...]
    note: str
    failure_semantics: tuple[str, ...]

    @property
    def production_closed(self) -> bool:
        return self.status in {"contract_verified", "real_fixture_verified"} and self.transport_status != "transport_pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "submodule": self.submodule,
            "node_kind": self.node_kind,
            "parent_node_id": self.parent_node_id,
            "input_fields": list(self.input_fields),
            "output_fields": list(self.output_fields),
            "data_role": self.data_role,
            "external_dependency": self.external_dependency,
            "status": self.status,
            "evidence": self.evidence,
            "transport_status": self.transport_status,
            "production_closed": self.production_closed,
            "source_refs": list(self.source_refs),
            "note": self.note,
            "failure_semantics": list(self.failure_semantics),
        }


@dataclass(frozen=True)
class DirectorSubmoduleClosure:
    name: str
    node_ids: tuple[str, ...]
    status: str
    mapping_gate: bool
    production_gate: bool
    blocking_nodes: tuple[str, ...]
    nodes: tuple[DirectorNodeClosure, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "node_ids": list(self.node_ids),
            "status": self.status,
            "mapping_gate": self.mapping_gate,
            "production_gate": self.production_gate,
            "blocking_nodes": list(self.blocking_nodes),
            "nodes": [item.to_dict() for item in self.nodes],
        }


@dataclass(frozen=True)
class DirectorSubmoduleClosureReport:
    overall_status: str
    mapping_gate: bool
    production_gate: bool
    manifest_status: str
    manifest_unchanged: bool
    node_count: int
    blocking_nodes: tuple[str, ...]
    submodules: tuple[DirectorSubmoduleClosure, ...]
    nodes: tuple[DirectorNodeClosure, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "mapping_gate": self.mapping_gate,
            "production_gate": self.production_gate,
            "manifest_status": self.manifest_status,
            "manifest_unchanged": self.manifest_unchanged,
            "node_count": self.node_count,
            "blocking_nodes": list(self.blocking_nodes),
            "submodules": [item.to_dict() for item in self.submodules],
            "nodes": [item.to_dict() for item in self.nodes],
        }


def _failure_semantics(contract: NodeContract) -> tuple[str, ...]:
    semantics = [
        "输入字段缺失、类型错误或数组顺序不一致时拒绝收口",
        "保留原节点索引、错误字段和失败分支，不压缩失败项",
    ]
    if contract.external_dependency != "none":
        semantics.append("缺少外部 transport 时返回 transport_pending/blocked，不伪造成功结果")
    return tuple(semantics)


def _build_node_closure(node_id: str, submodule: str, contracts: Mapping[str, NodeContract]) -> DirectorNodeClosure:
    contract = contracts.get(node_id)
    if contract is None:
        raise DirectorSubmoduleClosureError(f"编导子模块引用了未注册节点：{node_id}")
    if contract.module != DIRECTOR_MODULE:
        raise DirectorSubmoduleClosureError(
            f"节点 {node_id} 当前属于 {contract.module}，不能登记为编导内部节点"
        )
    evidence = NODE_EVIDENCE.get(node_id)
    if evidence is None:
        raise DirectorSubmoduleClosureError(f"节点 {node_id} 缺少节点级证据快照")
    status = evidence["status"]
    transport_status = evidence["transport_status"]
    if status not in NODE_STATUSES or transport_status not in TRANSPORT_STATUSES:
        raise DirectorSubmoduleClosureError(f"节点 {node_id} 的证据状态无效")
    return DirectorNodeClosure(
        node_id=node_id,
        name=contract.name,
        submodule=submodule,
        node_kind=contract.node_kind,
        parent_node_id=contract.parent_node_id,
        input_fields=contract.input_fields,
        output_fields=contract.output_fields,
        data_role=contract.data_role,
        external_dependency=contract.external_dependency,
        status=status,
        evidence=evidence["evidence"],
        transport_status=transport_status,
        source_refs=tuple(evidence["source_refs"]),
        note=evidence["note"],
        failure_semantics=_failure_semantics(contract),
    )


def build_director_submodule_closure(
    manifest: DirectorLockedManifest,
) -> DirectorSubmoduleClosureReport:
    """校验并生成编导层逐节点闭环报告，不修改 Manifest。"""

    convergence = validate_director_convergence(manifest)
    if convergence.status != "DIRECTOR_LOCKED":
        raise DirectorSubmoduleClosureError("编导总收口尚未达到 DIRECTOR_LOCKED")
    contracts = _contract_map()
    all_nodes: list[DirectorNodeClosure] = []
    submodules: list[DirectorSubmoduleClosure] = []
    for submodule, node_ids in DIRECTOR_SUBMODULE_NODE_IDS.items():
        nodes = tuple(_build_node_closure(node_id, submodule, contracts) for node_id in node_ids)
        blocking = tuple(
            node.node_id
            for node in nodes
            if node.status in {"transport_pending", "blocked"}
        )
        status = "BLOCKED" if any(node.status == "blocked" for node in nodes) else (
            "PARTIAL" if blocking else "CLOSED"
        )
        submodules.append(
            DirectorSubmoduleClosure(
                name=submodule,
                node_ids=node_ids,
                status=status,
                mapping_gate=True,
                production_gate=not blocking,
                blocking_nodes=blocking,
                nodes=nodes,
            )
        )
        all_nodes.extend(nodes)

    blocking_nodes = tuple(node.node_id for node in all_nodes if node.status in {"transport_pending", "blocked"})
    overall_status = "BLOCKED" if any(node.status == "blocked" for node in all_nodes) else (
        "PARTIAL" if blocking_nodes else "CLOSED"
    )
    return DirectorSubmoduleClosureReport(
        overall_status=overall_status,
        mapping_gate=True,
        production_gate=not blocking_nodes,
        manifest_status=manifest.status,
        manifest_unchanged=True,
        node_count=len(all_nodes),
        blocking_nodes=blocking_nodes,
        submodules=tuple(submodules),
        nodes=tuple(all_nodes),
    )


__all__ = [
    "DIRECTOR_SUBMODULE_NODE_IDS",
    "DirectorNodeClosure",
    "DirectorSubmoduleClosure",
    "DirectorSubmoduleClosureError",
    "DirectorSubmoduleClosureReport",
    "build_director_submodule_closure",
]
