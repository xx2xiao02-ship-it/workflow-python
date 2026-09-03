"""编导 v2 模型理解层：文案语义分段与视觉任务候选，模型可替换。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


SEMANTIC_ROLES = frozenset(
    {
        "钩子/问题建立",
        "结论/行动引导",
        "观点转折/对比",
        "案例或事实",
        "观点解释",
        "结论落点",
        "情绪强化",
        "观点推进",
    }
)
VISUAL_TASKS = frozenset(
    {
        "还原案例",
        "表现过程",
        "表现对比",
        "解释观点",
        "强化情绪",
        "呈现结果",
        "直接展示对象",
    }
)
SYSTEM_PROMPT = (
    "你是口播短视频的文案理解层。只负责语义分段和画面意图候选，不负责时间、媒介和收口字段。"
    "输出 JSON：segments 数组，与输入 segments 一一对应；每个元素包含 text（必须逐字等于输入段）、"
    "semantic_units 数组（候选镜头单元）；每个单元包含 text、role、visual_task、visual_intent。"
    "规则：单元按完整语义切分，句号/问号/感叹号/分号是强切点，引号内的句读不能拆开，"
    "右引号不能成为下一个单元的开头；所有单元按顺序拼接后必须逐字还原原段。"
    "role 只能取：钩子/问题建立、结论/行动引导、观点转折/对比、案例或事实、观点解释、结论落点、情绪强化、观点推进。"
    "visual_task 只能取：还原案例、表现过程、表现对比、解释观点、强化情绪、呈现结果、直接展示对象。"
)


class SemanticModelError(ValueError):
    """模型理解层输入或输出不满足契约。"""


def build_semantic_prompt(segments: Sequence[str]) -> str:
    return json.dumps(
        {"task": "semantic_segmentation", "segments": list(segments)},
        ensure_ascii=False,
        indent=2,
    )


def validate_semantic_plan(plan: Mapping[str, Any], segments: Sequence[str]) -> list[str]:
    """校验模型理解输出：无损还原、引号完整、枚举合法。"""

    errors: list[str] = []
    plan_segments = plan.get("segments")
    if not isinstance(plan_segments, list):
        return ["semantic_plan.segments 必须是数组"]
    if len(plan_segments) != len(segments):
        return [f"semantic_plan.segments 数量 {len(plan_segments)} != 输入段数量 {len(segments)}"]
    for index, (segment, item) in enumerate(zip(segments, plan_segments)):
        if not isinstance(item, Mapping):
            errors.append(f"segments[{index}] 不是对象")
            continue
        if str(item.get("text")) != segment:
            errors.append(f"segments[{index}] 文本与输入不一致")
        units = item.get("semantic_units")
        if not isinstance(units, list) or not units:
            errors.append(f"segments[{index}].semantic_units 必须是非空数组")
            continue
        joined = "".join(
            str(unit.get("text"))
            for unit in units
            if isinstance(unit, Mapping)
        )
        if joined != segment:
            errors.append(f"segments[{index}] 语义单元拼接未逐字还原原文")
        for unit_index, unit in enumerate(units):
            if not isinstance(unit, Mapping):
                errors.append(f"segments[{index}].semantic_units[{unit_index}] 不是对象")
                continue
            unit_text = str(unit.get("text"))
            if unit_text.lstrip().startswith(("”", '"', "」")):
                errors.append(f"segments[{index}].semantic_units[{unit_index}] 以孤立右引号开头")
            if unit.get("role") not in SEMANTIC_ROLES:
                errors.append(f"segments[{index}].semantic_units[{unit_index}].role 不在白名单")
            if unit.get("visual_task") not in VISUAL_TASKS:
                errors.append(f"segments[{index}].semantic_units[{unit_index}].visual_task 不在白名单")
    return errors


class RuleSemanticModel:
    """确定性语义模型：当前规则引擎的语义分段能力，作为无模型/兜底实现。"""

    name = "rule"

    def plan(self, segments: Sequence[str], timelines: Sequence[Mapping[str, int]]) -> dict[str, Any]:
        from .pipeline import _classify_semantic_role, _third_level_shot_parts, _visual_task

        result: dict[str, Any] = {"segments": []}
        for index, (segment, timeline) in enumerate(zip(segments, timelines)):
            duration_us = int(timeline["end"]) - int(timeline["start"])
            units = [
                {
                    "text": part,
                    "role": _classify_semantic_role(part),
                    "visual_task": _visual_task(part),
                    "visual_intent": f"围绕“{part.strip()}”完成{_visual_task(part)}，画面只服务于口播信息。",
                }
                for part in _third_level_shot_parts(segment, duration_us)
            ]
            result["segments"].append({"segment_index": index, "text": segment, "semantic_units": units})
        return result


class ModelCallableSemanticModel:
    """把任意可调用模型包装成语义模型：callable(system_prompt, user_prompt) -> JSON 文本。"""

    def __init__(self, callable: Callable[[str, str], str], *, name: str = "custom_semantic") -> None:
        self.callable = callable
        self.name = name

    def plan(self, segments: Sequence[str], timelines: Sequence[Mapping[str, int]]) -> dict[str, Any]:
        raw = self.callable(SYSTEM_PROMPT, build_semantic_prompt(segments))
        if not isinstance(raw, str) or not raw.strip():
            raise SemanticModelError("语义模型返回空内容")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SemanticModelError(f"语义模型返回非 JSON：{exc}") from exc
        if not isinstance(data, Mapping):
            raise SemanticModelError("语义模型返回内容必须是对象")
        return data


def run_semantic_model_with_retry(
    model: Any,
    segments: Sequence[str],
    timelines: Sequence[Mapping[str, int]],
    *,
    max_retries: int = 2,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """模型输出不通过契约校验时重试，仍失败则回退规则语义模型。"""

    meta: dict[str, Any] = {
        "model": getattr(model, "name", "unknown"),
        "retried": False,
        "errors": [],
        "fallback": None,
    }
    for _ in range(max_retries + 1):
        try:
            plan = model.plan(segments, timelines)
            errors = validate_semantic_plan(plan, segments)
            if not errors:
                return plan, meta
            meta["errors"] = errors
        except Exception as exc:  # noqa: BLE001 - 模型失败统一进入重试/兜底
            meta["errors"] = [str(exc)]
        meta["retried"] = True
    fallback = RuleSemanticModel()
    plan = fallback.plan(segments, timelines)
    meta["fallback"] = fallback.name
    return plan, meta


def build_semantic_model(
    name: str = "rule",
    *,
    transport: Callable[[str, str], str] | None = None,
    auth_document: str | Path | None = None,
) -> Any:
    """模型选择入口：rule 为规则引擎，seed21_turbo 为生产文本模型，也可注入任意 transport。"""

    model_name = (name or "rule").strip().lower()
    if model_name == "rule":
        return RuleSemanticModel()
    if transport is not None:
        return ModelCallableSemanticModel(transport, name=model_name)
    if model_name == "seed21_turbo":
        if auth_document is None:
            raise SemanticModelError("seed21_turbo 需要提供 auth_document")
        try:
            from tools.run_locked_director_text_live import DualSeed21TurboTextTransport
        except Exception as exc:  # noqa: BLE001
            raise SemanticModelError(f"无法加载 Seed 2.1 turbo transport：{exc}") from exc
        return ModelCallableSemanticModel(
            DualSeed21TurboTextTransport(Path(auth_document)),
            name=model_name,
        )
    raise SemanticModelError(f"未知语义模型：{model_name}")
