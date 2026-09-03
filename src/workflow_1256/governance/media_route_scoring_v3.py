"""V3.1 编导包的五类镜头评分、轨道契约与最终媒体路由锁。

该模块只在 DirectorLockedManifest 之后工作：不改写剪映 STT 字幕、微秒
时间线或镜头 ID。模型只提供逐镜语义特征和中文证据；所有候选得分、
资格判断和最终 shot_class 均由本模块确定性计算。全片比例、连续镜头和
数字人曝光只进入质检报告，不反向修改单镜头分类。
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

V3_SHOT_CLASSES = (
    "aigc",
    "image",
    "digital_human",
    "explanation",
    "mixed_explanation",
)
SHOT_CLASS_LABELS = {
    "aigc": "AIGC 视频",
    "image": "静态图片",
    "digital_human": "数字人视频",
    "explanation": "说明镜头",
    "mixed_explanation": "混合说明镜头",
}
MEDIA_TYPE_BY_SHOT_CLASS = {
    "aigc": "aigc_video",
    "image": "static_image",
    "digital_human": "digital_human_video",
    "explanation": "explanation",
    "mixed_explanation": "mixed_explanation",
}
RELATION_TEMPLATE_BY_TYPE = {
    "causal": "causal_chain",
    "comparison": "contrast_split",
    "sequence": "process_steps",
    "hierarchy": "hierarchy_layers",
    "timeline": "timeline_path",
    "data_trend": "data_trend",
    "concept": "concept_map",
}
RELATION_MARKERS = (
    ("comparison", ("对比", "相比", "而非", "不是", "差异", "高于", "低于", "更多", "更少")),
    ("causal", ("因为", "因此", "所以", "导致", "原因", "结果", "从而", "使得", "于是")),
    ("sequence", ("首先", "其次", "然后", "接着", "随后", "最后", "步骤", "流程", "先", "再")),
    ("hierarchy", ("层级", "结构", "组成", "分为", "包括", "核心", "底层", "上层")),
    ("timeline", ("过去", "如今", "之后", "此前", "当年", "后来", "年份", "年", "月", "日", "从", "到")),
    ("data_trend", ("增长", "下降", "比例", "百分", "%", "数据", "达到", "翻倍", "减少")),
    ("concept", ("本质", "意味着", "关键", "逻辑", "原则", "问题", "方法", "解法")),
)
V3_1_SCORE_POLICY_VERSION = "media_route_scoring_v3_1"
CLASSIFICATION_FEATURE_KEYS = (
    "motion_need",
    "temporal_dependency",
    "static_completeness",
    "visual_evidence",
    "relation_strength",
    "information_density",
    "presenter_value",
    "production_feasibility",
)
CLASS_CORE_FEATURES = {
    "aigc": ("motion_need", "temporal_dependency"),
    "image": ("static_completeness", "visual_evidence"),
    "digital_human": ("presenter_value", "information_density"),
    "explanation": ("relation_strength", "information_density"),
    "mixed_explanation": ("relation_strength", "presenter_value"),
}
CLASS_STABLE_ORDER = {
    shot_class: index for index, shot_class in enumerate(V3_SHOT_CLASSES)
}

# 混合说明是说明镜头的子类，不是与说明镜头并列的普通候选。只有在
# 逻辑关系和主播出镜需求都明显成立，且混合方案相对纯说明有足够增益时，
# 才允许使用数字人画中画；否则必须回到纯说明镜头。
MIXED_EXPLANATION_MIN_RELATION_STRENGTH = 60
MIXED_EXPLANATION_MIN_PRESENTER_VALUE = 70
MIXED_EXPLANATION_MIN_SCORE_LEAD = 5.0

# 每个候选独立满分 100。权重均为百分比，Python 负责唯一计算来源。
V3_1_SCORE_WEIGHTS = {
    "aigc": {
        "motion_need": 35,
        "temporal_dependency": 30,
        "visual_evidence": 20,
        "production_feasibility": 15,
    },
    "image": {
        "static_completeness": 35,
        "visual_evidence": 25,
        "temporal_dependency_inverse": 20,
        "information_density": 10,
        "production_feasibility": 10,
    },
    "digital_human": {
        "presenter_value": 45,
        "information_density": 15,
        "static_completeness": 10,
        "visual_evidence": 10,
        "production_feasibility": 20,
    },
    "explanation": {
        "relation_strength": 40,
        "information_density": 25,
        "static_completeness": 15,
        "motion_need_inverse": 10,
        "production_feasibility": 10,
    },
    "mixed_explanation": {
        "relation_strength": 30,
        "presenter_value": 30,
        "information_density": 15,
        "visual_evidence": 10,
        "production_feasibility": 15,
    },
}

DEFAULT_DYNAMIC_PARAMS: dict[str, Any] = {
    "opening_video_bonus": 15,
    "opening_image_penalty": 15,
    "static_streak_warning": 3,
    "static_streak_force": 4,
    "static_streak_force_bonus": 30,
    "aigc_target_ratio": 0.55,
    "image_target_ratio": 0.25,
    "explanation_target_ratio": 0.20,
    "digital_human_max_exposure_ratio": 0.15,
    "mixed_pip_exposure_ratio": 1.0,
    "beam_width": 16,
}


class MediaRouteScoringV3Error(ValueError):
    """V3.1 五类镜头路由、动态参数或轨道契约不合法。"""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MediaRouteScoringV3Error(f"{field} 必须是对象")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise MediaRouteScoringV3Error(f"{field} 必须是数组")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaRouteScoringV3Error(f"{field} 必须是非空字符串")
    return value.strip()


def _feature_number(value: Any, field: str) -> int:
    """严格读取模型特征：bool、浮点和数字字符串都不能混入契约。"""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise MediaRouteScoringV3Error(
            f"FEATURE_CONTRACT_INVALID: {field} 必须是 0-100 的整数"
        )
    return value


def _validate_feature_record(value: Any, field: str, *, shot_id: str | None = None) -> dict[str, Any]:
    record = _mapping(value, field)
    actual_shot_id = _text(record.get("shot_id"), f"{field}.shot_id")
    if shot_id is not None and actual_shot_id != shot_id:
        raise MediaRouteScoringV3Error(
            f"FEATURE_CONTRACT_INVALID: {field}.shot_id 必须是 {shot_id}"
        )
    normalized: dict[str, Any] = {"shot_id": actual_shot_id}
    for key in CLASSIFICATION_FEATURE_KEYS:
        normalized[key] = _feature_number(record.get(key), f"{field}.{key}")
    evidence = _mapping(record.get("evidence"), f"{field}.evidence")
    normalized_evidence: dict[str, str] = {}
    for key in CLASSIFICATION_FEATURE_KEYS:
        evidence_value = evidence.get(key)
        if (
            not isinstance(evidence_value, str)
            or not evidence_value.strip()
            or not re.search(r"[\u4e00-\u9fff]", evidence_value)
        ):
            raise MediaRouteScoringV3Error(
                f"FEATURE_CONTRACT_INVALID: {field}.evidence.{key} 必须是非空中文证据"
            )
        normalized_evidence[key] = evidence_value.strip()
    normalized["evidence"] = normalized_evidence
    # 可选字段仅用于完全同分时的裁决；未提供时不影响分数，采用特征分作为
    # 确定性的置信度代理，避免模型偷偷引入第二套权重。
    raw_confidence = record.get("feature_confidence")
    if raw_confidence is not None:
        confidence = _mapping(raw_confidence, f"{field}.feature_confidence")
        normalized_confidence: dict[str, int] = {}
        for key in CLASSIFICATION_FEATURE_KEYS:
            normalized_confidence[key] = _feature_number(
                confidence.get(key), f"{field}.feature_confidence.{key}"
            )
        normalized["feature_confidence"] = normalized_confidence
    else:
        normalized["feature_confidence"] = {
            key: normalized[key] for key in CLASSIFICATION_FEATURE_KEYS
        }
    return normalized


def validate_classification_features(
    value: Any, shot_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """校验并按冻结 shot_id 顺序返回 V3.1 特征审计。"""

    if isinstance(value, Mapping):
        rows = value.get("classification_features")
        if rows is None:
            rows = value.get("features")
    else:
        rows = value
    if not isinstance(rows, list) or not rows:
        raise MediaRouteScoringV3Error(
            "FEATURE_CONTRACT_INVALID: classification_features 必须是非空数组"
        )
    normalized = [_validate_feature_record(item, f"classification_features[{i}]") for i, item in enumerate(rows)]
    ids = [item["shot_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise MediaRouteScoringV3Error("FEATURE_CONTRACT_INVALID: classification_features 存在重复 shot_id")
    if shot_ids is not None:
        expected = list(shot_ids)
        if ids != expected:
            raise MediaRouteScoringV3Error(
                "FEATURE_CONTRACT_INVALID: classification_features 的 shot_id、顺序必须与冻结镜头一致"
            )
    return normalized


def _timeline(value: Any, field: str) -> dict[str, int]:
    raw = _mapping(value, field)
    start_us = raw.get("start_us", raw.get("start"))
    end_us = raw.get("end_us", raw.get("end"))
    if isinstance(start_us, bool) or not isinstance(start_us, int):
        raise MediaRouteScoringV3Error(f"{field}.start_us 必须是整数微秒")
    if isinstance(end_us, bool) or not isinstance(end_us, int) or end_us <= start_us:
        raise MediaRouteScoringV3Error(f"{field}.end_us 必须大于 start_us")
    return {"start_us": start_us, "end_us": end_us, "duration_us": end_us - start_us}


def _number(value: Any, field: str, *, minimum: float, maximum: float, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MediaRouteScoringV3Error(f"动态参数 {field} 必须是数字")
    result = int(value) if integer else float(value)
    if result < minimum or result > maximum:
        raise MediaRouteScoringV3Error(f"动态参数 {field} 必须在 {minimum} 到 {maximum} 之间")
    return result


def normalize_v3_dynamic_params(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """只接受已声明的动态参数，避免拼写错误被悄悄忽略。"""

    incoming = dict(value or {})
    unknown = sorted(set(incoming).difference(DEFAULT_DYNAMIC_PARAMS))
    if unknown:
        raise MediaRouteScoringV3Error("不支持的 v3 动态参数：" + "、".join(unknown))
    result = dict(DEFAULT_DYNAMIC_PARAMS)
    for key, raw in incoming.items():
        if key in {"opening_video_bonus", "opening_image_penalty", "static_streak_force_bonus"}:
            result[key] = _number(raw, key, minimum=0, maximum=40, integer=True)
        elif key in {"static_streak_warning", "static_streak_force"}:
            result[key] = _number(raw, key, minimum=1, maximum=8, integer=True)
        elif key in {"aigc_target_ratio", "image_target_ratio", "explanation_target_ratio", "digital_human_max_exposure_ratio", "mixed_pip_exposure_ratio"}:
            result[key] = _number(raw, key, minimum=0, maximum=1)
        elif key == "beam_width":
            result[key] = _number(raw, key, minimum=1, maximum=64, integer=True)
    if int(result["static_streak_force"]) < int(result["static_streak_warning"]):
        raise MediaRouteScoringV3Error("动态参数 static_streak_force 不能小于 static_streak_warning")
    total_target = sum(float(result[key]) for key in ("aigc_target_ratio", "image_target_ratio", "explanation_target_ratio"))
    if not math.isclose(total_target, 1.0, abs_tol=0.001):
        raise MediaRouteScoringV3Error("AIGC、图片、说明主画面目标比例之和必须为 1")
    return result


def dynamic_params_schema() -> dict[str, Any]:
    """提供给页面显示与保存的参数说明。

    旧参数仍可读取，保证历史任务和设置页面可打开；V3.1 选择器不会使用
    它们改变任何单镜头得分。比例、连续镜头阈值和曝光阈值只用于最终质检。
    """

    return {
        "format": "JSON object",
        "defaults": dict(DEFAULT_DYNAMIC_PARAMS),
        "fields": {
            "opening_video_bonus": "历史参数，仅记录，不参与 V3.1 单镜头选择",
            "opening_image_penalty": "历史参数，仅记录，不参与 V3.1 单镜头选择",
            "static_streak_warning": "连续静态图片质检预警阈值",
            "static_streak_force": "连续静态图片质检强提示阈值",
            "static_streak_force_bonus": "历史参数，仅记录，不参与 V3.1 单镜头选择",
            "aigc_target_ratio": "AIGC 主画面质检参考比例，不改分类",
            "image_target_ratio": "静态图片主画面质检参考比例，不改分类",
            "explanation_target_ratio": "说明主画面质检参考比例，不改分类",
            "digital_human_max_exposure_ratio": "数字人曝光质检参考上限，不改分类",
            "mixed_pip_exposure_ratio": "混合说明内数字人画中画覆盖比例",
            "beam_width": "历史参数，仅记录，不参与 V3.1 单镜头选择",
        },
    }


def _source_text(shot: Mapping[str, Any]) -> str:
    production = shot.get("production_spec") if isinstance(shot.get("production_spec"), Mapping) else {}
    story_mapping = production.get("story_mapping") if isinstance(production.get("story_mapping"), Mapping) else {}
    values = (
        shot.get("source_text"), shot.get("story_beat"), shot.get("clip_role"),
        production.get("narration_text"), production.get("semantic_mapping"),
        story_mapping.get("semantic_mapping"), story_mapping.get("silent_action"),
    )
    return " ".join(str(item).strip() for item in values if str(item or "").strip())


def _relation_type(text: str) -> str | None:
    for relation, markers in RELATION_MARKERS:
        if any(marker in text for marker in markers):
            if relation == "timeline" and not re.search(r"(?:\d{2,4}年|\d+月|\d+日|过去|如今|之后|此前|当年|后来|从.+到)", text):
                continue
            if relation == "data_trend" and not (re.search(r"\d", text) or any(marker in text for marker in ("增长", "下降", "比例", "百分"))):
                continue
            return relation
    return None


def _relation_objects(text: str) -> list[str]:
    pieces = [item.strip(" ，,；;。！？!?：:") for item in re.split(r"[，,；;。！？!?：:]", text) if item.strip(" ，,；;。！？!?：:")]
    result: list[str] = []
    for item in pieces:
        if item not in result:
            result.append(item[:32])
        if len(result) == 4:
            break
    return result[:4]


def _score_from_features(shot_class: str, features: Mapping[str, int]) -> float:
    weights = V3_1_SCORE_WEIGHTS[shot_class]
    total = 0.0
    for key, weight in weights.items():
        if key == "temporal_dependency_inverse":
            value = 100 - int(features["temporal_dependency"])
        elif key == "motion_need_inverse":
            value = 100 - int(features["motion_need"])
        else:
            value = int(features[key])
        total += value * float(weight) / 100.0
    return round(total, 2)


def _core_confidence(shot_class: str, features: Mapping[str, Any]) -> float:
    keys = CLASS_CORE_FEATURES[shot_class]
    confidence = features.get("feature_confidence") or features
    return sum(float(confidence.get(key, 0)) for key in keys) / len(keys)


def _candidate_eligibility(
    shot_class: str, features: Mapping[str, int], relation: str | None,
    relation_objects: Sequence[str], *, index: int,
) -> tuple[bool, str]:
    if shot_class == "aigc":
        return True, "AIGC 默认具备资格；首镜另受 AIGC 硬约束。"
    if shot_class == "image":
        if int(features["temporal_dependency"]) >= 90 and int(features["static_completeness"]) < 30:
            return False, "时间过程依赖达到 90 且单张表达完整度低于 30，排除静态图片。"
        return True, "静态图片默认具备资格；存在动作只影响分数，不取消资格。"
    if shot_class == "digital_human":
        eligible = int(features["presenter_value"]) >= 40
        return eligible, "主播/可信度特征达到 40。" if eligible else "主播/可信度特征低于 40。"
    if shot_class == "explanation":
        eligible = int(features["relation_strength"]) >= 50 and len(relation_objects) >= 2
        if eligible:
            return True, "关系强度达到 50 且检测到不少于两个关系对象。"
        return False, "需要关系强度达到 50 且关系对象不少于 2 个。"
    eligible = (
        relation is not None
        and int(features["relation_strength"]) >= MIXED_EXPLANATION_MIN_RELATION_STRENGTH
        and int(features["presenter_value"]) >= MIXED_EXPLANATION_MIN_PRESENTER_VALUE
        and len(relation_objects) >= 2
    )
    if eligible:
        return (
            True,
            "混合说明子类要求关系强度达到 60、主播价值达到 70，且关系对象不少于 2 个；"
            "最终还需比纯说明至少领先 5 分。",
        )
    return (
        False,
        "混合说明是说明镜头子类，需要关系强度达到 60、主播价值达到 70，"
        "且关系对象不少于 2 个；最终还需比纯说明至少领先 5 分。",
    )


def _apply_mixed_explanation_subtype_gate(
    candidates: dict[str, dict[str, Any]],
) -> None:
    """把混合说明限制为纯说明之上的必要增强，而不是平行兜底类型。"""

    mixed = candidates["mixed_explanation"]
    explanation = candidates["explanation"]
    if not mixed["eligible"]:
        return
    if not explanation["eligible"]:
        mixed["eligible"] = False
        mixed["eligibility_reason"] = "混合说明属于说明镜头子类，但纯说明候选本身未通过资格。"
        return
    score_lead = round(float(mixed["score"]) - float(explanation["score"]), 2)
    if score_lead < MIXED_EXPLANATION_MIN_SCORE_LEAD:
        mixed["eligible"] = False
        mixed["eligibility_reason"] = (
            "混合说明属于说明镜头子类，必须比纯说明至少领先 5 分；"
            f"当前仅领先 {score_lead:.2f} 分，回落为纯说明候选。"
        )


def _score_candidates(
    shot: Mapping[str, Any], context: Mapping[str, Any], timeline: Mapping[str, int], *, index: int,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    del timeline  # 时间线在调用方严格校验；V3.1 公式不读取时长或全片状态。
    shot_id = _text(shot.get("shot_id"), "shot.shot_id")
    feature_record = _validate_feature_record(
        context.get("classification_features"),
        f"{shot_id}.classification_features",
        shot_id=shot_id,
    )
    features = feature_record
    source = _source_text(shot)
    relation = _relation_type(source)
    relation_objects = _relation_objects(source) if relation else []
    base: dict[str, dict[str, Any]] = {}

    def put(shot_class: str) -> None:
        eligible, eligibility_reason = _candidate_eligibility(
            shot_class, features, relation, relation_objects, index=index,
        )
        score = _score_from_features(shot_class, features)
        base[shot_class] = {
            "eligible": eligible,
            "eligibility_reason": eligibility_reason,
            "criterion_scores": {
                key: int(features[key]) for key in CLASSIFICATION_FEATURE_KEYS
            },
            "base_score": score,
            "score": score,
            "core_confidence": round(_core_confidence(shot_class, features), 2),
        }

    for shot_class in V3_SHOT_CLASSES:
        put(shot_class)
    _apply_mixed_explanation_subtype_gate(base)
    return base, relation


def _pip_track(timeline: Mapping[str, int], params: Mapping[str, Any]) -> dict[str, Any]:
    duration_us = int(timeline["duration_us"])
    visible_us = int(round(duration_us * float(params["mixed_pip_exposure_ratio"])))
    visible_us = max(1, min(duration_us, visible_us))
    pip_start_us = int(timeline["start_us"])
    return {
        "type": "digital_human_pip",
        "digital_human_asset_ref": "task_material_input:digital_human",
        "pip_time_range": {
            "start_us": pip_start_us,
            "end_us": pip_start_us + visible_us,
        },
        "anchor": "top_right",
        "safe_area": "不得遮挡剪映 STT 字幕和核心关系节点",
        "purpose": "补充解释 / 观点强调 / 建立可信度",
    }


def _render_plan(shot_class: str, relation_type: str | None, relation_objects: Sequence[str], timeline: Mapping[str, int], params: Mapping[str, Any]) -> dict[str, Any]:
    if shot_class not in {"explanation", "mixed_explanation"}:
        return {}
    if not relation_type or len(relation_objects) < 2:
        raise MediaRouteScoringV3Error("说明镜头缺少逻辑关系类型或关系对象")
    template_id = RELATION_TEMPLATE_BY_TYPE[relation_type]
    result: dict[str, Any] = {
        "relation_type": relation_type,
        "relation_objects": list(relation_objects),
        "template_id": template_id,
        "template_version": "v3.0",
        "main_track": "explanation_video",
        "overlay_tracks": [],
    }
    if shot_class == "mixed_explanation":
        result["overlay_tracks"] = [_pip_track(timeline, params)]
    return result


def _required_assets(shot_class: str, render_plan: Mapping[str, Any]) -> list[dict[str, str]]:
    if shot_class == "aigc":
        return [
            {"asset": "first_frame_image", "action": "create", "source": "首帧生成"},
            {"asset": "aigc_video", "action": "create", "source": "AIGC动画"},
        ]
    if shot_class == "image":
        return [{"asset": "first_frame_image", "action": "create", "source": "首帧生成"}]
    if shot_class == "digital_human":
        return [{"asset": "digital_human_video", "action": "create", "source": "数字人"}]
    template = str(render_plan.get("template_id") or "")
    assets = [{"asset": "explanation_template", "action": "apply", "source": f"v3 风格包：{template}"}]
    if shot_class == "mixed_explanation":
        assets.append({"asset": "digital_human_video", "action": "create", "source": "数字人画中画"})
    return assets


def _skip_actions(shot_class: str) -> dict[str, bool]:
    return {
        "skip_first_frame": shot_class in {"digital_human", "explanation", "mixed_explanation"},
        "skip_aigc_video": shot_class != "aigc",
        "skip_video_prompt": shot_class != "aigc",
    }


def resolve_media_routes_v3(
    director_lock: Mapping[str, Any], governance: Mapping[str, Any], dynamic_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按逐镜 V3.1 特征独立评分，确定性选择单一路由。"""

    params = normalize_v3_dynamic_params(dynamic_params)
    lock = _mapping(director_lock, "director_lock")
    if lock.get("status") != "DIRECTOR_LOCKED":
        raise MediaRouteScoringV3Error("v3 媒体评分只接受 DIRECTOR_LOCKED 的编导清单")
    shots = _list(lock.get("shots"), "director_lock.shots")
    contexts = _list(governance.get("shot_contexts"), "director_governance.shot_contexts")
    if not shots or len(shots) != len(contexts):
        raise MediaRouteScoringV3Error("v3 镜头路由必须与冻结镜头逐条一一对应")
    prepared: list[dict[str, Any]] = []
    total_duration_us = 0
    feature_rows: list[dict[str, Any]] = []
    for index, (raw_shot, raw_context) in enumerate(zip(shots, contexts, strict=True)):
        shot = _mapping(raw_shot, f"director_lock.shots[{index}]")
        context = _mapping(raw_context, f"director_governance.shot_contexts[{index}]")
        shot_id = _text(shot.get("shot_id"), f"director_lock.shots[{index}].shot_id")
        if _text(context.get("shot_id"), f"shot_contexts[{index}].shot_id") != shot_id:
            raise MediaRouteScoringV3Error("v3 镜头上下文 shot_id 与冻结镜头顺序不一致")
        timeline = _timeline(shot.get("timeline"), f"{shot_id}.timeline")
        context_timeline = _timeline(context.get("timeline"), f"{shot_id}.context.timeline")
        if timeline != context_timeline:
            raise MediaRouteScoringV3Error(f"{shot_id} 的 v3 媒体路由不得改写冻结时间线")
        feature_record = _validate_feature_record(
            context.get("classification_features"),
            f"{shot_id}.classification_features",
            shot_id=shot_id,
        )
        feature_rows.append(feature_record)
        candidates, relation = _score_candidates(shot, context, timeline, index=index)
        prepared.append({
            "shot": shot, "context": context, "shot_id": shot_id, "timeline": timeline,
            "candidates": candidates, "relation_type": relation,
            "relation_objects": _relation_objects(_source_text(shot)) if relation else [],
        })
        total_duration_us += int(timeline["duration_us"])

    decisions: list[dict[str, Any]] = []
    static_streak = 0
    digital_exposure_us = 0
    previous_has_digital = False
    main_counts = {"aigc": 0, "image": 0, "explanation": 0}
    max_digital_exposure_us = int(total_duration_us * float(params["digital_human_max_exposure_ratio"]))
    for index, item in enumerate(prepared):
        candidates = item["candidates"]
        duration_us = int(item["timeline"]["duration_us"])
        # 低于 3 秒是编导层硬路由规则，不参与 V3.1 得分竞争：除首镜外
        # 必须由静态首帧承载。此前 V3.1 只按特征评分、忽略时长，导致
        # 2.x 秒镜头被错误选成 AIGC，直到素材层才被 Seedance 拒绝。
        forced_short_image = index != 0 and duration_us < 3_000_000
        if forced_short_image:
            eligible = ["image"]
            for shot_class in V3_SHOT_CLASSES:
                candidates[shot_class]["eligible"] = shot_class == "image"
                candidates[shot_class]["eligibility_reason"] = (
                    "低于 3 秒的非首镜硬规则：必须使用静态图片。"
                    if shot_class == "image"
                    else "低于 3 秒的非首镜禁止使用该媒体路线。"
                )
        else:
            eligible = [shot_class for shot_class in V3_SHOT_CLASSES if candidates[shot_class]["eligible"]]
        if index == 0:
            # 首镜是业务硬约束；其它镜头不受全片状态干预。
            eligible = ["aigc"] if candidates["aigc"]["eligible"] else []
        if not eligible:
            raise MediaRouteScoringV3Error(
                f"{item['shot_id']} 没有通过 V3.1 硬性资格的镜头分类候选"
            )
        ranked = sorted(
            eligible,
            key=lambda shot_class: (
                -float(candidates[shot_class]["score"]),
                -float(candidates[shot_class]["core_confidence"]),
                CLASS_STABLE_ORDER[shot_class],
            ),
        )
        selected_class = ranked[0]
        selected = candidates[selected_class]
        top_score = float(selected["score"])
        second_score = float(candidates[ranked[1]]["score"]) if len(ranked) > 1 else None
        score_gap = round(top_score - second_score, 2) if second_score is not None else None
        confidence = "HIGH" if second_score is None or score_gap >= 5 else "LOW"
        review_status = "NEEDS_REVIEW" if top_score < 60 else "PASS"
        render_plan = _render_plan(
            selected_class, item["relation_type"], item["relation_objects"], item["timeline"], params,
        )
        overlay_tracks = list(render_plan.get("overlay_tracks") or [])
        exposure_us = int(item["timeline"]["duration_us"]) if selected_class == "digital_human" else sum(
            int(track.get("pip_time_range", {}).get("end_us", 0))
            - int(track.get("pip_time_range", {}).get("start_us", 0))
            for track in overlay_tracks if isinstance(track, Mapping)
        )
        if selected_class == "image":
            static_streak += 1
        else:
            static_streak = 0
        has_digital = exposure_us > 0
        if has_digital:
            digital_exposure_us += exposure_us
        previous_has_digital = has_digital
        main_class = "explanation" if selected_class == "mixed_explanation" else selected_class
        if main_class in main_counts:
            main_counts[main_class] += 1
        reason = _reason(
            selected_class, item["relation_type"], index,
            score_gap=score_gap, confidence=confidence, review_status=review_status,
            forced_short_image=forced_short_image,
        )
        decisions.append({
            "shot_id": item["shot_id"],
            "shot_class": selected_class,
            "media_type": MEDIA_TYPE_BY_SHOT_CLASS[selected_class],
            "media_label": SHOT_CLASS_LABELS[selected_class],
            "media_reason": reason,
            "base_score": selected["base_score"],
            "criterion_scores": dict(selected["criterion_scores"]),
            "adjustments": [],
            "final_score": selected["score"],
            "classification_features": copy.deepcopy(feature_rows[index]),
            "candidate_scores": {
                shot_class: round(float(candidates[shot_class]["score"]), 2)
                for shot_class in V3_SHOT_CLASSES
            },
            "eligible_candidates": list(eligible),
            "candidate_details": {
                shot_class: {
                    "eligible": bool(candidates[shot_class]["eligible"]),
                    "eligibility_reason": candidates[shot_class]["eligibility_reason"],
                    "score": candidates[shot_class]["score"],
                    "core_confidence": candidates[shot_class]["core_confidence"],
                }
                for shot_class in V3_SHOT_CLASSES
            },
            "score_gap": score_gap,
            "confidence": confidence,
            "review_status": review_status,
            "selection_reason": reason,
            "score_policy_version": V3_1_SCORE_POLICY_VERSION,
            "render_plan": render_plan,
            "digital_human_exposure_us": exposure_us,
            "sequence_state_before": {
                "static_streak": static_streak - 1 if selected_class == "image" else 0,
                "digital_human_exposure_us": digital_exposure_us - exposure_us,
            },
        })
    total_score = round(sum(float(item["final_score"]) for item in decisions), 2)
    decisions_by_id = {str(item["shot_id"]): item for item in decisions}
    quality_checks = _quality_checks(
        decisions,
        total_duration_us=total_duration_us,
        digital_exposure_us=digital_exposure_us,
        max_digital_exposure_us=max_digital_exposure_us,
        previous_has_digital=previous_has_digital,
        params=params,
    )
    return {
        "policy_version": V3_1_SCORE_POLICY_VERSION,
        "dynamic_params": params,
        "decisions": decisions,
        "by_shot": decisions_by_id,
        "policy": {
            "hard_rules": {
                "first_shot_must_be_aigc": True,
                "short_non_opening_shot_must_be_static_image": True,
                "short_non_opening_shot_threshold_us": 3_000_000,
                "stt_timeline_immutable": True,
            },
            "full_film_score": round(total_score, 2),
            "total_duration_us": total_duration_us,
            "digital_human_max_exposure_us": max_digital_exposure_us,
            "digital_human_selected_exposure_us": int(digital_exposure_us),
            "main_counts": dict(main_counts),
            "quality_checks": quality_checks,
            "selection_policy": "per_shot_independent_feature_score",
            "global_distribution_adjustment": False,
            "static_streak_adjustment": False,
        },
    }


