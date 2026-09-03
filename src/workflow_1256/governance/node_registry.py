"""1256 三大模块的节点注册表与交接门禁。

注册表只描述现有工作流的节点边界，不改变任何原节点的字段名或执行顺序。
它的作用是让每个内部节点有明确的数据责任、父批处理关系和下一层交接条件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


MODULES = ("编导层", "素材层", "剪辑层")


@dataclass(frozen=True)
class NodeContract:
    node_id: str
    name: str
    module: str
    node_kind: str
    input_fields: tuple[str, ...] = ()
    output_fields: tuple[str, ...] = ()
    parent_node_id: str | None = None
    data_role: str = ""
    external_dependency: str = "none"
    implementation_status: str = "contract_registered"

    def __post_init__(self) -> None:
        if not self.node_id or not self.name:
            raise ValueError("节点 ID 和中文名称不能为空")
        if self.module not in MODULES:
            raise ValueError(f"未知业务模块：{self.module}")
        if not isinstance(self.input_fields, tuple) or not isinstance(self.output_fields, tuple):
            raise TypeError("输入输出字段必须是 tuple")
        if len(set(self.input_fields)) != len(self.input_fields):
            raise ValueError(f"{self.node_id} 输入字段重复")
        if len(set(self.output_fields)) != len(self.output_fields):
            raise ValueError(f"{self.node_id} 输出字段重复")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "module": self.module,
            "node_kind": self.node_kind,
            "input_fields": list(self.input_fields),
            "output_fields": list(self.output_fields),
            "parent_node_id": self.parent_node_id,
            "data_role": self.data_role,
            "external_dependency": self.external_dependency,
            "implementation_status": self.implementation_status,
        }


def _fields(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _node(
    node_id: int | str,
    name: str,
    module: str,
    node_kind: str,
    inputs: str = "",
    outputs: str = "",
    *,
    parent: int | str | None = None,
    role: str = "",
    dependency: str = "none",
    status: str = "contract_registered",
) -> NodeContract:
    return NodeContract(
        node_id=str(node_id),
        name=name,
        module=module,
        node_kind=node_kind,
        input_fields=_fields(inputs),
        output_fields=_fields(outputs),
        parent_node_id=str(parent) if parent is not None else None,
        data_role=role,
        external_dependency=dependency,
        implementation_status=status,
    )


# 52 个顶层节点 + 9 个批处理/循环内部节点 = 61 个可执行节点。
NODE_CONTRACTS: tuple[NodeContract, ...] = (
    # 编导层：把文本、TTS 时间线和镜头规划收敛成 DirectorLockedManifest。
    _node(100001, "开始", "编导层", "start", outputs="host_image,ref_images,text", role="workflow_entry"),
    _node(129109, "具体画面导演", "编导层", "plugin", "text", "director_plan,ok,segment_beats,segments", role="director_plan", dependency="LLM", status="fixture_observed"),
    _node(103964, "镜头精细化", "编导层", "batch", "duration,items,segments,timelines", "Code_list,LLM_list", role="shot_grouping", dependency="LLM+code", status="fixture_observed"),
    _node(197742, "镜头精细化导演", "编导层", "llm", "duration,segment,segment_index", "shots", parent=103964, role="shot_grouping", dependency="LLM", status="transport_required"),
    _node(127095, "时长计算时间线规划", "编导层", "code", "segments,shots,duration,timelines", "shots,clip_duration,int_duration,timelines", parent=103964, role="shot_timeline", status="fixture_verified"),
    _node(102833, "全文视觉意图导演", "编导层", "llm", "segments,director_plan", "items,music_cues,reasoning_content", role="visual_intent", dependency="LLM", status="transport_required"),
    _node(178142, "循环", "编导层", "loop", "segments", "link_list", role="order_control"),
    _node(165901, "时间线", "编导层", "plugin", "links", "all_timelines,timelines", role="tts_timeline", dependency="TTS", status="fixture_observed"),
    _node(105880, "TTS-STT 字幕与坑位规划", "编导层", "pipeline", "segment_text,group_timelines,all_timelines,audio_sources", "captions,BGM_duration,duration,new_segments,new_timelines,shot_slot_plan", role="caption_timeline", dependency="CapCut STT+Mini", status="transport_implemented_live_pending"),
    _node(111882, "字幕", "编导层", "plugin", "texts,timelines", "infos", role="caption_material", dependency="TTS/字幕服务", status="fixture_observed"),
    _node(116616, "Shot_Visual_Arrangement", "编导层", "plugin", "Code_list,ref_image,segments,debug", "debug,error,int_duration,motion_seed,prompt,ref_image,timelines", role="visual_asset_plan", dependency="LLM+素材服务", status="contract_only"),
    # 最终提示词和首尾帧参数属于素材层 PromptPackage 编译，不进入编导锁定对象。
    _node(195692, "生成视频提示词撰写", "素材层", "batch", "items,motion_seed,ref_image,clip_duration", "plan_out_list", role="prompt_package_compile", dependency="LLM", status="contract_only"),
    _node(181639, "图生视频提示词导演", "素材层", "llm", "items,motion_seed,ref_image,clip_duration", "plans", parent=195692, role="prompt_package_compile", dependency="LLM", status="transport_required"),
    _node(170263, "首尾帧顺延", "素材层", "code", "LLM_list,motion_seed,items,image_url_list,Code_list,story_context,shot_metadata,digital_human_shot_indices,use_frame_continuity", "clip_duration,error,items,motion_seed,ref_image,ref_image_e,ref_image_f,story_context,skipped_digital_human_shot_indices", role="frame_continuity_prepare", status="fixture_verified"),
    _node(113743, "提示词生成", "素材层", "code", "plan_out_list,ref_image,clip_duration,story_context,shot_metadata,digital_human_shot_indices,use_frame_continuity", "camera_fixed,error,int_duration,ref_image_e,ref_image_f,video_prompt,skipped_digital_human_shot_indices", role="prompt_package_compile", status="contract_only"),
    _node(156199, "画面类型识别", "编导层", "code", "director_plan,Code_list,new_segments,new_timelines,link_list,timelines", "error,host_llm_input,host_map", role="route_selection", status="contract_only"),
    _node(139488, "host 镜头识别", "编导层", "llm", "host_llm_input", "host_idxs,reasoning_content", role="route_selection", dependency="LLM", status="transport_required"),
    _node(117861, "Host 任务组装", "编导层", "code", "host_map,host_idxs", "audio_url,audio_time,new_timelines,error", role="host_timeline", status="contract_only"),
    # 素材层：把编导需求变成可追踪的图片、视频、音频和替代路线资产。
    _node(159953, "speech_synthesis", "编导层", "plugin", "text,speed_ratio,voice_id", "data,log_id,msg", parent=178142, role="narration_timing_lock", dependency="TTS", status="transport_required"),
    _node(184922, "首帧生成", "素材层", "batch", "prompt,ref_image,image_model,shot_metadata,digital_human_shot_indices,grid_config,grid_layout", "image_url_list,grid_batches,skipped_digital_human_shot_indices", role="image_asset", dependency="aishuch", status="transport_required"),
    _node(142186, "create_image_task", "素材层", "plugin", "prompt,ref_image,image_model", "task_id,status", parent=184922, role="image_task_create", dependency="aishuch", status="transport_required"),
    _node(106538, "get_task_result", "素材层", "plugin", "task_id", "image_url,status,error", parent=184922, role="image_task_query", dependency="aishuch", status="transport_required"),
    _node(115723, "AIGC动画", "素材层", "plugin", "timelines,video_urls,transition,transition_duration,volume", "infos", role="aigc_material", dependency="视频服务", status="fixture_observed"),
    _node(175652, "host_audio_split", "素材层", "plugin", "host_audio_timeline,links", "failed_segments,msg,segments,status,url_list", role="host_audio_asset", dependency="素材服务", status="transport_required"),
    _node(143380, "merge_audio_urls", "素材层", "plugin", "audio_urls", "duration,failed_count,format,msg,status,success,timeline,total,url", role="host_audio_asset", dependency="素材服务", status="transport_required"),
    _node(173596, "Create_infinitetalk_Task", "素材层", "plugin", "api_key,audio,image,prompt,Max_side_length,Scale,intensity", "msg,status,task_ID", role="digital_human_task_create", dependency="InfiniteTalk", status="transport_required"),
    _node(118959, "time_sleep", "素材层", "plugin", "seconds", "seconds", role="poll_control", status="contract_only"),
    _node(137386, "query_infinitetalk_task", "素材层", "plugin", "api_key,taskId", "cost_coins,cost_time,errorBody,isSuccess,msg,output_url,status,task_ID", role="digital_human_task_query", dependency="InfiniteTalk", status="transport_required"),
    _node(1178381, "query_infinitetalk_task_1", "素材层", "plugin", "api_key,taskId", "cost_coins,cost_time,errorBody,isSuccess,msg,output_url,status,task_ID", role="digital_human_task_query", dependency="InfiniteTalk", status="transport_required"),
    _node(160040, "video_split_by_timeline_2", "素材层", "plugin", "timeline,video_url", "failed_count,failed_segments,ffmpeg_path,msg,segments,status,success_count,total,url_list", role="video_asset_split", dependency="FFmpeg/素材服务", status="transport_required"),
    _node(152553, "HOST数字人", "素材层", "plugin", "timelines,video_urls,transition,transition_duration,volume", "infos", role="digital_human_asset", dependency="视频服务", status="fixture_observed"),
    _node(192311, "BGM 任务组装", "素材层", "code", "timelines,music_cues", "bgm_tasks,bgm_timelines,transition_schemes", role="bgm_asset_plan", status="contract_only"),
    _node(141288, "批处理_2", "素材层", "batch", "bgm_tasks", "AudioUrl_list", role="bgm_asset_generation", dependency="BGM服务", status="transport_required"),
    _node(186546, "gen_bgm", "素材层", "plugin", "task", "audio_url,status,error", parent=141288, role="bgm_asset_generation", dependency="BGM服务", status="transport_required"),
    _node(165818, "merge_bgm_timeline", "素材层", "plugin", "audio_urls,timelines,transition_schemes", "audio_url,audio_url_list,duration", role="bgm_asset_timeline", dependency="BGM服务", status="transport_required"),
    _node(116592, "背景音乐", "素材层", "plugin", "mp3_urls,timelines,volume", "infos", role="bgm_material", dependency="音频服务", status="fixture_observed"),
    _node(191683, "解说", "素材层", "plugin", "mp3_urls,timelines,audio_effect,volume", "infos", role="narration_material", dependency="TTS/音频服务", status="fixture_observed"),
    _node(110697, "批处理", "素材层", "batch", "video_prompt,int_duration,ref_image_f,ref_image_e,video_model,use_frame_continuity", "public_video_url_list", role="aigc_video_generation", dependency="Seedance", status="transport_required"),
    _node(139653, "video_generate", "素材层", "plugin", "prompt,first_frame,last_frame,video_model", "task_id,status", parent=110697, role="aigc_video_task_create", dependency="Seedance", status="transport_required"),
    _node(106157, "video_query", "素材层", "plugin", "task_id", "video_url,status,error", parent=110697, role="aigc_video_task_query", dependency="Seedance", status="transport_required"),
    _node(1693143, "query_infinitetalk_task_2", "素材层", "plugin", "api_key,taskId", "cost_coins,cost_time,errorBody,isSuccess,msg,output_url,status,task_ID", role="digital_human_task_query", dependency="InfiniteTalk", status="transport_required"),
    _node(121815, "变量聚合", "素材层", "variable_merge", "branch_outputs", "output_url", role="branch_selection", status="contract_only"),
    # 剪辑层：把最新草稿地址、已追踪素材和轨道计划写入剪映。
    _node(182422, "创建草稿", "剪辑层", "plugin", "height,width", "draft_url,tip_url", role="draft_create", dependency="CapCut Mate", status="transport_required"),
    _node(135313, "解说*", "剪辑层", "plugin", "audio_infos,draft_url", "audio_ids,draft_url,track_id", role="narration_track_write", dependency="CapCut Mate", status="fixture_observed"),
    _node(110576, "背景音乐*", "剪辑层", "plugin", "audio_infos,draft_url", "audio_ids,draft_url,track_id", role="bgm_track_write", dependency="CapCut Mate", status="fixture_observed"),
    _node(174651, "AIGC动画*", "剪辑层", "plugin", "draft_url,video_infos", "draft_url,segment_ids,segment_infos,track_id,video_ids", role="aigc_track_write", dependency="CapCut Mate", status="fixture_observed"),
    _node(116930, "数字人*", "剪辑层", "plugin", "draft_url,video_infos", "draft_url,segment_ids,segment_infos,track_id,video_ids", role="digital_human_track_write", dependency="CapCut Mate", status="fixture_observed"),
    _node(179989, "字幕*", "剪辑层", "plugin", "captions,draft_url,font,font_size,has_shadow,transform_y", "draft_url,segment_ids,segment_infos,text_ids,track_id", role="caption_track_write", dependency="CapCut Mate", status="fixture_observed"),
    _node(143635, "保存草稿", "剪辑层", "plugin", "draft_url", "draft_url,message", role="draft_save", dependency="CapCut Mate", status="transport_required"),
    _node(151394, "keyframes_infos", "剪辑层", "plugin", "ctype,offsets,segment_infos,values", "keyframes_infos", role="keyframe_plan", dependency="CapCut Mate", status="fixture_verified"),
    _node(115137, "add_keyframes", "剪辑层", "plugin", "draft_url,keyframes", "draft_url", role="keyframe_write", dependency="CapCut Mate", status="transport_required"),
    _node(1904923, "关键帧", "剪辑层", "plugin", "ctype,offsets,segment_infos,values", "keyframes_infos", role="keyframe_plan", dependency="CapCut Mate", status="fixture_verified"),
    _node(1962357, "关键帧+", "剪辑层", "plugin", "draft_url,keyframes", "draft_url", role="keyframe_write", dependency="CapCut Mate", status="transport_required"),
    _node(187358, "add_effects", "剪辑层", "plugin", "draft_url,effect_infos", "draft_url,effect_ids,segment_ids,track_id", role="effect_write", dependency="CapCut Mate", status="blocked_by_real_effect_validation"),
    _node(197721, "effect_infos", "剪辑层", "plugin", "effects,timelines", "infos", role="effect_plan", dependency="CapCut Mate", status="blocked_by_real_effect_validation"),
    _node(1922689, "add_effects_1", "剪辑层", "plugin", "draft_url,effect_infos", "draft_url,effect_ids,segment_ids,track_id", role="effect_write", dependency="CapCut Mate", status="blocked_by_real_effect_validation"),
    _node(1765956, "effect_infos_2", "剪辑层", "plugin", "effects,timelines", "infos", role="effect_plan", dependency="CapCut Mate", status="blocked_by_real_effect_validation"),
    _node(167309, "视频数据汇总", "剪辑层", "code", "url_list,new_timelines,public_video_url_list,timelines", "super_segments,super_timelines,super_url_list", role="track_data_merge", status="contract_only"),
    _node(179757, "转场音效", "剪辑层", "code", "super_timelines", "key0,key1,key2", role="transition_audio", status="contract_only"),
    _node(191914, "audio_infos", "剪辑层", "plugin", "mp3_urls,timelines", "infos", role="audio_track_plan", dependency="音频服务", status="contract_blocked_missing_mapping"),
    _node(900001, "结束", "剪辑层", "end", "draft_url", "content", role="workflow_exit", status="gate_required"),
)


MODULE_GATES: dict[str, dict[str, Any]] = {
    "编导层": {
        "handoff_manifest": "DirectorLockedManifest",
        "required_state": "DIRECTOR_LOCKED",
        "required_keys": ["group_id", "shot_id", "caption_id", "timeline", "requirement_id"],
        "rules": [
            "大镜头和小镜头数组保持原顺序并连续覆盖总时间线",
            "TTS 结果先冻结时间线，再允许素材层读取",
            "每个小镜头必须有素材需求，不在编导层写入剪映草稿",
        ],
    },
    "素材层": {
        "handoff_manifest": "AssetRegistry",
        "required_state": "ASSETS_READY",
        "mapping_state": "ASSET_MAPPING_ONLY",
        "required_keys": ["asset_id", "requirement_id", "actual_timeline", "status", "evidence_level"],
        "rules": [
            "素材实际时长与编导规划时长分开保存",
            "创建任务成功不等于素材成功，必须经过 query/poll",
            "17 个 AIGC 主路线与 18 个数字人替代资产不得强行一一对应",
        ],
    },
    "剪辑层": {
        "handoff_manifest": "EditManifest",
        "required_state": "DRAFT_VALIDATED",
        "mapping_state": "EDIT_MAPPING_ONLY",
        "required_keys": ["asset_id", "track_role", "planned_timeline", "edit_timeline", "draft_url"],
        "rules": [
            "每次写入插件都必须接收并返回最新 draft_url",
            "轨道时间线不能用素材规划数据冒充实际写入结果",
            "特效默认关闭；未完成真实参数验收前不得进入正式基线",
            "结束节点只能返回保存草稿节点的非空 draft_url",
        ],
    },
}


def validate_node_registry(nodes: Iterable[NodeContract] = NODE_CONTRACTS) -> dict[str, Any]:
    """验证节点注册表完整性，返回可写入日志的摘要。"""

    node_list = list(nodes)
    ids = [node.node_id for node in node_list]
    if len(ids) != len(set(ids)):
        raise ValueError("节点注册表存在重复 node_id")
    parent_ids = {node.node_id for node in node_list}
    for node in node_list:
        if node.parent_node_id and node.parent_node_id not in parent_ids:
            raise ValueError(f"{node.node_id} 的父批处理节点不存在：{node.parent_node_id}")
    counts = {module: sum(node.module == module for node in node_list) for module in MODULES}
    if sum(counts.values()) != len(node_list):
        raise ValueError("存在未归属三大业务模块的节点")
    return {
        "total_nodes": len(node_list),
        "module_counts": counts,
        "node_ids": ids,
        "parent_node_count": sum(node.parent_node_id is not None for node in node_list),
    }


def get_module_nodes(module: str) -> tuple[NodeContract, ...]:
    if module not in MODULES:
        raise ValueError(f"未知业务模块：{module}")
    return tuple(node for node in NODE_CONTRACTS if node.module == module)


def build_module_governance_snapshot(manifest: Any) -> dict[str, Any]:
    """把当前 ProjectManifest 转成三层治理状态快照。"""

    registry = validate_node_registry()
    director = manifest.director
    assets = manifest.assets
    edit = manifest.edit
    return {
        "project_id": manifest.project_id,
        "run_id": manifest.run_id,
        "registry": registry,
        "modules": {
            module: {
                "node_count": len(get_module_nodes(module)),
                "nodes": [node.to_dict() for node in get_module_nodes(module)],
                "gate": dict(MODULE_GATES[module]),
            }
            for module in MODULES
        },
        "observed_data": {
            "director_groups": len(director.groups),
            "director_shots": len(director.shots),
            "captions": len(director.captions),
            "asset_records": len(assets.records),
            "edit_bindings": len(edit.bindings),
        },
        "gate_results": {
            "编导层_mapping_gate": director.status == "DIRECTOR_LOCKED",
            "素材层_mapping_gate": assets.status == "ASSET_MAPPING_ONLY",
            "素材层_production_gate": assets.status in {"ASSETS_READY", "ASSETS_READY_WITH_WARNINGS"},
            "剪辑层_mapping_gate": edit.status == "EDIT_MAPPING_ONLY",
            "剪辑层_production_gate": edit.status in {"DRAFT_STRUCTURALLY_VALID", "DRAFT_VALIDATED"},
        },
    }


__all__ = [
    "MODULES",
    "MODULE_GATES",
    "NODE_CONTRACTS",
    "NodeContract",
    "build_module_governance_snapshot",
    "get_module_nodes",
    "validate_node_registry",
]
