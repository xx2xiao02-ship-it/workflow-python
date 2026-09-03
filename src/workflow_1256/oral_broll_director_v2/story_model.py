"""编导 v2 故事层：二级分段类型判定（故事/说明/混合）与故事大纲（只覆盖故事段）。

分段判定采用“规则初判 + 模型只处理模糊项 + 白名单校验 + 失败回退规则”的模式：
可确定的功能标签直接定案（解释观点/直接展示对象/呈现结果 → 说明，还原案例 → 故事），
表现过程/表现对比/强化情绪这类模糊项才交给模型，模型结果仍由代码白名单把关。

故事大纲只覆盖被判定为 story 的二级分段，scene_groups 必须按顺序连续覆盖这些段落、
无空洞、无重排；narration_mappings 与故事段一一对应。未注入模型 transport 时，
使用确定性 rule 骨架（不访问任何外部服务），保证离线链路可全量验证。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..story_writer import normalize_cinematic_story, run_cinematic_story_writer


SEGMENT_TYPE_CHOICES = frozenset({"story", "explanation", "mixed"})
STORY_VISUAL_TASKS = frozenset({"还原案例"})
EXPLANATION_VISUAL_TASKS = frozenset({"解释观点", "直接展示对象", "呈现结果"})
AMBIGUOUS_VISUAL_TASKS = frozenset({"表现过程", "表现对比", "强化情绪"})

METHOD_MARKERS = ("第一招", "第二招", "第三招", "步骤", "方法", "做法", "诀窍", "技巧", "流程", "演示")
NARRATIVE_MARKERS = ("人", "他", "她", "你", "创业者", "同事", "老板", "朋友", "案例", "一个人")
DATA_MARKERS = ("数字", "比例", "增长", "下降", "翻倍", "对比", "vs", "VS", "%", "倍")
EMOTION_SCENE_MARKERS = ("焦虑", "害怕", "恐慌", "担心", "残酷", "希望", "温度", "共情")

SYSTEM_PROMPT = (
    "你是口播短视频编导的故事/说明分流层。只负责判断给定的模糊二级语义分段更适合用故事演绎"
    "还是说明画面，不负责时间、媒介和收口字段。"
    "输出 JSON：segment_types 对象，key 为输入的 group_id；每个值只能取："
    "story（适合用连续故事场景演绎：案例还原、人物关系、有情节的过程）；"
    "explanation（适合用说明动效/数据图表/步骤流程呈现：纯步骤、数据对比、观点解释、对象展示、结果呈现）；"
    "mixed（一段内既有叙事又有说明，需按三级小段分别归属）。"
    "只能使用这三种取值，不要输出其他字段。"
)


class StoryModelError(ValueError):
    """故事层输入或输出不满足契约。"""


def rule_segment_type(text: str, visual_task: str) -> tuple[str, bool]:
    """规则初判：返回 (segment_type, needs_model)。needs_model 只对模糊任务为真。"""

    value = text.strip()
    if visual_task in STORY_VISUAL_TASKS:
        return "story", False
    if visual_task in EXPLANATION_VISUAL_TASKS:
        return "explanation", False
    if visual_task == "表现过程":
        if any(marker in value for marker in METHOD_MARKERS):
            return "explanation", False
        if any(marker in value for marker in NARRATIVE_MARKERS):
            return "story", False
        return "mixed", True
    if visual_task == "表现对比":
        if any(marker in value for marker in DATA_MARKERS):
            return "explanation", False
        if any(marker in value for marker in NARRATIVE_MARKERS):
            return "story", False
        return "mixed", True
    if visual_task == "强化情绪":
        if any(marker in value for marker in EMOTION_SCENE_MARKERS):
            return "story", False
        return "mixed", True
    return "mixed", True


def build_segment_type_prompt(groups: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps({"task": "segment_type_routing", "groups": list(groups)}, ensure_ascii=False, indent=2)


def validate_segment_type_plan(plan: Mapping[str, Any], group_ids: Sequence[str]) -> list[str]:
    errors: list[str] = []
    segment_types = plan.get("segment_types")
    if not isinstance(segment_types, Mapping):
        return ["segment_type_plan.segment_types 必须是对象"]
    for group_id in group_ids:
        value = segment_types.get(group_id)
        if value not in SEGMENT_TYPE_CHOICES:
            errors.append(f"{group_id}.segment_type 不在白名单（story/explanation/mixed）")
    return errors


class RuleSegmentTypeModel:
    """确定性分流模型：规则初判结果，作为无模型/兜底实现。"""

    name = "rule"

    def plan(self, groups: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        segment_types = {
            str(group["group_id"]): rule_segment_type(
                str(group["narration_text"]), str(group["visual_task"])
            )[0]
            for group in groups
        }
        return {"segment_types": segment_types}


class ModelCallableSegmentTypeModel:
    """把任意可调用模型包装成分流模型：callable(system_prompt, user_prompt) -> JSON 文本。"""

    def __init__(self, callable: Callable[[str, str], str], *, name: str = "custom_segment_type") -> None:
        self.callable = callable
        self.name = name

    def plan(self, groups: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        raw = self.callable(SYSTEM_PROMPT, build_segment_type_prompt(groups))
        if not isinstance(raw, str) or not raw.strip():
            raise StoryModelError("分流模型返回空内容")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StoryModelError(f"分流模型返回非 JSON：{exc}") from exc
        if not isinstance(data, Mapping):
            raise StoryModelError("分流模型返回内容必须是对象")
        return data


def build_segment_types(
    segments: Sequence[str],
    timelines: Sequence[Mapping[str, Any]],
    visual_tasks: Sequence[str],
    model: Any = None,
) -> dict[str, Any]:
    """规则先判全部段；只把模糊段交给模型，白名单校验失败重试后回退规则默认值。"""

    result: dict[str, Any] = {}
    ambiguous: list[str] = []
    for index, (segment, visual_task) in enumerate(zip(segments, visual_tasks, strict=True)):
        group_id = f"g{index + 1:02d}"
        segment_type, needs_model = rule_segment_type(segment, visual_task)
        result[group_id] = segment_type
        if needs_model:
            ambiguous.append(group_id)
    meta: dict[str, Any] = {
        "model": getattr(model, "name", "rule") if model is not None else "rule",
        "retried": False,
        "errors": [],
        "fallback": None,
    }
    if model is not None and ambiguous:
        index_by_group = {f"g{i + 1:02d}": i for i in range(len(segments))}
        groups = [
            {
                "group_id": group_id,
                "narration_text": segments[index_by_group[group_id]],
                "visual_task": visual_tasks[index_by_group[group_id]],
            }
            for group_id in ambiguous
        ]
        for _ in range(3):
            try:
                plan = model.plan(groups)
                errors = validate_segment_type_plan(plan, ambiguous)
                if not errors:
                    for group_id, segment_type in plan["segment_types"].items():
                        result[group_id] = segment_type
                    return {"segment_types": result, "ambiguous": ambiguous, "meta": meta}
                meta["errors"] = errors
            except Exception as exc:  # noqa: BLE001 - 模型失败统一进入重试/兜底
                meta["errors"] = [str(exc)]
            meta["retried"] = True
        meta["fallback"] = "rule"
    return {"segment_types": result, "ambiguous": ambiguous, "meta": meta}


def build_segment_type_model(
    name: str = "rule",
    *,
    transport: Callable[[str, str], str] | None = None,
    auth_document: str | Path | None = None,
) -> Any:
    """模型选择入口：rule 为规则引擎，seed21_turbo 为生产文本模型，也可注入任意 transport。"""

    model_name = (name or "rule").strip().lower()
    if model_name == "rule":
        return RuleSegmentTypeModel()
    if transport is not None:
        return ModelCallableSegmentTypeModel(transport, name=model_name)
    if model_name == "seed21_turbo":
        if auth_document is None:
            raise StoryModelError("seed21_turbo 需要提供 auth_document")
        try:
            from tools.run_locked_director_text_live import DualSeed21TurboTextTransport
        except Exception as exc:  # noqa: BLE001
            raise StoryModelError(f"无法加载 Seed 2.1 turbo transport：{exc}") from exc
        return ModelCallableSegmentTypeModel(
            DualSeed21TurboTextTransport(Path(auth_document)),
            name=model_name,
        )
    raise StoryModelError(f"未知分流模型：{model_name}")


def _story_group_ids(segment_types: Mapping[str, str]) -> list[str]:
    return [group_id for group_id, segment_type in segment_types.items() if segment_type == "story"]


def _chunk_indices(count: int, chunk_count: int) -> list[list[int]]:
    if count <= chunk_count:
        return [[index] for index in range(count)]
    chunks: list[list[int]] = []
    base = count // chunk_count
    remainder = count % chunk_count
    cursor = 0
    for chunk_index in range(chunk_count):
        size = base + (1 if chunk_index < remainder else 0)
        chunks.append(list(range(cursor, cursor + size)))
        cursor += size
    return chunks


def _rule_story_scaffold(
    story_group_ids: Sequence[str],
    indices: Sequence[int],
    segments: Sequence[str],
    visual_tasks: Sequence[str],
) -> dict[str, Any]:
    """确定性故事骨架：按故事段连续分组成 1-5 个场景，字段全部可离线生成。"""

    chunk_count = min(5, len(story_group_ids))
    chunks = _chunk_indices(len(story_group_ids), chunk_count)
    scene_groups: list[dict[str, Any]] = []
    group_to_scene: dict[str, str] = {}
    for scene_index, chunk in enumerate(chunks):
        scene_id = f"scene_{scene_index + 1:02d}"
        group_ids = [story_group_ids[offset] for offset in chunk]
        first_index = indices[chunk[0]]
        focus = _focus_text(segments[first_index])
        scene_groups.append(
            {
                "scene_id": scene_id,
                "group_ids": group_ids,
                "scene_purpose": f"用连续动作承接观点：{visual_tasks[first_index]}",
                "state_before": "承接上一场景的状态",
                "visible_conflict": "画面冲突仍在推进",
                "turn": f"围绕“{focus}”发生可见变化",
                "state_after": "落到本段结论所需状态",
            }
        )
        for group_id in group_ids:
            group_to_scene[group_id] = scene_id
    mappings: list[dict[str, Any]] = []
    for offset, group_id in enumerate(story_group_ids):
        index = indices[offset]
        focus = _focus_text(segments[index])
        mappings.append(
            {
                "group_id": group_id,
                "scene_id": group_to_scene[group_id],
                "source_claim": focus,
                "story_range": f"第{offset + 1}段故事事件",
                "trailer_highlight": "",
                "semantic_mapping": f"将“{focus}”映射为可见动作",
                "conflict": "",
                "choice_or_action": "",
                "consequence": "",
                "trailer_hook": "",
                "silent_action": f"以无对白动作演出“{focus}”",
                "metaphor": "事件隐喻与口播观点对应",
                "state_before": "承接上一镜头状态",
                "state_after": "本段落点",
                "bridge_to_next": "延续到下一镜头",
            }
        )
    return {
        "movie_outline": {
            "protagonist": "口播观点中的主体",
            "protagonist_goal": "完成口播所述目标",
            "central_conflict": "口播所述阻力或矛盾",
            "character_arc": "随观点推进发生变化",
            "ending_hook": "以呈现结果为悬念落点",
        },
        "opening_title": "看似解决了，代价谁来付？",
        "scene_groups": scene_groups,
        "narration_mappings": mappings,
    }


def _focus_text(value: str, limit: int = 36) -> str:
    text = "".join(value.split())
    return text if len(text) <= limit else text[:limit]


def _remap_story_group_ids(story: Mapping[str, Any], story_group_ids: Sequence[str]) -> dict[str, Any]:
    """v1 故事按传入段重新编号 g01..；这里映射回原始 group_id，scene_id 保持不变。"""

    local_to_original = {f"g{index + 1:02d}": group_id for index, group_id in enumerate(story_group_ids)}
    result = dict(story)
    scene_groups = []
    for scene in story.get("scene_groups", []):
        scene = dict(scene)
        scene["group_ids"] = [local_to_original[group_id] for group_id in scene["group_ids"]]
        scene_groups.append(scene)
    mappings = []
    for mapping in story.get("narration_mappings", []):
        mapping = dict(mapping)
        mapping["group_id"] = local_to_original[mapping["group_id"]]
        mappings.append(mapping)
    result["scene_groups"] = scene_groups
    result["narration_mappings"] = mappings
    return result


def validate_story_coverage(story: Mapping[str, Any], story_group_ids: Sequence[str]) -> list[str]:
    errors: list[str] = []
    flattened = [
        group_id
        for scene in story.get("scene_groups", [])
        for group_id in scene.get("group_ids", [])
    ]
    if flattened != list(story_group_ids):
        errors.append("scene_groups 必须按顺序连续覆盖全部故事段且无空洞")
    mapping_ids = [mapping.get("group_id") for mapping in story.get("narration_mappings", [])]
    if mapping_ids != list(story_group_ids):
        errors.append("narration_mappings 必须与故事段一一对应且保持顺序")
    return errors


def build_story_draft(
    text: str,
    segments: Sequence[str],
    timelines: Sequence[Mapping[str, Any]],
    segment_types: Mapping[str, str],
    visual_tasks: Sequence[str],
    *,
    transport: Callable[[str, str], Any] | None = None,
) -> dict[str, Any] | None:
    """产出只覆盖故事段的故事大纲；无 transport 时使用规则骨架，不访问外部模型。"""

    story_group_ids = _story_group_ids(segment_types)
    if not story_group_ids:
        return None
    indices = [int(group_id[1:]) - 1 for group_id in story_group_ids]
    story_segments = [str(segments[index]) for index in indices]
    story_timelines = [dict(timelines[index]) for index in indices]
    if transport is not None:
        story = run_cinematic_story_writer(
            "".join(story_segments),
            story_segments,
            timelines=story_timelines,
            transport=transport,
            strict_trailer_contract=False,
            strict_silent_film=False,
        )
        story = _remap_story_group_ids(story, story_group_ids)
        origin = "cinematic_model"
    else:
        story = _rule_story_scaffold(story_group_ids, indices, segments, visual_tasks)
        origin = "rule_scaffold"
    errors = validate_story_coverage(story, story_group_ids)
    if errors:
        raise StoryModelError("故事层校验失败：" + "；".join(errors))
    return {
        "story_group_ids": story_group_ids,
        "origin": origin,
        "story": story,
    }


def resolve_story_context(group_id: str, story_draft: Mapping[str, Any] | None) -> dict[str, Any]:
    """把故事映射落到镜头：scene_id / story_range / silent_action / metaphor 等。"""

    if story_draft is None:
        return {}
    mappings = story_draft.get("story", {}).get("narration_mappings", [])
    mapping = next((item for item in mappings if item.get("group_id") == group_id), None)
    if not mapping:
        return {}
    return {
        "scene_id": str(mapping.get("scene_id") or ""),
        "story_range": str(mapping.get("story_range") or ""),
        "silent_action": str(mapping.get("silent_action") or ""),
        "metaphor": str(mapping.get("metaphor") or ""),
        "trailer_highlight": str(mapping.get("trailer_highlight") or ""),
    }


__all__ = [
    "AMBIGUOUS_VISUAL_TASKS",
    "EXPLANATION_VISUAL_TASKS",
    "SEGMENT_TYPE_CHOICES",
    "STORY_VISUAL_TASKS",
    "StoryModelError",
    "build_segment_type_model",
    "build_segment_types",
    "build_story_draft",
    "resolve_story_context",
    "rule_segment_type",
    "validate_segment_type_plan",
    "validate_story_coverage",
]