def _quality_checks(
    decisions: Sequence[Mapping[str, Any]], *, total_duration_us: int,
    digital_exposure_us: int, max_digital_exposure_us: int,
    previous_has_digital: bool, params: Mapping[str, Any],
) -> dict[str, Any]:
    counts = Counter(str(item.get("shot_class") or "") for item in decisions)
    total = len(decisions)
    ratios = {
        shot_class: round(counts[shot_class] / total, 4) if total else 0.0
        for shot_class in V3_SHOT_CLASSES
    }
    max_image_streak = 0
    current_image_streak = 0
    max_digital_streak = 0
    current_digital_streak = 0
    low_confidence: list[str] = []
    needs_review: list[str] = []
    previous_digital = False
    consecutive_digital: list[str] = []
    for decision in decisions:
        shot_id = str(decision.get("shot_id") or "")
        if decision.get("shot_class") == "image":
            current_image_streak += 1
            max_image_streak = max(max_image_streak, current_image_streak)
        else:
            current_image_streak = 0
        is_digital = decision.get("shot_class") in {"digital_human", "mixed_explanation"}
        if is_digital:
            current_digital_streak += 1
            max_digital_streak = max(max_digital_streak, current_digital_streak)
            if previous_digital:
                consecutive_digital.append(shot_id)
        else:
            current_digital_streak = 0
        previous_digital = is_digital
        if decision.get("confidence") == "LOW":
            low_confidence.append(shot_id)
        if decision.get("review_status") == "NEEDS_REVIEW":
            needs_review.append(shot_id)
    return {
        "class_counts": {shot_class: counts[shot_class] for shot_class in V3_SHOT_CLASSES},
        "class_ratios": ratios,
        "image_zero": total > 0 and counts["image"] == 0,
        "max_image_streak": max_image_streak,
        "image_streak_warning": max_image_streak >= int(params["static_streak_warning"]),
        "image_streak_force_warning": max_image_streak >= int(params["static_streak_force"]),
        "max_digital_streak": max_digital_streak,
        "consecutive_digital_human_shots": consecutive_digital,
        "low_confidence_shots": low_confidence,
        "needs_review_shots": needs_review,
        "first_shot_is_aigc": bool(decisions and decisions[0].get("shot_class") == "aigc"),
        "digital_human_exposure_us": int(digital_exposure_us),
        "digital_human_exposure_ratio": round(
            digital_exposure_us / total_duration_us, 4
        ) if total_duration_us else 0.0,
        "digital_human_exposure_over_limit": digital_exposure_us > max_digital_exposure_us,
        "timeline_and_order_immutable": all(
            str(item.get("shot_id") or "") for item in decisions
        ),
        "selection_only_uses_shot_features": True,
        "previous_digital_state": previous_has_digital,
    }


