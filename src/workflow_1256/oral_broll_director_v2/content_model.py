"""编导 v2 画面创意层：模型给景别/视角/构图/运镜建议，代码只做白名单校验。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


SHOT_SIZE_CHOICES = frozenset({"超远景", "远景", "全景", "中景", "近景", "特写", "极特写"})
CARRIER_MODE_CHOICES = frozenset({"完整人物", "人物局部", "物件", "空间", "建筑环境"})
VIEWPOINT_CHOICES = frozenset({"平视广角", "平视正面", "平视侧面", "侧前方平视", "侧面近距离", "俯拍"})
COMPOSITION_CHOICES = frozenset(
    {"纵向主体—物件关系", "边缘压力切入", "斜向纵深", "遮挡或框景", "多人层级", "局部焦点与留白"}
)
CAMERA_MOTION_CHOICES = frozenset({"固定机位", "轻微横移", "轻微推近", "轻微拉远", "跟拍"})

SYSTEM_PROMPT = (
    "你是口播短视频的画面创意层。只负责给每个镜头建议景别、视角、构图、运镜和画面意图，"
    "不负责时间、媒介和收口字段。输出 JSON：proposals 对象，key 为 shot_id；"
    "每个 proposal 包含 shot_size、carrier_mode、viewpoint、composition、camera_motion、visual_intent。"
    "取值只能从以下白名单选择："
    f"shot_size={sorted(SHOT_SIZE_CHOICES)}；carrier_mode={sorted(CARRIER_MODE_CHOICES)}；"
    f"viewpoint={sorted(VIEWPOINT_CHOICES)}；composition={sorted(COMPOSITION_CHOICES)}；"
    f"camera_motion={sorted(CAMERA_MOTION_CHOICES)}。"
)


class ContentModelError(ValueError):
    """画面创意层输入或输出不满足契约。"""


def build_content_prompt(shots: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "shot_id": shot.get("shot_id"),
            "narration": str(shot.get("narration_text") or ""),
            "visual_task": str(shot.get("visual_task") or ""),
            "media_type": str(shot.get("media_type") or ""),
            "segment_type": str(shot.get("segment_type") or ""),
            "explanation_mode": str(shot.get("explanation_mode") or ""),
            "duration_seconds": round(int(shot["duration_us"]) / 1_000_000, 3),
            "section": str(shot.get("section") or ""),
        }
        for shot in shots
    ]
    return json.dumps({"task": "visual_direction_proposal", "shots": payload}, ensure_ascii=False, indent=2)


def validate_content_plan(plan: Mapping[str, Any], shot_ids: Sequence[str]) -> list[str]:
    errors: list[str] = []
    proposals = plan.get("proposals")
    if not isinstance(proposals, Mapping):
        return ["content_plan.proposals 必须是对象"]
    for shot_id in shot_ids:
        proposal = proposals.get(shot_id)
        if not isinstance(proposal, Mapping):
            errors.append(f"proposals 缺少 {shot_id}")
            continue
        if proposal.get("shot_size") not in SHOT_SIZE_CHOICES:
            errors.append(f"{shot_id}.shot_size 不在白名单")
        if proposal.get("carrier_mode") not in CARRIER_MODE_CHOICES:
            errors.append(f"{shot_id}.carrier_mode 不在白名单")
        if proposal.get("viewpoint") not in VIEWPOINT_CHOICES:
            errors.append(f"{shot_id}.viewpoint 不在白名单")
        if proposal.get("composition") not in COMPOSITION_CHOICES:
            errors.append(f"{shot_id}.composition 不在白名单")
        if proposal.get("camera_motion") not in CAMERA_MOTION_CHOICES:
            errors.append(f"{shot_id}.camera_motion 不在白名单")
    return errors


class RuleContentModel:
    """确定性画面创意模型：规则引擎的视觉方向逻辑，作为无模型/兜底实现。"""

    name = "rule"

    def plan(self, shots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        proposals: dict[str, Any] = {}
        sequence_count = len(shots)
        for sequence_index, shot in enumerate(shots):
            text = str(shot["narration_text"]).strip()
            task = str(shot["visual_task"])
            media_type = str(shot["media_type"])
            has_person = any(token in text for token in ("人", "他", "她", "你", "创业者", "同事", "人物"))
            has_object = any(token in text for token in ("电脑", "手机", "文件", "方案", "海报", "键盘", "饭", "工具", "数据"))
            if media_type in {"digital_human_video", "overlay_explanation"}:
                shot_size, carrier, viewpoint, composition = "中景", "完整人物", "平视正面", "局部焦点与留白"
                camera_motion = "固定机位"
            elif media_type == "explanation_video":
                shot_size, carrier, viewpoint, composition = "全景", "空间", "平视正面", "局部焦点与留白"
                camera_motion = "固定机位"
            elif task in {"表现过程", "还原案例"}:
                shot_size = "中景" if sequence_index % 2 == 0 else "近景"
                carrier = "人物局部" if has_person else "物件"
                viewpoint = "侧前方平视" if carrier == "人物局部" else "俯拍"
                composition = "斜向纵深" if sequence_index % 2 == 0 else "纵向主体—物件关系"
                camera_motion = "跟拍" if media_type == "aigc_video" else "固定机位"
            elif task == "表现对比":
                shot_size, carrier, viewpoint, composition = "全景", "空间", "平视侧面", "斜向纵深"
                camera_motion = "轻微横移" if media_type == "aigc_video" else "固定机位"
            elif task == "强化情绪":
                shot_size, carrier, viewpoint, composition = "近景", "人物局部", "侧面近距离", "局部焦点与留白"
                camera_motion = "轻微推近" if media_type == "aigc_video" else "固定机位"
            elif task == "呈现结果":
                shot_size, carrier, viewpoint, composition = "全景", "空间", "平视正面", "遮挡或框景"
                camera_motion = "轻微拉远" if media_type == "aigc_video" else "固定机位"
            elif has_object:
                shot_size, carrier, viewpoint, composition = "特写", "物件", "俯拍", "纵向主体—物件关系"
                camera_motion = "固定机位"
            else:
                shot_size, carrier, viewpoint, composition = "中景", "空间", "平视侧面", "局部焦点与留白"
                camera_motion = "固定机位"
            if sequence_count >= 5 and sequence_index == 0:
                shot_size, carrier, viewpoint, composition = "远景", "建筑环境", "平视广角", "斜向纵深"
            proposals[shot["shot_id"]] = {
                "shot_size": shot_size,
                "carrier_mode": carrier,
                "viewpoint": viewpoint,
                "composition": composition,
                "camera_motion": camera_motion,
                "visual_intent": f"围绕“{text[:42]}”完成{task}，画面只服务于口播信息。",
            }
        return {"proposals": proposals}


class ModelCallableContentModel:
    """把任意可调用模型包装成画面创意模型：callable(system_prompt, user_prompt) -> JSON 文本。"""

    def __init__(self, callable: Callable[[str, str], str], *, name: str = "custom_content") -> None:
        self.callable = callable
        self.name = name

    def plan(self, shots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        raw = self.callable(SYSTEM_PROMPT, build_content_prompt(shots))
        if not isinstance(raw, str) or not raw.strip():
            raise ContentModelError("画面创意模型返回空内容")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContentModelError(f"画面创意模型返回非 JSON：{exc}") from exc
        if not isinstance(data, Mapping):
            raise ContentModelError("画面创意模型返回内容必须是对象")
        return data


def run_content_model_with_retry(
    model: Any,
    shots: Sequence[Mapping[str, Any]],
    *,
    max_retries: int = 2,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """模型输出不在白名单或格式错误时重试，仍失败则回退规则画面创意模型。"""

    shot_ids = [str(shot["shot_id"]) for shot in shots]
    meta: dict[str, Any] = {
        "model": getattr(model, "name", "unknown"),
        "retried": False,
        "errors": [],
        "fallback": None,
    }
    for _ in range(max_retries + 1):
        try:
            plan = model.plan(shots)
            errors = validate_content_plan(plan, shot_ids)
            if not errors:
                return plan, meta
            meta["errors"] = errors
        except Exception as exc:  # noqa: BLE001 - 模型失败统一进入重试/兜底
            meta["errors"] = [str(exc)]
        meta["retried"] = True
    fallback = RuleContentModel()
    plan = fallback.plan(shots)
    meta["fallback"] = fallback.name
    return plan, meta


def build_content_model(
    name: str = "rule",
    *,
    transport: Callable[[str, str], str] | None = None,
    auth_document: str | Path | None = None,
) -> Any:
    """模型选择入口：rule 为规则引擎，seed21_turbo 为生产文本模型，也可注入任意 transport。"""

    model_name = (name or "rule").strip().lower()
    if model_name == "rule":
        return RuleContentModel()
    if transport is not None:
        return ModelCallableContentModel(transport, name=model_name)
    if model_name == "seed21_turbo":
        if auth_document is None:
            raise ContentModelError("seed21_turbo 需要提供 auth_document")
        try:
            from tools.run_locked_director_text_live import DualSeed21TurboTextTransport
        except Exception as exc:  # noqa: BLE001
            raise ContentModelError(f"无法加载 Seed 2.1 turbo transport：{exc}") from exc
        return ModelCallableContentModel(
            DualSeed21TurboTextTransport(Path(auth_document)),
            name=model_name,
        )
    raise ContentModelError(f"未知画面创意模型：{model_name}")
