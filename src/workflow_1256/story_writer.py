"""编导层独立故事编写器：把文案收口为可验证的起承转合状态链。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class StoryWriterValidationError(ValueError):
    pass


class StoryWriterTransportRequired(RuntimeError):
    pass


StoryTransport = Callable[[str, str], Any]
STORY_REVIEW_PENDING = "PENDING_USER_REVIEW"
STORY_REVIEW_APPROVED = "APPROVED"
STORY_REVIEW_REJECTED = "REJECTED"
ACT_ORDER = ("起", "承", "转", "合")
WEAK_OPENING_TITLES = frozenset({
    "真正改变一切的是什么？",
    "真正改变结局的是什么？",
    "接下来会发生什么？",
    "真相还在后面？",
})
OPENING_TENSION_MARKERS = (
    "为什么", "谁", "代价", "失控", "掉队", "来不及", "真相", "危险",
    "不是", "而是", "当", "最后", "开始", "还能", "会先", "变了",
)
SILENT_FILM_FORBIDDEN_VISUAL_TOKENS = (
    "屏幕", "输入框", "对话", "文字", "字幕", "聊天", "弹窗", "通知", "UI", "图标",
    "Token", "积木", "便签", "催缴单", "电费单", "工牌", "可读", "封条", "标签",
    "分类签", "警示牌", "牌子", "目录", "名录", "书名", "打印", "电表",
)


@dataclass(frozen=True)
class DirectorStoryDraft:
    """编导层交给用户审核的连续故事草案，不是素材层可消费的锁定清单。"""

    project_id: str
    run_id: str
    plan_version: str
    source_text: str
    segments: list[str]
    timelines: list[dict[str, Any]]
    story: dict[str, Any]
    revision: int = 1
    review_status: str = STORY_REVIEW_PENDING

    def __post_init__(self) -> None:
        for name, value in (
            ("project_id", self.project_id),
            ("run_id", self.run_id),
            ("plan_version", self.plan_version),
            ("source_text", self.source_text),
        ):
            if not isinstance(value, str) or not value.strip():
                raise StoryWriterValidationError(f"{name} 必须是非空字符串")
        if not isinstance(self.segments, list) or not self.segments:
            raise StoryWriterValidationError("story_draft.segments 不能为空")
        if not isinstance(self.timelines, list) or len(self.segments) != len(self.timelines):
            raise StoryWriterValidationError("story_draft 的 segments 与 timelines 必须一一对应")
        if self.review_status not in {STORY_REVIEW_PENDING, STORY_REVIEW_APPROVED, STORY_REVIEW_REJECTED}:
            raise StoryWriterValidationError("story_draft.review_status 无效")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise StoryWriterValidationError("story_draft.revision 必须是正整数")
        if not isinstance(self.story, Mapping):
            raise StoryWriterValidationError("story_draft.story 必须是对象")

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "plan_version": self.plan_version,
            "source_text": self.source_text,
            "segments": list(self.segments),
            "timelines": [dict(item) for item in self.timelines],
            "story": dict(self.story),
            "revision": self.revision,
            "review_status": self.review_status,
            "handoff_state": "WAITING_FOR_USER_APPROVAL",
        }

SYSTEM_PROMPT = """你是短视频编导层的故事编写器。你的任务不是复述文案，也不是设计画面或写提示词；而是把原文已有事实组织成能被镜头演出的因果故事。

必须按 起、承、转、合 四幕输出。每幕都要说明：它承接的原文事实、上一状态、触发或变化、当前状态、下一幕张力。优先形成“状态→触发→反应/选择→结果或余波”，禁止把抽象概念、数据结论或逻辑术语直接当成画面。不得虚构原文没有的人物、事件、数据或结论。