def _reason(
    shot_class: str, relation_type: str | None, index: int, *,
    score_gap: float | None = None, confidence: str = "LOW", review_status: str = "PASS",
    forced_short_image: bool = False,
) -> str:
    if forced_short_image:
        reason = "镜头低于 3 秒，编导层硬规则强制使用静态图片，跳过 AIGC。"
    elif index == 0:
        reason = "首镜硬约束：使用 AIGC 视频建立开场视觉锚点；"
    elif shot_class == "aigc":
        reason = "动态呈现需求、时间过程依赖等特征加权后，AIGC 视频评分最高。"
    elif shot_class == "image":
        reason = "单张图片表达完整度、视觉证据等特征加权后，静态图片评分最高。"
    elif shot_class == "digital_human":
        reason = "主播价值、信息密度等特征加权后，数字人视频评分最高。"
    elif shot_class == "explanation":
        reason = f"关系强度、信息密度等特征加权后，{relation_type or '逻辑'}说明镜头评分最高。"
    else:
        reason = f"关系强度、主播价值等特征加权后，{relation_type or '逻辑'}混合说明评分最高。"
    if score_gap is not None:
        reason += f" 领先第二名 {score_gap:.2f} 分，置信度 {confidence}。"
    else:
        reason += f" 只有一个合格候选，置信度 {confidence}。"
    if review_status == "NEEDS_REVIEW":
        reason += " 最高分低于 60，需人工复核。"
    return reason


def build_director_media_route_lock_v3(
    director_lock: Mapping[str, Any], governance: Mapping[str, Any], dynamic_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """将五类 V3.1 评分结果收口为下游唯一读取的媒体锁。"""

    resolved = resolve_media_routes_v3(director_lock, governance, dynamic_params)
    lock = _mapping(director_lock, "director_lock")
    shots = _list(lock.get("shots"), "director_lock.shots")
    routes: list[dict[str, Any]] = []
    for raw_shot in shots:
        shot = _mapping(raw_shot, "director_lock.shot")
        shot_id = _text(shot.get("shot_id"), "director_lock.shot.shot_id")
        decision = resolved["by_shot"][shot_id]
        shot_class = str(decision["shot_class"])
        render_plan = dict(decision.get("render_plan") or {})
        routes.append({
            "shot_id": shot_id,
            "group_id": _text(shot.get("group_id"), f"{shot_id}.group_id"),
            "timeline": _timeline(shot.get("timeline"), f"{shot_id}.timeline"),
            "shot_class": shot_class,
            "media_type": decision["media_type"],
            "media_label": decision["media_label"],
            "media_reason": decision["media_reason"],
            "policy_version": V3_1_SCORE_POLICY_VERSION,
            "base_score": decision["base_score"],
            "criterion_scores": dict(decision["criterion_scores"]),
            "adjustments": list(decision["adjustments"]),
            "final_score": decision["final_score"],
            "classification_features": copy.deepcopy(decision["classification_features"]),
            "candidate_scores": dict(decision["candidate_scores"]),
            "eligible_candidates": list(decision["eligible_candidates"]),
            "confidence": decision["confidence"],
            "review_status": decision["review_status"],
            "selection_reason": decision["selection_reason"],
            "score_gap": decision["score_gap"],
            "candidate_details": copy.deepcopy(decision["candidate_details"]),
            "score_policy_version": V3_1_SCORE_POLICY_VERSION,
            "required_assets": _required_assets(shot_class, render_plan),
            "skip": _skip_actions(shot_class),
            "editing_track": "main_visual",
            "coverage_policy": "fit_to_locked_shot_timeline",
            "render_plan": render_plan,
        })
    counts = Counter(str(route["shot_class"]) for route in routes)
    return {
        "schema_version": "director-media-route-lock-v3",
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
            "shot_class_counts": {shot_class: counts[shot_class] for shot_class in V3_SHOT_CLASSES},
            "digital_human_exposure_us": resolved["policy"]["digital_human_selected_exposure_us"],
            "policy_version": resolved["policy_version"],
        },
        "policy": resolved["policy"],
        "dynamic_params": resolved["dynamic_params"],
    }