只输出合法 JSON：
{
  "story_spine":"一句话因果主线",
  "emotional_arc":"一句话情绪变化",
  "acts":[
    {"phase":"起","source_anchor":"原文事实概括","state_before":"开始状态","trigger":"触发或承接动作","state_after":"本幕落点","next_tension":"下一幕问题"},
    {"phase":"承","source_anchor":"原文事实概括","state_before":"开始状态","trigger":"触发或承接动作","state_after":"本幕落点","next_tension":"下一幕问题"},
    {"phase":"转","source_anchor":"原文事实概括","state_before":"开始状态","trigger":"触发或承接动作","state_after":"本幕落点","next_tension":"下一幕问题"},
    {"phase":"合","source_anchor":"原文事实概括","state_before":"开始状态","trigger":"触发或承接动作","state_after":"本幕落点","next_tension":"余韵或行动"}
  ]
}"""

CINEMATIC_SYSTEM_PROMPT = """你是无对白短视频的电影故事编导。配音负责解释知识观点；你负责把这些观点映射为一条连续默片故事。
这不是“把知识点逐句配图”，更不是产品演示。先从原文抽取产业或社会变化的核心命题：旧系统如何运作、新能力改变了什么、哪种治理或资源分配选择被迫出现、选择会让谁受益或承担代价。再为这次文案创造适配的故事世界、主角与人物关系；禁止复用上一条文案的人名、职业、场景或冲突。
必须完成一篇可独立成立、可供用户审核的完整故事：主角必须有具体且可见的目标、持续阻力、失败代价、一个使其无法回头的事件、主动做出的关键选择、选择造成的后果与人物变化。所有事件必须形成因果链，而不能是“看到一个概念→理解概念”的说明链。
禁止独角戏。故事必须有 2—4 名具名的功能型配角；每人都要有自己的目标、与主角的关系、与其相冲突或结盟的立场，以及至少一次改变主角行动的无对白互动。至少一名配角代表具体的人际阻力或制度压力，至少一名配角让“失败的代价”落到真实的人身上。配角不得只是路人、背景板或递道具。
故事必须同时具备三层冲突：外部冲突（任务/资源/时间/环境）、关系冲突（主角与配角目标不一致或互相牵制）、内部冲突（主角必须在两种会付出代价的选择之间决定）。每一层必须在故事中升级，并在关键选择后产生新的后果。
完整故事必须是现代城市默片，不出现对话、旁白、可读文字、字幕、聊天记录、输入框、通知、弹窗、屏幕 UI、图标、数据面板、可识别的 Token 字样或用积木逐字拼句子的画面。需要映射抽象概念时，只能通过可演出的动作、物件、空间、光线、人物关系和可见后果来表达；不能让角色观看或操作一个解释概念的界面。
原文每个配音大分段都只能选取同一完整故事中的一段“电影预告片高光事件”，而非各自独立的小寓言。高光事件必须承接前一段的因果，并留下下一段的悬念；允许预告片式跳时，但必须让观众看出是同一个主角、同一目标和同一危机。高光内容必须服务原文观点：例如规模失控、拆解重组、智力的可计量生产、效率跃迁与能源代价，均通过本次故事主角的命运和行动隐喻，不直接解释概念。
为保证审核稿不会截断：silent_story_text 控制在 450—650 个汉字；每个 scene_groups 的文字字段不超过 36 个汉字；narration_mappings 中 source_claim、story_range 不超过 36 个汉字，silent_action 不超过 56 个汉字，其余字段每项不超过 28 个汉字。每项都必须是信息密度高的短句，不要复述原文，不要重复其他字段。

先写一份可独立成立的电影故事大纲：主角、目标、核心冲突、人物弧光、结尾悬念。再写一段连续 silent_story_text，只写场景、动作、物件、人物反应和空间变化，不得出现对白、聊天记录、输入框、通知推送或解释字幕。最后把每个给定 group_id 的配音段落一一映射到这条故事中的场景动作和隐喻关系；不得遗漏、重排或跨段吞并 group_id。故事可以虚构人物和场景作为隐喻，但不得改变原文的事实结论或表达相反观点。

另外输出一个用于视频开场文字模板的 opening_title：最多 12 个汉字（含标点），必须是有具体信息的悬念式标题。标题至少包含“具体对象/变化”与“风险、代价或反常结果”中的两项，并留下一个观众必须继续看才能回答的问题；不要只写“真正改变一切的是什么？”这类空泛问题。优先使用“效率越高，代价越大？ / 谁会先为效率买单？ / 看似解决了，代价谁来付？”这类结构，不直接公布正文答案，不使用“震惊”“必看”等夸张套话。这个标题属于剪辑文字轨，不属于默片画面，因此不要把它写进 silent_story_text。

场景组是连续电影段落，不是逐句配图：当配音段落不少于 3 段时，scene_groups 必须是 3 到 5 组；每组覆盖相邻的一个或多个 group_id，至少一个场景组必须覆盖多个 group_id。用场景内动作变化承接多个观点，不要让每个 group_id 都独占一个 scene_id。

只输出合法 JSON：
{
  "opening_title":"悬念式开场标题",
  "movie_outline":{"protagonist":"主角设定","protagonist_goal":"目标","central_conflict":"冲突","character_arc":"人物变化","ending_hook":"结尾悬念"},
  "source_semantic_anchor":{"core_thesis":"原文真正要表达的产业或社会命题","old_system":"被改变的旧生产或治理方式","new_capability":"新技术或新机制带来的能力","governance_choice":"组织必须做出的治理或资源分配选择","systemic_cost":"规模化后的真实代价"},
  "story_anchor":{"premise":"一句话故事前提","protagonist_want":"主角具体想得到什么","opposing_force":"持续阻力","stakes_if_fail":"失败会失去什么","point_of_no_return":"不可逆转事件","decisive_choice":"主角关键选择","transformation":"结尾人物如何改变","final_image_hook":"结尾可见悬念"},
  "supporting_characters":[{"name":"具名配角","role":"叙事功能","own_goal":"他或她想要什么","relationship_to_protagonist":"与主角的关系","conflict_or_alliance":"冲突或结盟立场","decisive_interaction":"无对白互动如何改变主角行动"}],
  "conflict_chain":{"external":"外部冲突及升级","relationship":"人物关系冲突及升级","internal":"主角两难与选择","escalation":"三层冲突如何汇入关键选择"},
  "silent_story_text":"连续无对白故事文本",
  "scene_groups":[{"scene_id":"scene_01","group_ids":["g01"],"scene_purpose":"场景任务","state_before":"开始状态","visible_conflict":"可见冲突","turn":"本场变化","state_after":"结束状态"}],
  "narration_mappings":[{"group_id":"g01","scene_id":"scene_01","source_claim":"本段原文观点","story_range":"对应完整故事中的起止事件","trailer_highlight":"从该范围挑出的预告片高光情节","semantic_mapping":"文案观点与故事关系","conflict":"本段正在发生的阻力","choice_or_action":"主角为应对阻力采取的行动","consequence":"行动导致的新状态","trailer_hook":"留给下一段的悬念钩子","silent_action":"无对白动作","metaphor":"隐喻关系","state_before":"承接状态","state_after":"本段落点","bridge_to_next":"下一段视觉承接"}]
}"""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoryWriterValidationError(f"{field} 必须是非空字符串")
    return value.strip()


def build_story_prompt(text: str) -> str:
    return "原文如下：\n" + _text(text, "text")


def _decode(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise StoryWriterValidationError("故事编写器响应必须是 JSON 对象或 JSON 字符串")
    cleaned = value.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise StoryWriterValidationError("故事编写器响应不是合法 JSON") from exc
    if not isinstance(result, Mapping):
        raise StoryWriterValidationError("故事编写器响应根节点必须是对象")
    return result


def normalize_story(value: Any) -> dict[str, Any]:
    data = _decode(value)
    acts = data.get("acts")
    if not isinstance(acts, list) or len(acts) != 4:
        raise StoryWriterValidationError("acts 必须恰好包含起、承、转、合四幕")
    result_acts: list[dict[str, str]] = []
    required = ("phase", "source_anchor", "state_before", "trigger", "state_after", "next_tension")
    for index, raw in enumerate(acts):
        if not isinstance(raw, Mapping):
            raise StoryWriterValidationError(f"acts[{index}] 必须是对象")
        item = {field: _text(raw.get(field), f"acts[{index}].{field}") for field in required}
        if item["phase"] != ACT_ORDER[index]:
            raise StoryWriterValidationError("acts 必须按起、承、转、合顺序输出")
        result_acts.append(item)
    return {
        "story_spine": _text(data.get("story_spine"), "story_spine"),
        "emotional_arc": _text(data.get("emotional_arc"), "emotional_arc"),
        "acts": result_acts,
    }


def run_story_writer(text: str, *, transport: StoryTransport | None = None) -> dict[str, Any]:
    """独立运行；未注入 transport 时不访问任何模型。"""

    user_prompt = build_story_prompt(text)
    if transport is None:
        raise StoryWriterTransportRequired("未注入故事编写器模型 transport；当前不会擅自调用外部模型")
    return normalize_story(transport(SYSTEM_PROMPT, user_prompt))


def _segments(value: Sequence[str]) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise StoryWriterValidationError("segments 必须是非空字符串数组")
    result = [_text(item, f"segments[{index}]") for index, item in enumerate(value)]
    if not result:
        raise StoryWriterValidationError("segments 不能为空")
    return result


def build_cinematic_story_prompt(
    text: str,
    segments: Sequence[str],
    timelines: Sequence[Mapping[str, int]] | None = None,
    review_feedback: str | None = None,
    protagonist_profile: Mapping[str, Any] | None = None,
) -> str:
    groups = _segments(segments)
    if timelines is not None and len(timelines) != len(groups):
        raise StoryWriterValidationError("timelines 必须与 segments 一一对应")
    payload = []
    for index, segment in enumerate(groups):
        item: dict[str, Any] = {"group_id": f"g{index + 1:02d}", "narration_text": segment}
        if timelines is not None:
            item["timeline"] = dict(timelines[index])
        payload.append(item)
    prompt = "原文：\n" + _text(text, "text") + "\n\n配音分段：\n" + json.dumps(payload, ensure_ascii=False)
    profile = protagonist_profile if isinstance(protagonist_profile, Mapping) else {}
    mode = str(profile.get("mode") or "auto").strip().lower()
    name = str(profile.get("name") or "").strip()
    gender = str(profile.get("gender") or "auto").strip().lower()
    age_group = str(profile.get("age_group") or "auto").strip().lower()
    identity = str(profile.get("identity") or "").strip()
    if mode not in {"auto", "manual"}:
        raise StoryWriterValidationError("protagonist_profile.mode 仅支持 auto 或 manual")
    if gender not in {"auto", "male", "female", "neutral"}:
        raise StoryWriterValidationError("protagonist_profile.gender 无效")
    if age_group not in {"auto", "child", "teen", "young_adult", "adult", "middle_aged", "elderly"}:
        raise StoryWriterValidationError("protagonist_profile.age_group 无效")
    if mode == "manual" and not any((name, identity, gender != "auto", age_group != "auto")):
        raise StoryWriterValidationError("手动主角设定至少需要姓名、身份、性别或年龄段之一")
    prompt += (
        "\n\n主角身份规则（优先级：用户手动设定 > 原文具名人物 > 自动创作）：\n"
        + json.dumps({
            "mode": mode,
            "name": name,
            "gender": gender,
            "age_group": age_group,
            "identity": identity,
        }, ensure_ascii=False)
        + "\n若原文的核心人物是具名真实人物（例如褚时健），主角必须使用该人物，"
          "不得替换成虚构的年轻女性或沿用历史默认角色。movie_outline.protagonist 必须明确写出"
          "主角姓名、性别、年龄阶段和身份；手动设定必须逐项服从。原文没有具名人物时，"
          "再根据文案语义自动生成适配的性别、年龄阶段和身份，禁止默认套用年轻女性主角。"
    )
    if review_feedback is not None and review_feedback.strip():
        prompt += "\n\n上一版审核意见（必须逐项修正，不能忽略）：\n" + review_feedback.strip()
    return prompt


def fallback_opening_title(value: Any) -> str:
    """为旧故事稿或无故事段落提供可追踪的悬念式开场标题。"""

    text = "".join(str(value or "").split()).strip("，。！？；：:;,!? ")
    if text:
        return f"{text[:4]}，谁来买单？"
    return "效率越高，代价越大？"


def _is_weak_opening_title(value: str) -> bool:
    title = "".join(value.split())
    return (
        title in WEAK_OPENING_TITLES
        or len(title) < 8
        or not any(marker in title for marker in OPENING_TENSION_MARKERS)
    )


def normalize_cinematic_story(value: Any, segments: Sequence[str], *, strict_trailer_contract: bool = False) -> dict[str, Any]:
    data = _decode(value)
    group_texts = _segments(segments)
    group_ids = [f"g{index + 1:02d}" for index in range(len(group_texts))]
    outline = data.get("movie_outline")
    if not isinstance(outline, Mapping):
        raise StoryWriterValidationError("movie_outline 必须是对象")
    movie_outline = {key: _text(outline.get(key), f"movie_outline.{key}") for key in (
        "protagonist", "protagonist_goal", "central_conflict", "character_arc", "ending_hook"
    )}
    opening_title = str(data.get("opening_title") or "").strip()
    if not opening_title or _is_weak_opening_title(opening_title):
        opening_title = fallback_opening_title(movie_outline["central_conflict"])
    opening_title = " ".join(opening_title.split())
    if len(opening_title) > 12:
        opening_title = fallback_opening_title(movie_outline["central_conflict"])
    opening_title = opening_title[:12]
    raw_semantic_anchor = data.get("source_semantic_anchor")
    if strict_trailer_contract and not isinstance(raw_semantic_anchor, Mapping):
        raise StoryWriterValidationError("source_semantic_anchor 必须是对象")
    source_semantic_anchor = {}
    if isinstance(raw_semantic_anchor, Mapping):
        source_semantic_anchor = {
            key: _text(raw_semantic_anchor.get(key), f"source_semantic_anchor.{key}")
            for key in ("core_thesis", "old_system", "new_capability", "governance_choice", "systemic_cost")
        }
    anchor = data.get("story_anchor")
    if strict_trailer_contract and not isinstance(anchor, Mapping):
        raise StoryWriterValidationError("story_anchor 必须是对象")
    story_anchor = {}
    if isinstance(anchor, Mapping):
        story_anchor = {key: _text(anchor.get(key), f"story_anchor.{key}") for key in (
            "premise", "protagonist_want", "opposing_force", "stakes_if_fail",
            "point_of_no_return", "decisive_choice", "transformation", "final_image_hook",
        )}
    raw_supporting = data.get("supporting_characters")
    if strict_trailer_contract and (not isinstance(raw_supporting, list) or not 2 <= len(raw_supporting) <= 4):
        raise StoryWriterValidationError("supporting_characters 必须包含 2 到 4 名功能型配角")
    supporting_characters: list[dict[str, str]] = []
    if isinstance(raw_supporting, list):
        for index, raw in enumerate(raw_supporting):
            if not isinstance(raw, Mapping):
                raise StoryWriterValidationError(f"supporting_characters[{index}] 必须是对象")
            supporting_characters.append({
                key: _text(raw.get(key), f"supporting_characters[{index}].{key}")
                for key in ("name", "role", "own_goal", "relationship_to_protagonist", "conflict_or_alliance", "decisive_interaction")
            })
    raw_conflict_chain = data.get("conflict_chain")
    if strict_trailer_contract and not isinstance(raw_conflict_chain, Mapping):
        raise StoryWriterValidationError("conflict_chain 必须是对象")
    conflict_chain = {}
    if isinstance(raw_conflict_chain, Mapping):
        conflict_chain = {
            key: _text(raw_conflict_chain.get(key), f"conflict_chain.{key}")
            for key in ("external", "relationship", "internal", "escalation")
        }
    scene_values = data.get("scene_groups")
    if not isinstance(scene_values, list) or not scene_values:
        raise StoryWriterValidationError("scene_groups 必须是非空数组")
    minimum_scene_count = min(3, len(group_ids))
    maximum_scene_count = min(5, len(group_ids))
    if not minimum_scene_count <= len(scene_values) <= maximum_scene_count:
        raise StoryWriterValidationError(
            f"scene_groups 必须包含 {minimum_scene_count} 到 {maximum_scene_count} 个连续场景组"
        )
    scene_ids: list[str] = []
    scene_groups: list[dict[str, Any]] = []
    flattened_group_ids: list[str] = []
    for index, raw in enumerate(scene_values):
        if not isinstance(raw, Mapping):
            raise StoryWriterValidationError(f"scene_groups[{index}] 必须是对象")
        scene_id = _text(raw.get("scene_id"), f"scene_groups[{index}].scene_id")
        ids = raw.get("group_ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise StoryWriterValidationError(f"scene_groups[{index}].group_ids 必须是字符串数组")
        scene_ids.append(scene_id)
        flattened_group_ids.extend(ids)
        scene_groups.append({
            "scene_id": scene_id, "group_ids": list(ids),
            **{key: _text(raw.get(key), f"scene_groups[{index}].{key}") for key in (
                "scene_purpose", "state_before", "visible_conflict", "turn", "state_after"
            )},
        })
    if len(set(scene_ids)) != len(scene_ids) or flattened_group_ids != group_ids:
        raise StoryWriterValidationError("scene_groups 必须按顺序且恰好覆盖全部 group_id")
    if len(group_ids) > 5 and not any(len(scene["group_ids"]) > 1 for scene in scene_groups):
        raise StoryWriterValidationError("配音段落超过 5 段时，至少一个 scene_groups 必须合并相邻 group_id")
    mapping_values = data.get("narration_mappings")
    if not isinstance(mapping_values, list) or len(mapping_values) != len(group_ids):
        raise StoryWriterValidationError("narration_mappings 必须与配音分段一一对应")
    mappings: list[dict[str, str]] = []
    fields = (
        "group_id", "scene_id", "source_claim", "story_range", "trailer_highlight",
        "semantic_mapping", "conflict", "choice_or_action", "consequence", "trailer_hook",
        "silent_action", "metaphor", "state_before", "state_after", "bridge_to_next",
    )
    for index, raw in enumerate(mapping_values):
        if not isinstance(raw, Mapping):
            raise StoryWriterValidationError(f"narration_mappings[{index}] 必须是对象")
        required_fields = fields if strict_trailer_contract else (
            "group_id", "scene_id", "semantic_mapping", "silent_action", "metaphor", "state_before", "state_after", "bridge_to_next",
        )
        mapping = {field: _text(raw.get(field), f"narration_mappings[{index}].{field}") for field in required_fields}
        if not strict_trailer_contract:
            mapping.update({field: "" for field in fields if field not in mapping})
        if mapping["group_id"] != group_ids[index] or mapping["scene_id"] not in scene_ids:
            raise StoryWriterValidationError("narration_mappings 必须保持 group_id 顺序并引用已有 scene_id")
        mappings.append(mapping)
    return {
        "opening_title": opening_title,
        "movie_outline": movie_outline,
        "source_semantic_anchor": source_semantic_anchor,
        "story_anchor": story_anchor,
        "supporting_characters": supporting_characters,
        "conflict_chain": conflict_chain,
        "silent_story_text": _text(data.get("silent_story_text"), "silent_story_text"),
        "scene_groups": scene_groups,
        "narration_mappings": mappings,
    }


def validate_cinematic_story_quality(
    story: Mapping[str, Any], *, protagonist_name: str = "", strict_silent_film: bool = False
) -> dict[str, Any]:
    """将用户已确认的故事审核底线变成运行时门槛，而不是只靠提示词。"""
    result = dict(story)
    if protagonist_name and protagonist_name not in str(result["movie_outline"]["protagonist"]):
        raise StoryWriterValidationError(f"电影故事主角必须是 {protagonist_name}")
    if strict_silent_film:
        visible_text = "\n".join(
            [str(result["silent_story_text"])]
            + [str(mapping["silent_action"]) for mapping in result["narration_mappings"]]
        )
        offenders = [token for token in SILENT_FILM_FORBIDDEN_VISUAL_TOKENS if token.lower() in visible_text.lower()]
        if offenders:
            raise StoryWriterValidationError(
                "默片故事含有禁止的可读内容或说明性界面：" + "、".join(offenders)
            )
        if len(result["supporting_characters"]) < 2:
            raise StoryWriterValidationError("默片故事至少需要两名功能型配角")
        missing = [
            character["name"] for character in result["supporting_characters"]
            if character["name"] not in str(result["silent_story_text"])
        ]
        if missing:
            raise StoryWriterValidationError("完整故事未呈现功能型配角互动：" + "、".join(missing))
    return result


def run_cinematic_story_writer(
    text: str,
    segments: Sequence[str],
    *,
    timelines: Sequence[Mapping[str, int]] | None = None,
    transport: StoryTransport | None = None,
    review_feedback: str | None = None,
    strict_trailer_contract: bool = True,
    protagonist_name: str = "",
    strict_silent_film: bool = False,
    protagonist_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    prompt = build_cinematic_story_prompt(
        text, segments, timelines, review_feedback, protagonist_profile=protagonist_profile
    )
    if transport is None:
        raise StoryWriterTransportRequired("未注入电影故事模型 transport；当前不会擅自调用外部模型")
    retry_prompt = (
        "\n\n上一版生成未通过自动质量门。必须完全重写并只输出 JSON："
        "不得出现可读文字、任何屏幕或说明性界面，也不得出现 Token、积木、便签、账单、工牌、"
        "封条、标签、目录、名录、书名、警示牌或依赖文字理解的物件；所有人物字段使用通用主角"
        "关系字段 relationship_to_protagonist。完整默片故事正文必须逐一写出 supporting_characters 中每位配角的名字，"
        "并让每人和主角发生一次可见、无对白的行动互动；不能只在 JSON 人物字段中列名。"
    )
    last_error: StoryWriterValidationError | None = None
    for attempt in range(3):
        try:
            retry_context = ""
            if attempt and last_error is not None:
                retry_context = f"\n上一版精确失败原因：{last_error}。必须逐项修正后再输出。"
            story = normalize_cinematic_story(
                transport(CINEMATIC_SYSTEM_PROMPT, prompt if attempt == 0 else prompt + retry_prompt + retry_context),
                segments,
                strict_trailer_contract=strict_trailer_contract,
            )
            return validate_cinematic_story_quality(
                story, protagonist_name=protagonist_name, strict_silent_film=strict_silent_film
            )
        except StoryWriterValidationError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


__all__ = [
    "ACT_ORDER",
    "CINEMATIC_SYSTEM_PROMPT",
    "DirectorStoryDraft",
    "STORY_REVIEW_APPROVED",
    "STORY_REVIEW_PENDING",
    "STORY_REVIEW_REJECTED",
    "SYSTEM_PROMPT",
    "StoryWriterTransportRequired",
    "StoryWriterValidationError",
    "build_story_prompt",
    "build_cinematic_story_prompt",
    "fallback_opening_title",
    "normalize_cinematic_story",
    "normalize_story",
    "run_cinematic_story_writer",
    "run_story_writer",
]