def apply_v3_route_lock_to_governance(governance: Mapping[str, Any], route_lock: Mapping[str, Any]) -> dict[str, Any]:
    """把锁定路线写入治理快照，供 116616 和逐镜脚本读取，不回写 v2 输入。"""

    result = copy.deepcopy(dict(_mapping(governance, "governance")))
    contexts = _list(result.get("shot_contexts"), "governance.shot_contexts")
    routes = _list(route_lock.get("routes"), "route_lock.routes")
    if len(contexts) != len(routes):
        raise MediaRouteScoringV3Error("v3 路由与治理上下文数量不一致")
    for index, (raw_context, raw_route) in enumerate(zip(contexts, routes, strict=True)):
        context = _mapping(raw_context, f"governance.shot_contexts[{index}]")
        route = _mapping(raw_route, f"route_lock.routes[{index}]")
        if context.get("shot_id") != route.get("shot_id"):
            raise MediaRouteScoringV3Error("v3 路由与治理上下文 shot_id 不一致")
        updated = dict(context)
        updated["shot_class"] = route["shot_class"]
        updated["media_type"] = route["media_type"]
        updated["media_reason"] = route["media_reason"]
        updated["classification_features"] = copy.deepcopy(route.get("classification_features") or {})
        updated["candidate_scores"] = copy.deepcopy(route.get("candidate_scores") or {})
        updated["eligible_candidates"] = list(route.get("eligible_candidates") or [])
        updated["confidence"] = route.get("confidence")
        updated["review_status"] = route.get("review_status")
        updated["selection_reason"] = route.get("selection_reason")
        updated["score_policy_version"] = route.get("score_policy_version")
        updated["render_plan"] = copy.deepcopy(dict(route.get("render_plan") or {}))
        contexts[index] = updated
    result["shot_contexts"] = contexts
    result["media_route_policy"] = copy.deepcopy(dict(route_lock.get("policy") or {}))
    result["media_route_policy_version"] = V3_1_SCORE_POLICY_VERSION
    return result


__all__ = [
    "CLASSIFICATION_FEATURE_KEYS",
    "DEFAULT_DYNAMIC_PARAMS",
    "MEDIA_TYPE_BY_SHOT_CLASS",
    "MediaRouteScoringV3Error",
    "SHOT_CLASS_LABELS",
    "V3_1_SCORE_POLICY_VERSION",
    "V3_SHOT_CLASSES",
    "apply_v3_route_lock_to_governance",
    "build_director_media_route_lock_v3",
    "dynamic_params_schema",
    "normalize_v3_dynamic_params",
    "resolve_media_routes_v3",
    "validate_classification_features",
]
