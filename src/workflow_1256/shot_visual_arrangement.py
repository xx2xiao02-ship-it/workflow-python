"""116616“全局视觉序列编排器”的无外部调用 Python 实现。

行为依据：audit/source/116616_coze_original_sanitized.py。
模型调用不在本模块内发生，必须通过 gpt_transport 和 mini_transport 显式注入。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .character_asset import inject_character_asset_refs
from .canvas import canvas_prompt_label, resolve_canvas
from .governance.media_route_scoring_v3 import (
    MediaRouteScoringV3Error,
    validate_classification_features,
)


class ShotVisualArrangementValidationError(ValueError):
    """输入或固定模型响应不符合原始节点契约。"""


class ShotVisualArrangementTransportRequired(RuntimeError):
    """未注入模型传输层，避免误调用外部服务。"""


class GptTransport(Protocol):
    def __call__(self, payload: Mapping[str, Any]) -> Any: ...


class MiniTransport(Protocol):
    def __call__(self, payload: Mapping[str, Any]) -> Any: ...


# 这是模型 transport 的兼容标签，不是独立的鉴权配置。
# 生产调用由上层 DualSeed21TurboTextTransport 从用户鉴权文档读取
# Seed 2.1 turbo 的主/备用端点；这里不能硬编码具体供应商或鉴权。
GLOBAL_PLANNER_MODEL = "seed-2.1-turbo"
FEATURE_EVALUATOR_MODEL = "seed-2.1-turbo"
MINI_MODEL = "ep-20260609123759-6sv2j"
GPT_THINKING = {"type": "disabled"}
GPT_TIMEOUT = 100
MINI_TIMEOUT = 60
MINI_PARALLEL_WORKERS = 4
RETRIES = 2
RETRY_DELAY_SECONDS = 0.6
MAX_GPT_GROUP_CONTEXT_CHARS = 96
MAX_GPT_SHOT_TEXT_CHARS = 72
FEATURE_BATCH_SIZE = 6
FEATURE_PARALLEL_WORKERS = 4
FEATURE_SOURCE_TEXT_CHARS = 180
FEATURE_STORY_CONTEXT_CHARS = 360
FEATURE_SEGMENT_CONTEXT_CHARS = 480

SHOT_SIZES = ["超远景", "远景", "全景", "中景", "近景", "特写", "极特写"]
# 编导层和模型有时会使用更口语化的景别名称；统一归一到现有下游契约，
# 不新增第二套镜头字段。
SHOT_SIZE_ALIASES = {
    "超大全景": "超远景",
    "大远景": "超远景",
}
WIDE_SHOT_SIZES = {"超远景", "远景", "全景"}
MID_SHOT_SIZES = {"中景"}
CLOSE_SHOT_SIZES = {"近景", "特写", "极特写"}
CARRIER_MODES = ["完整人物", "人物局部", "物件", "空间", "建筑环境"]
# “意识流”是视觉设计类型，不是物理承载方式。为兼容模型把设计类型
# 误写到 carrier_mode 的结果，按空间承载归一；具体意识流语义仍由
# story_context.shot_design_type 保留并继续进入提示词。
CARRIER_MODE_ALIASES = {
    "意识流": "空间",
    "意识流镜头": "空间",
}
COMPOSITIONS = [
    "纵向主体—物件关系",
    "边缘压力切入",
    "斜向纵深",
    "遮挡或框景",
    "多人层级",
    "局部焦点与留白",
]
REF_STYLE_GUIDE = "参考图仅承接画风、色彩、光影、线条、材质与人物气质。"
DEFAULT_CHARACTER_ANCHOR = "同一位年轻职场人"

GPT_SYSTEM_PROMPT = "\n# 全局镜头编排\n\n看完整序列后，统一为每条镜头确定：场景、景别、承载、视角、构图、视觉重点。不要逐条孤立决定。\n\n故事连续性：每条输入镜头都给出事件、叙事作用和已经成立的状态。先判断它承接了什么，再决定该让观众看见什么。把抽象判断、机制解释或观点，转换为输入事实所允许的可观察因果瞬间：已有状态→触发/动作→人物或环境反应→结果/余波。不得逐句把逻辑词做成示意画面；不得用泛化的办公室人物、屏幕或图表替代因果推进；不得虚构新事件。若需要表达压力、效率、选择、变化，优先用人物与物件/空间关系发生的可见变化来承载。\n\n场景：工作不等于工位。会议/协作可用办公区、会议室、看板、走廊、电梯厅；系统压力可用楼宇、通道、空间环境；下班/回家/吃饭/深夜优先通勤和私人生活空间。\n景别：超远景=城市/楼宇/系统压力；远景=环境距离/通勤；全景=人物与空间；中景=人物与任务关系；近景=停顿/疲惫；特写=提醒/文件/手部；极特写=即时压力点。\n三条及以上：至少两种景别，至少一条非完整人物承载；不得连续三条同景别；不得全部完整人物。五条及以上的完整序列：必须同时覆盖远/全类（超远景、远景、全景）、中景、近/特类（近景、特写、极特写）；按叙事需要形成建立空间→人物关系→局部情绪或信息重点的景别节奏，不得机械循环景别。\n同场景连续镜头：景别、承载、视角、构图至少变化一项。\n\n构图必须落实：\n多人层级=至少两人且前后景职责明确；\n边缘压力切入=压力从边缘压入；\n斜向纵深=通道、桌面、看板或空间延伸；\n遮挡或框景=门框、肩背、物件边缘等遮挡；\n纵向主体—物件关系=人物与关键物件有前后或上下关系；\n局部焦点与留白=局部动作、物件或表情成为中心。\n\n镜头语法：\n完整人物多用全景/中景；\n人物局部多用近景/特写；\n物件多用近景/特写；\n空间或建筑多用远景/全景；\n提醒、打断、侵入、追加、堆积优先边缘压力切入、遮挡或局部焦点；\n楼宇、走廊、电梯、地铁、通道优先斜向纵深。\n\n输入使用压缩字段：\nq = 镜头组数组；\n每组：g=组号，c=该段上下文，s=[[镜头id,镜头事件,叙事作用,已成立状态]...]。\n\n只输出紧凑 JSON，禁止解释：\n{\n  \"p\": [\n    [\"镜头id\",\"时间+具体场景\",\"景别\",\"承载方式\",\"视角短语\",\"构图\",\"视觉重点短句\"]\n  ]\n}\n\n景别只能是：超远景、远景、全景、中景、近景、特写、极特写。\n承载只能是：完整人物、人物局部、物件、空间、建筑环境。\n构图只能是：纵向主体—物件关系、边缘压力切入、斜向纵深、遮挡或框景、多人层级、局部焦点与留白。\n不得改写、删除、合并、重排镜头；不得输出 prompt、光影、时长、时间线。\n"
GPT_SYSTEM_PROMPT += (
    "\n空镜、意象和意识流设计必须服从编导层的 shot_context.shot_design_type："
    "空间空镜只能用空间/建筑环境承载；模型若把意识流写入承载字段，按空间承载解释；"
    "物件空镜只能用物件承载，即使事件文字提到人物，也不得被人物语义带偏；"
    "意象镜头和意识流镜头只能用既有物件、空间、光影或非文字感知承载；"
    "它们必须保留原故事的空间、道具、光线或内在状态承接，不得随机删除主角或生成无关风景。"
    "语义转折处若 transition_role=承上启下，画面必须明确完成前状态到后状态的可见桥接。"
    "如果设计类型是人物行动或人物反应，必须保留可见人物/物件动作或反应，不能因固定机位而改成空镜。"
    "时长小于3秒的镜头由上游路由硬判为静态图片并跳过AIGC；本节点不改写时间线。\n"
)
GPT_SYSTEM_PROMPT += (
    "\n运行时 user JSON 的 contract.expected_shot_count 和 contract.expected_shot_ids 是硬约束："
    "p 必须逐项、按相同顺序完整覆盖这些 shot_id；不得输出 classification_features 或其它长篇解释。\n"
)
FEATURE_EVALUATOR_SYSTEM_PROMPT = (
    "\n# V3.1 逐镜语义特征评估\n"
    "只评估 user JSON 中当前批次的镜头，不生成视觉规划，不计算权重，不决定 shot_class。\n"
    "contract.expected_shot_count 和 contract.expected_shot_ids 是硬约束："
    "classification_features 必须逐项覆盖全部 ID，数量和顺序完全一致。\n"
    "每条镜头必须输出以下 8 个 0-100 整数："
    "motion_need、temporal_dependency、static_completeness、visual_evidence、"
    "relation_strength、information_density、presenter_value、production_feasibility。\n"
    "evidence 必须是包含这 8 个键的中文非空短证据，每条尽量不超过 30 个汉字。"
    "禁止输出 candidate_scores、shot_class、prompt 或解释性长文本。\n"
    "只输出 JSON："
    "{\"classification_features\":[{\"shot_id\":\"与输入一致\","
    "\"motion_need\":0,\"temporal_dependency\":0,\"static_completeness\":0,"
    "\"visual_evidence\":0,\"relation_strength\":0,\"information_density\":0,"
    "\"presenter_value\":0,\"production_feasibility\":0,\"evidence\":{"
    "\"motion_need\":\"中文短证据\",\"temporal_dependency\":\"中文短证据\","
    "\"static_completeness\":\"中文短证据\",\"visual_evidence\":\"中文短证据\","
    "\"relation_strength\":\"中文短证据\",\"information_density\":\"中文短证据\","
    "\"presenter_value\":\"中文短证据\",\"production_feasibility\":\"中文短证据\"}}]}"
)
MINI_SYSTEM_PROMPT = "\n# 首帧与动态种子资料补全器\n\n当前输入是一小组已锁定镜头。只为每条补五项：\nsubject_action：首帧可见动作/姿势/物件状态；\nspace_layers：前中后景，或局部前后关系；\nkey_objects：1~3个具体可见对象；\nlighting_mood：具体光源、明暗关系和情绪；\nmotion_seed：从首帧自然向后发展的单句动态种子。\n\n规则：\n- subject_action 只写可直接画出的事实：松开键盘、手停在半空、手机亮起、任务卡覆盖排期等。\n- 禁止“呈现压力、处于停顿状态、提醒物件、关键物件”等抽象/占位词。\n- 提醒/消息/待办用手机、电脑屏幕、任务卡片、文件、餐具旁亮起的设备等自然载体。\n- key_objects 只写真实对象，不写“完整人物、压力来源、工作状态”。\n- 不写品牌、可读文字、复杂 UI、字幕；不改写事件；不重复景别、视角、构图、承载方式。\n- space_layers 必须服务给定构图。lighting_mood 写清环境光与局部亮点关系。\n- motion_seed 只描述首帧后的轻微、连续、可执行动态：状态延续 → 轻微变化 → 停住或落点。\n- motion_seed 不写镜头切换、推拉摇移、转场、换场、换人、增加关键物件、剧情跳跃或新的结果。\n- 每个字段控制在 12~42 个汉字内。\n\n只输出 JSON：\n{\n  \"materials\": [\n    {\n      \"shot_id\": \"与输入一致\",\n      \"subject_action\": \"中文短句\",\n      \"space_layers\": \"中文短句\",\n      \"key_objects\": \"中文短句\",\n      \"lighting_mood\": \"中文短句\",\n      \"motion_seed\": \"中文单句\"\n    }\n  ]\n}\n"

MINI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "materials": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "shot_id": {"type": "string"},
                    "subject_action": {"type": "string"},
                    "space_layers": {"type": "string"},
                    "key_objects": {"type": "string"},
                    "lighting_mood": {"type": "string"},
                    "motion_seed": {"type": "string"},
                },
                "required": [
                    "shot_id",
                    "subject_action",
                    "space_layers",
                    "key_objects",
                    "lighting_mood",
                    "motion_seed",
                ],
            },
        }
    },
    "required": ["materials"],
}


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def compact_gpt_text(value: Any, limit: int) -> str:
    value = re.sub(r"\s+", "", text(value))
    return value if len(value) <= limit else value[:limit] + "…"


def plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    for method in ("model_dump", "dict"):
        if hasattr(value, method):
            try:
                return plain(getattr(value, method)())
            except Exception:
                pass
    if hasattr(value, "__dict__"):
        try:
            return plain(vars(value))
        except Exception:
            pass
    return value


def decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value.strip())
        except Exception:
            return value
    return value


def obj(value: Any) -> dict[str, Any]:
    value = decode(plain(value))
    return value if isinstance(value, dict) else {}


def arr(value: Any) -> list[Any]:
    value = decode(plain(value))
    return value if isinstance(value, list) else []


def field(data: Any, key: str, default: Any = None) -> Any:
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def integer(value: Any) -> int | None:
    try:
        number = int(float(value))
        return number if number > 0 else None
    except Exception:
        return None


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return text(value).lower() in ("1", "true", "yes", "on", "是")


def timeline(value: Any) -> dict[str, int] | None:
    value = obj(value)
    try:
        start = int(float(value.get("start")))
        end = int(float(value.get("end")))
        return {"start": start, "end": end} if end > start else None
    except Exception:
        return None


def refs(value: Any) -> list[str]:
    values = arr(value)
    if not values and value:
        values = [value]

    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        item = decode(item)
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            url = text(
                item.get("url")
                or item.get("link")
                or item.get("image_url")
                or item.get("ref_image")
                or item.get("ref")
            )
        else:
            url = ""
        if url and url not in seen:
            result.append(url)
            seen.add(url)
    return result


def json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    raw = text(value)
    if not raw:
        return {}
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except Exception:
            pass
    return {}


def fail(message: Any, debug_info: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "prompt": [],
        "ref_image": [],
        "motion_seed": [],
        "timelines": [],
        "int_duration": [],
        "error": text(message),
        "debug": debug_info or {},
    }


def _resolve_transport_response(value: Any) -> Any:
    if isinstance(value, Mapping) and "data" in value and value.get("ok") is not None:
        return value.get("data")
    return value


def chat_text(data: Any) -> str:
    choices = arr(obj(data).get("choices"))
    if not choices:
        return ""
    message = obj(obj(choices[0]).get("message"))
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            text(obj(item).get("text") or obj(item).get("content"))
            for item in content
            if text(obj(item).get("text") or obj(item).get("content"))
        )
    return ""


def response_text(data: Any) -> str:
    data = obj(data)
    if text(data.get("output_text")):
        return text(data.get("output_text"))
    for output in arr(data.get("output")):
        for content in arr(obj(output).get("content")):
            content = obj(content)
            value = text(content.get("text") or content.get("content"))
            if value:
                return value
    return chat_text(data)


def _story_context_by_group(value: Any) -> dict[str, dict[str, str]]:
    """把编导故事的场景和分段映射压成每个大分段一个语义锚点。"""

    story = obj(value)
    mappings = {
        text(item.get("group_id")): dict(item)
        for item in arr(story.get("narration_mappings"))
        if text(obj(item).get("group_id"))
    }
    scenes_by_group: dict[str, dict[str, Any]] = {}
    for raw_scene in arr(story.get("scene_groups")):
        scene = obj(raw_scene)
        for group_id in arr(scene.get("group_ids")):
            if text(group_id):
                scenes_by_group[text(group_id)] = scene
    result: dict[str, dict[str, str]] = {}
    for group_id, mapping in mappings.items():
        scene = scenes_by_group.get(group_id, {})
        result[group_id] = {
            "scene_id": text(mapping.get("scene_id") or scene.get("scene_id")),
            "scene_purpose": text(scene.get("scene_purpose")),
            "visible_conflict": text(scene.get("visible_conflict")),
            "turn": text(scene.get("turn")),
            "state_before": text(mapping.get("state_before") or scene.get("state_before")),
            "state_after": text(mapping.get("state_after") or scene.get("state_after")),
            "semantic_mapping": text(mapping.get("semantic_mapping")),
            "silent_action": text(mapping.get("silent_action")),
            "metaphor": text(mapping.get("metaphor")),
            "bridge_to_next": text(mapping.get("bridge_to_next")),
        }
    return result


def _story_context_by_shot(value: Any) -> dict[str, dict[str, Any]]:
    """读取冻结编导后的逐小镜头治理上下文。"""

    story = obj(value)
    governance = obj(story.get("director_governance"))
    result: dict[str, dict[str, Any]] = {}
    for raw in arr(governance.get("shot_contexts")):
        context = obj(raw)
        shot_id = text(context.get("shot_id"))
        if shot_id:
            result[shot_id] = dict(context)
    return result


def build_sequence(raw: Any) -> tuple[dict[str, Any] | None, str]:
    segments = arr(field(raw, "segments", []))
    code_list = arr(field(raw, "Code_list", []))
    story_by_group = _story_context_by_group(field(raw, "story_context", {}))
    story_by_shot = _story_context_by_shot(field(raw, "story_context", {}))
    if not segments or not code_list:
        return None, "segments 与 Code_list 均不能为空。"
    if len(segments) != len(code_list):
        return None, "segments 与 Code_list 长度不一致。"

    gpt_groups: list[dict[str, Any]] = []
    flat: list[dict[str, Any]] = []
    groups: list[list[dict[str, Any]]] = []

    for group_index, segment in enumerate(segments):
        segment = text(segment)
        code_group = obj(code_list[group_index])
        shots = arr(code_group.get("shots"))
        durations = arr(code_group.get("int_duration"))
        timelines = arr(code_group.get("timelines"))

        if not segment or not shots:
            return None, f"第 {group_index + 1} 组 segments 或 shots 为空。"
        if len(shots) != len(durations) or len(shots) != len(timelines):
            return None, f"第 {group_index + 1} 组 shots、int_duration、timelines 长度不一致。"

        gpt_shots: list[list[str]] = []
        group_records: list[dict[str, Any]] = []
        story_context = story_by_group.get(f"g{group_index + 1:02d}", {})

        for shot_index, raw_shot in enumerate(shots):
            shot = obj(raw_shot)
            source_text = text(shot.get("source_text"))
            clip_role = text(shot.get("clip_role"))
            story_beat = text(shot.get("story_beat"))
            shot_timeline = timeline(timelines[shot_index])
            int_duration = integer(durations[shot_index])

            if not source_text or not clip_role or not story_beat:
                return None, f"第 {group_index + 1} 组第 {shot_index + 1} 条镜头缺少语义字段。"
            if not shot_timeline or not int_duration:
                return None, f"第 {group_index + 1} 组第 {shot_index + 1} 条镜头的时长或时间线非法。"

            shot_id = f"g{group_index + 1:02d}_s{shot_index + 1:02d}"
            shot_context = dict(story_context)
            shot_context.update(story_by_shot.get(shot_id, {}))
            gpt_shots.append([
                shot_id,
                compact_gpt_text(source_text, MAX_GPT_SHOT_TEXT_CHARS),
                compact_gpt_text(clip_role, 24),
                compact_gpt_text(story_beat, MAX_GPT_SHOT_TEXT_CHARS),
                *([compact_gpt_text(json.dumps(shot_context, ensure_ascii=False), MAX_GPT_SHOT_TEXT_CHARS)] if shot_context else []),
            ])
            record = {
                "shot_id": shot_id,
                "source_text": source_text,
                "clip_role": clip_role,
                "story_beat": story_beat,
                "segment_context": segment,
                "int_duration": int_duration,
                "timeline": shot_timeline,
                "story_context": shot_context,
            }
            group_records.append(record)
            flat.append(record)

        group_payload = {
            "g": f"g{group_index + 1:02d}",
            "c": compact_gpt_text(segment, MAX_GPT_GROUP_CONTEXT_CHARS),
            "s": gpt_shots,
        }
        if story_context:
            group_payload["story_context"] = story_context
        gpt_groups.append(group_payload)
        groups.append(group_records)

    return {"gpt_groups": gpt_groups, "flat": flat, "groups": groups}, ""


def _transport_to_text(value: Any) -> str:
    value = _resolve_transport_response(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping) and ("p" in value or "materials" in value or "classification_features" in value):
        return json.dumps(value, ensure_ascii=False)
    return chat_text(value) or response_text(value)


def gpt_plan(groups: list[dict[str, Any]], transport: GptTransport | None) -> tuple[Any, str, dict[str, Any]]:
    if transport is None:
        raise ShotVisualArrangementTransportRequired("未配置全局规划模型传输层；当前只能完成契约测试。")
    expected_shot_ids = [
        str(shot[0])
        for group in groups
        for shot in arr(obj(group).get("s"))
        if isinstance(shot, list) and shot
    ]
    user_content = json.dumps(
        {
            "contract": {
                "expected_shot_count": len(expected_shot_ids),
                "expected_shot_ids": expected_shot_ids,
            },
            "q": groups,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload = {
        "model": GLOBAL_PLANNER_MODEL,
        "thinking": GPT_THINKING,
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": GPT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    output_content = _transport_to_text(transport(payload))
    stats = {
        "gpt_input_chars": len(user_content),
        "gpt_system_chars": len(GPT_SYSTEM_PROMPT),
        "gpt_output_chars": len(output_content),
        "gpt_group_count": len(groups),
        "gpt_shot_count": sum(len(item.get("s", [])) for item in groups),
    }
    data = json_value(output_content)
    if not isinstance(data, dict):
        return None, "全局视觉规划模型未返回 JSON。", stats
    return data, "", stats


def _feature_batch_payload(
    records: list[dict[str, Any]],
    plans: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a bounded, ID-addressable request for one feature batch."""

    expected_shot_ids = [str(record["shot_id"]) for record in records]
    shots: list[dict[str, Any]] = []
    plans = plans or {}
    context_keys = (
        "shot_design_type", "narrative_job", "silent_action", "visible_conflict",
        "state_before", "state_after", "turn", "bridge_to_next", "transition_role",
        "must_show", "metaphor",
    )
    for record in records:
        shot_id = str(record["shot_id"])
        item: dict[str, Any] = {
            "shot_id": shot_id,
            "source_text": compact_gpt_text(record.get("source_text"), FEATURE_SOURCE_TEXT_CHARS),
            "clip_role": compact_gpt_text(record.get("clip_role"), 48),
            "story_beat": compact_gpt_text(record.get("story_beat"), FEATURE_SOURCE_TEXT_CHARS),
        }
        segment_context = compact_gpt_text(record.get("segment_context"), FEATURE_SEGMENT_CONTEXT_CHARS)
        if segment_context:
            item["segment_context"] = segment_context
        story_context = obj(record.get("story_context"))
        if story_context:
            compact_context: dict[str, Any] = {}
            for key in context_keys:
                value = story_context.get(key)
                if isinstance(value, list):
                    value = "、".join(str(part) for part in value[:6])
                value = compact_gpt_text(value, FEATURE_STORY_CONTEXT_CHARS)
                if value:
                    compact_context[key] = value
            if compact_context:
                item["story_context"] = compact_context
        plan = obj(plans.get(shot_id))
        if plan:
            item["visual_plan"] = {
                key: compact_gpt_text(plan.get(key), 96)
                for key in (
                    "scene_context", "shot_size", "carrier_mode", "viewpoint",
                    "composition", "visual_focus",
                )
                if compact_gpt_text(plan.get(key), 96)
            }
        shots.append(item)
    return {
        "contract": {
            "expected_shot_count": len(expected_shot_ids),
            "expected_shot_ids": expected_shot_ids,
        },
        "shots": shots,
    }


def _feature_batch(
    records: list[dict[str, Any]],
    plans: Mapping[str, dict[str, Any]] | None,
    transport: GptTransport | None,
) -> tuple[list[dict[str, Any]], int]:
    if transport is None:
        raise ShotVisualArrangementTransportRequired("未配置 V3.1 分段特征模型传输层；当前只能完成契约测试。")
    user_content = json.dumps(
        _feature_batch_payload(records, plans), ensure_ascii=False, separators=(",", ":")
    )
    payload = {
        "model": FEATURE_EVALUATOR_MODEL,
        "thinking": GPT_THINKING,
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": FEATURE_EVALUATOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    output_content = _transport_to_text(transport(payload))
    data = json_value(output_content)
    if not isinstance(data, dict):
        raise MediaRouteScoringV3Error("FEATURE_CONTRACT_INVALID: 分段特征模型未返回 JSON")
    try:
        normalized = validate_classification_features(
            data, [str(record["shot_id"]) for record in records]
        )
    except MediaRouteScoringV3Error:
        raise
    return normalized, len(output_content)


def _resolve_parallel_workers(value: int | None, *, default: int, env_name: str) -> int:
    """Resolve a bounded worker count without allowing malformed env config to stop a task."""

    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ShotVisualArrangementValidationError("并行 worker 数必须是正整数")
        return value
    raw = os.environ.get(env_name, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = default
        if parsed > 0:
            return parsed
    return default


def run_classification_feature_batches(
    params: Any = None,
    *,
    transport: GptTransport | None = None,
    plans: Mapping[str, dict[str, Any]] | None = None,
    batch_size: int = FEATURE_BATCH_SIZE,
    parallel_workers: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Evaluate V3.1 features in ordered segment batches, never in the global plan."""

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ShotVisualArrangementValidationError("V3.1 特征批大小必须是正整数")
    raw = plain(params)
    if isinstance(raw, dict) and isinstance(raw.get("_input"), dict):
        raw = raw["_input"]
    sequence, build_error = build_sequence(raw)
    if build_error:
        raise ShotVisualArrangementValidationError(build_error)
    if transport is None:
        raise ShotVisualArrangementTransportRequired("未配置 V3.1 分段特征模型传输层；当前只能完成契约测试。")

    batches: list[list[dict[str, Any]]] = []
    for group_records in sequence["groups"]:
        for start in range(0, len(group_records), batch_size):
            batches.append(group_records[start : start + batch_size])

    started_at = time.perf_counter()
    rows: list[dict[str, Any]] = []
    output_chars = 0
    if batches:
        worker_count = min(
            _resolve_parallel_workers(
                parallel_workers,
                default=FEATURE_PARALLEL_WORKERS,
                env_name="DIRECTOR_FEATURE_PARALLEL_WORKERS",
            ),
            len(batches),
        )
        batch_results: dict[int, tuple[list[dict[str, Any]], int]] = {}
        batch_errors: list[tuple[int, Exception]] = []
        start_events = [threading.Event() for _ in batches]

        def evaluate_batch(local_index: int, records: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]], int]:
            # 保证请求进入 transport 的顺序可审计；上一请求仍在等待网络时，
            # 后续 worker 可以继续执行，不把受控并行退化为串行。
            if local_index:
                start_events[local_index - 1].wait()
            start_events[local_index].set()
            batch_rows, batch_output_chars = _feature_batch(records, plans, transport)
            return local_index, batch_rows, batch_output_chars

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="v31-feature") as executor:
            futures = {
                executor.submit(evaluate_batch, local_index, records): local_index + 1
                for local_index, records in enumerate(batches)
            }
            for future in as_completed(futures):
                batch_index = futures[future]
                try:
                    result_index, batch_rows, batch_output_chars = future.result()
                    batch_results[result_index + 1] = (batch_rows, batch_output_chars)
                except Exception as error:  # preserve the lowest input-batch failure deterministically
                    batch_errors.append((batch_index, error))
        if batch_errors:
            batch_index, error = min(batch_errors, key=lambda item: item[0])
            if isinstance(error, MediaRouteScoringV3Error):
                message = str(error)
                if "FEATURE_CONTRACT_INVALID" not in message:
                    message = f"FEATURE_CONTRACT_INVALID: {message}"
                raise MediaRouteScoringV3Error(
                    f"{message}（第 {batch_index}/{len(batches)} 批）"
                ) from error
            raise error
        for batch_index in range(1, len(batches) + 1):
            batch_rows, batch_output_chars = batch_results[batch_index]
            rows.extend(batch_rows)
            output_chars += batch_output_chars
    else:
        worker_count = 1

    expected_ids = [str(record["shot_id"]) for record in sequence["flat"]]
    normalized = validate_classification_features(rows, expected_ids)
    return normalized, {
        "feature_batch_count": len(batches),
        "feature_batch_size": batch_size,
        "feature_shot_count": len(expected_ids),
        "feature_output_chars": output_chars,
        "feature_parallel_workers": worker_count,
        "feature_elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
    }


def validate_plan(data: Any, records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]] | None, str]:
    payload = obj(data)
    values = arr(payload.get("p"))
    if not values:
        values = arr(payload.get("plans"))
    ids = [record["shot_id"] for record in records]
    if len(values) != len(ids):
        return None, f"全局视觉规划模型输出镜头数不一致：期望 {len(ids)}，实际 {len(values)}。"

    mapping: dict[str, dict[str, Any]] = {}
    for raw in values:
        if isinstance(raw, list):
            if len(raw) != 7:
                return None, "全局视觉规划模型紧凑输出字段数不正确。"
            shot_id, scene_context, shot_size, carrier_mode, viewpoint, composition, visual_focus = [
                text(item) for item in raw
            ]
        else:
            item = obj(raw)
            shot_id = text(item.get("shot_id") or item.get("id"))
            scene_context = text(item.get("scene_context") or item.get("sc"))
            shot_size = text(item.get("shot_size") or item.get("sz"))
            carrier_mode = text(item.get("carrier_mode") or item.get("cm"))
            viewpoint = text(item.get("viewpoint") or item.get("vp"))
            composition = text(item.get("composition") or item.get("cp"))
            visual_focus = text(item.get("visual_focus") or item.get("vf"))

        # 先归一化模型/编导可能使用的口语化别名，再进入标准枚举校验。
        shot_size = SHOT_SIZE_ALIASES.get(shot_size, shot_size)
        carrier_mode = CARRIER_MODE_ALIASES.get(carrier_mode, carrier_mode)
        story_context = obj(
            next((record.get("story_context") for record in records if record.get("shot_id") == shot_id), {})
        )
        design_type = text(story_context.get("shot_design_type"))
        # 模型有时会被镜头事件中的人物词带偏，返回一个已知但与编导锁定
        # 类型冲突的承载方式。对已锁定的空镜类型做确定性纠偏，避免让
        # 可修复的模型偏差阻断整组逐镜脚本；普通镜头仍走严格校验。
        if design_type == "物件空镜" and carrier_mode in CARRIER_MODES and carrier_mode != "物件":
            carrier_mode = "物件"
        elif design_type == "空间空镜" and carrier_mode in CARRIER_MODES and carrier_mode not in {"空间", "建筑环境"}:
            carrier_mode = "建筑环境" if shot_size in WIDE_SHOT_SIZES else "空间"
        elif design_type in {"意象镜头", "意识流镜头"} and carrier_mode in CARRIER_MODES and carrier_mode not in {"物件", "空间", "建筑环境"}:
            carrier_mode = "空间" if shot_size in WIDE_SHOT_SIZES else "物件"
        plan = {
            "shot_id": shot_id,
            "scene_context": scene_context,
            "shot_size": shot_size,
            "carrier_mode": carrier_mode,
            "viewpoint": viewpoint,
            "composition": composition,
            "visual_focus": visual_focus,
        }
        if not shot_id or shot_id in mapping:
            return None, "全局视觉规划模型输出存在空或重复 shot_id。"
        if not scene_context or not viewpoint or not visual_focus:
            return None, f"{shot_id} 缺少必要规划字段。"
        if shot_size not in SHOT_SIZES:
            return None, f"{shot_id}.shot_size 不合法。"
        if carrier_mode not in CARRIER_MODES:
            return None, f"{shot_id}.carrier_mode 不合法。"
        if composition not in COMPOSITIONS:
            return None, f"{shot_id}.composition 不合法。"
        if design_type == "空间空镜" and carrier_mode not in {"空间", "建筑环境"}:
            return None, f"{shot_id} 已锁定为空间空镜，承载方式必须是空间或建筑环境。"
        if design_type == "物件空镜" and carrier_mode != "物件":
            return None, f"{shot_id} 已锁定为物件空镜，承载方式必须是物件。"
        if design_type in {"意象镜头", "意识流镜头"} and carrier_mode not in {"物件", "空间", "建筑环境"}:
            return None, f"{shot_id} 已锁定为{design_type}，承载方式必须是物件、空间或建筑环境。"
        mapping[shot_id] = plan

    if set(mapping) != set(ids):
        return None, "全局视觉规划模型输出的 shot_id 与输入不一致。"
    ordered = [mapping[shot_id] for shot_id in ids]

    if len(ordered) >= 3:
        rhythm_error = _first_rhythm_error(ordered)
        if rhythm_error:
            # 景别/承载节奏属于可修复问题：返回已解析计划，由上层做确定性
            # 纠偏，避免单条节奏问题直接中断整组逐镜脚本。
            return ordered, rhythm_error
    return ordered, ""


def _rhythm_errors(plans: list[dict[str, Any]]) -> list[str]:
    """返回景别/承载节奏问题，校验与自动纠偏共用同一套规则。"""

    errors: list[str] = []
    sizes = [plan["shot_size"] for plan in plans]
    if len(sizes) < 3:
        return errors
    if len(set(sizes)) < 2:
        errors.append("全局规划只使用了一种景别。")
    if all(plan["carrier_mode"] == "完整人物" for plan in plans):
        errors.append("全局规划全部使用完整人物承载。")
    for index in range(len(sizes) - 2):
        if sizes[index] == sizes[index + 1] == sizes[index + 2]:
            errors.append("存在连续三条相同景别。")
            break
    if len(sizes) >= 5:
        size_set = set(sizes)
        if not size_set & WIDE_SHOT_SIZES:
            errors.append("五条及以上镜头缺少远/全类景别。")
        if not size_set & MID_SHOT_SIZES:
            errors.append("五条及以上镜头缺少中景。")
        if not size_set & CLOSE_SHOT_SIZES:
            errors.append("五条及以上镜头缺少近/特类景别。")
    return errors


def _first_rhythm_error(plans: list[dict[str, Any]]) -> str:
    errors = _rhythm_errors(plans)
    return errors[0] if errors else ""


def _plan_design_type(plan: dict[str, Any], records_by_id: Mapping[str, Any]) -> str:
    record = obj(records_by_id.get(plan.get("shot_id")))
    return text(obj(record.get("story_context")).get("shot_design_type"))


def _set_repaired_shot_size(plan: dict[str, Any], design_type: str, new_size: str) -> None:
    plan["shot_size"] = new_size
    # 与校验阶段的承载纠偏保持一致：空镜/意象类型换景别时同步修正承载。
    if design_type == "空间空镜":
        plan["carrier_mode"] = "建筑环境" if new_size in WIDE_SHOT_SIZES else "空间"
    elif design_type in {"意象镜头", "意识流镜头"}:
        plan["carrier_mode"] = "空间" if new_size in WIDE_SHOT_SIZES else "物件"


def repair_plan_rhythm(
    plans: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, str]:
    """对全局视觉规划的景别/承载节奏做确定性纠偏，替代直接拦截。"""

    ordered = [dict(plan) for plan in plans]
    records_by_id = {record.get("shot_id"): record for record in records}
    for _ in range(4):
        if not _rhythm_errors(ordered):
            return ordered, ""
        if not _apply_rhythm_repair(ordered, records_by_id):
            return None, _first_rhythm_error(ordered)
    remaining = _rhythm_errors(ordered)
    if remaining:
        return None, remaining[0]
    return ordered, ""


def _apply_rhythm_repair(
    ordered: list[dict[str, Any]],
    records_by_id: Mapping[str, Any],
) -> bool:
    """尝试修复一个节奏问题；成功返回 True，外层循环会重新校验。"""

    sizes = [plan["shot_size"] for plan in ordered]
    count = len(ordered)
    design_types = [_plan_design_type(plan, records_by_id) for plan in ordered]
    locked_types = {"空间空镜", "物件空镜", "意象镜头", "意识流镜头"}

    def ok(index: int, new_size: str) -> bool:
        if new_size not in SHOT_SIZES:
            return False
        window = sizes[max(0, index - 2):index] + [new_size] + sizes[index + 1:min(count, index + 3)]
        return not any(window[i] == window[i + 1] == window[i + 2] for i in range(len(window) - 2))

    def preferred() -> list[int]:
        normal = [index for index in range(count) if design_types[index] not in locked_types]
        locked = [index for index in range(count) if design_types[index] in locked_types]
        return normal + locked

    def missing_categories() -> set[str]:
        if count < 5:
            return set()
        size_set = set(sizes)
        missing: set[str] = set()
        if not size_set & WIDE_SHOT_SIZES:
            missing.add("wide")
        if not size_set & MID_SHOT_SIZES:
            missing.add("mid")
        if not size_set & CLOSE_SHOT_SIZES:
            missing.add("close")
        return missing

    missing = missing_categories()

    def size_pool() -> list[str]:
        pool: list[str] = []
        if "wide" in missing:
            pool += ["远景", "全景"]
        if "mid" in missing:
            pool += ["中景"]
        if "close" in missing:
            pool += ["近景", "特写"]
        pool += ["远景", "全景", "中景", "近景", "特写", "超远景", "极特写"]
        return pool

    def apply_size(index: int, new_size: str) -> bool:
        if new_size not in SHOT_SIZES or new_size == sizes[index] or not ok(index, new_size):
            return False
        _set_repaired_shot_size(ordered[index], design_types[index], new_size)
        return True

    errors = _rhythm_errors(ordered)

    # 1. 连续三条相同景别：优先改动中间一条，且选择能补足缺失类别的景别。
    if any("连续三条相同景别" in error for error in errors):
        for index in range(count - 2):
            if sizes[index] == sizes[index + 1] == sizes[index + 2]:
                avoid = {sizes[index], sizes[index + 2]}
                for candidate_index in preferred():
                    if candidate_index not in (index, index + 1, index + 2):
                        continue
                    for size in size_pool():
                        if size in avoid or size == sizes[candidate_index]:
                            continue
                        if apply_size(candidate_index, size):
                            return True
                return False

    # 2. 只使用一种景别：把中间一条改成其他景别（优先缺失类别）。
    if any("只使用了一种景别" in error for error in errors):
        for size in size_pool():
            if size == sizes[0]:
                continue
            for candidate_index in (count // 2, *preferred()):
                if apply_size(candidate_index, size):
                    return True
        return False

    # 3. 全部完整人物承载：把一条普通镜头改为人物局部。
    if any("全部使用完整人物承载" in error for error in errors):
        for candidate_index in preferred():
            if design_types[candidate_index] in locked_types:
                continue
            ordered[candidate_index]["carrier_mode"] = "人物局部"
            return True
        return False

    # 4. 长序列缺少远/全、中景或近/特类：逐类补足。
    for category, sizes_for_category in (
        ("wide", ["远景", "全景"]),
        ("mid", ["中景"]),
        ("close", ["近景", "特写"]),
    ):
        if category not in missing:
            continue
        for candidate_index in preferred():
            for size in sizes_for_category:
                if apply_size(candidate_index, size):
                    return True
        return False

    return False


def apply_visual_director_lock(
    plans: list[dict[str, Any]], records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把编导治理层已冻结的视觉导演书落实为素材层不可覆盖的画面约束。"""

    if len(plans) != len(records):
        raise ShotVisualArrangementValidationError("视觉导演书与素材规划镜头数量不一致")
    locked_plans: list[dict[str, Any]] = []
    for plan, record in zip(plans, records, strict=True):
        story_context = obj(record.get("story_context"))
        direction = obj(story_context.get("visual_direction"))
        if not direction:
            locked_plans.append(dict(plan))
            continue
        locked = dict(plan)
        locked["shot_size"] = text(direction.get("shot_size")) or locked["shot_size"]
        locked["viewpoint"] = text(direction.get("camera_angle")) or locked["viewpoint"]
        locked["composition"] = text(direction.get("composition")) or locked["composition"]
        locked["visual_direction"] = direction
        locked_plans.append(locked)
    return locked_plans


def mini_materials_batch(
    records: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    transport: MiniTransport | None,
) -> tuple[Any, str]:
    if transport is None:
        raise ShotVisualArrangementTransportRequired("未配置首帧资料模型传输层；当前只能完成契约测试。")
    sequence = []
    for record, plan in zip(records, plans):
        item = {
            "shot_id": record["shot_id"],
            "source_text": record["source_text"],
            "story_beat": record["story_beat"],
            "scene_context": plan["scene_context"],
            "shot_size": plan["shot_size"],
            "carrier_mode": plan["carrier_mode"],
            "viewpoint": plan["viewpoint"],
            "composition": plan["composition"],
            "visual_focus": plan["visual_focus"],
        }
        if record.get("story_context"):
            item["story_context"] = dict(record["story_context"])
        sequence.append(item)
    payload = {
        "model": MINI_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": MINI_SYSTEM_PROMPT}]},
            {
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": json.dumps({"sequence": sequence}, ensure_ascii=False),
                }],
            },
        ],
        "thinking": {"type": "disabled"},
        "text": {"format": {"type": "json_schema", "name": "frame_materials", "strict": True, "schema": MINI_SCHEMA}},
    }
    raw = _transport_to_text(transport(payload))
    data = json_value(raw)
    return (data, "") if isinstance(data, dict) else (None, "豆包 2.0 mini 未返回 JSON。")


def fallback_motion_seed(record: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
    focus = text(plan.get("visual_focus"))
    story_context = obj(record.get("story_context"))
    design_type = text(story_context.get("shot_design_type"))
    if design_type == "空间空镜":
        return f"空间保持连续，光线或环境细节出现轻微变化，最后落在{focus}的承接状态。"
    if design_type == "物件空镜":
        return f"关键物件保持可辨识状态，只发生轻微余波变化，最后停在{focus}的落点。"
    if design_type == "意象镜头":
        return f"既有象征物、光影或空间关系完成一次可见转折，最后落在{focus}的意象落点。"
    if design_type == "意识流镜头":
        return f"非文字的光影、形状或空间感知短暂失焦再回稳，最后落在{focus}的内在状态。"
    if plan.get("carrier_mode") == "物件":
        return f"关键物件保持首帧状态并轻微变化，人物视线或手部随之停住，最后落在{focus}。"
    if plan.get("carrier_mode") in ("空间", "建筑环境"):
        return f"空间关系保持稳定，人物或环境出现轻微持续变化，最后停在{focus}的余波中。"
    return f"人物保持首帧姿势，完成一个轻微动作变化，最后停在{focus}的状态。"


def fallback_material(record: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, str]:
    focus = plan["visual_focus"]
    scene = plan["scene_context"]
    if plan["carrier_mode"] == "物件":
        action = f"画面聚焦与“{focus}”直接相关的关键物件，状态刚刚发生变化"
        layers = f"近处是核心物件，中后方保留{scene}的可辨识环境关系"
    elif plan["carrier_mode"] in ("空间", "建筑环境"):
        action = f"{scene}中呈现与“{focus}”相关的空间状态"
        layers = "前景保留局部遮挡或信息支点，中景呈现主要空间，后景延伸环境纵深"
    else:
        action = f"人物处于“{record['story_beat']}”的即时状态"
        layers = f"前景保留与任务相关的局部物件，中景是人物状态，后景延伸至{scene}"
    story_context = record.get("story_context") or {}
    design_type = text(obj(story_context).get("shot_design_type"))
    if story_context:
        if design_type == "空间空镜":
            action = (
                f"只表现{scene}的既有空间状态与环境锚点，"
                f"承接{story_context.get('state_before', '')}并落到{story_context.get('state_after', '')}，不出现人物。"
            )
        elif design_type == "物件空镜":
            action = (
                f"只表现与“{focus}”相关的既有物件状态和动作余波，"
                f"承接{story_context.get('state_before', '')}并落到{story_context.get('state_after', '')}，不出现人物。"
            )
        elif design_type == "意象镜头":
            action = (
                f"只表现与“{focus}”和语义转折相关的既有象征性物件、光影或空间关系，"
                f"把{story_context.get('state_before', '')}转化为{story_context.get('state_after', '')}，不出现人物。"
            )
        elif design_type == "意识流镜头":
            action = (
                f"只表现与“{focus}”相关的非文字光影、形状或空间感知变化，"
                f"把{story_context.get('state_before', '')}过渡到{story_context.get('state_after', '')}，不出现人物。"
            )
        else:
            action = (
                f"{action} 故事动作必须体现{story_context.get('silent_action', '')}，"
                f"并呈现{story_context.get('visible_conflict', '')}与{story_context.get('turn', '')}。"
            )
        layers = (
            f"{layers} 前后状态从{story_context.get('state_before', '')}过渡到"
            f"{story_context.get('state_after', '')}，隐喻为{story_context.get('metaphor', '')}。"
        )
    return {
        "subject_action": action,
        "space_layers": layers,
        "key_objects": f"与“{focus}”相关的关键物件和环境信息支点",
        "lighting_mood": "环境自然光与局部功能光共同塑造克制而真实的情绪",
        "motion_seed": fallback_motion_seed(record, plan),
    }


def normalize_materials(data: Any, records: list[dict[str, Any]], plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_values = arr(obj(data).get("materials"))
    raw_map: dict[str, dict[str, str]] = {}
    for raw in raw_values:
        item = obj(raw)
        shot_id = text(item.get("shot_id"))
        if shot_id:
            raw_map[shot_id] = {
                "subject_action": text(item.get("subject_action")),
                "space_layers": text(item.get("space_layers")),
                "key_objects": text(item.get("key_objects")),
                "lighting_mood": text(item.get("lighting_mood")),
                "motion_seed": text(item.get("motion_seed")),
            }
    result = []
    for record, plan in zip(records, plans):
        base = fallback_material(record, plan)
        current = raw_map.get(record["shot_id"], {})
        for key in base:
            if not current.get(key):
                current[key] = base[key]
        result.append({"shot_id": record["shot_id"], **current})
    return result


def mini_materials_parallel(
    group_records: list[list[dict[str, Any]]],
    plan_map: Mapping[str, dict[str, Any]],
    transport: MiniTransport | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    batches = [(records, [plan_map[item["shot_id"]] for item in records]) for records in group_records]
    result_map: dict[str, dict[str, Any]] = {}
    fallback_batches = 0
    success_batches = 0
    if not batches:
        return [], {"mini_batches": 0, "mini_success_batches": 0, "mini_fallback_batches": 0}

    worker_count = min(MINI_PARALLEL_WORKERS, len(batches))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(mini_materials_batch, records, plans, transport): (records, plans)
            for records, plans in batches
        }
        for future in as_completed(futures):
            records, plans = futures[future]
            try:
                data, error = future.result()
            except Exception as exc:
                data, error = None, f"mini 并行任务异常：{exc}"
            if error:
                rows = [{"shot_id": record["shot_id"], **fallback_material(record, plan)} for record, plan in zip(records, plans)]
                fallback_batches += 1
            else:
                rows = normalize_materials(data, records, plans)
                success_batches += 1
            for row in rows:
                result_map[row["shot_id"]] = row

    ordered = [result_map[record["shot_id"]] for records, _ in batches for record in records]
    return ordered, {
        "mini_batches": len(batches),
        "mini_success_batches": success_batches,
        "mini_fallback_batches": fallback_batches,
        "mini_parallel_workers": worker_count,
    }


def screen_or_text_guard(
    source_text: Any,
    story_beat: Any,
    plan: Mapping[str, Any],
    material: Mapping[str, Any],
) -> str:
    combined = " ".join([
        text(source_text), text(story_beat), text(plan.get("visual_focus")),
        text(material.get("subject_action")), text(material.get("key_objects")),
    ])
    screen_words = ["手机", "屏幕", "电脑", "弹窗", "消息", "提醒", "待办", "界面", "通知"]
    card_words = ["任务卡", "看板", "文件", "排期", "工单", "表格", "卡片"]
    if plan.get("carrier_mode") == "物件" and any(word in combined for word in screen_words):
        return "屏幕仅表现亮起、弹窗或提醒状态，不要求具体可读文字、复杂界面或品牌标识。"
    if any(word in combined for word in card_words):
        return "任务卡片、看板或文件仅表现排期、堆叠或状态变化，不要求具体可读文字。"
    return ""


def is_person_carrier(plan: Mapping[str, Any]) -> bool:
    return plan.get("carrier_mode") in ["完整人物", "人物局部"]


def build_prompt(
    plan: Mapping[str, Any],
    material: Mapping[str, Any],
    has_refs: bool,
    character_anchor: str,
    source_text: str,
    story_beat: str,
    canvas: Any = None,
    story_context: Mapping[str, Any] | None = None,
) -> str:
    prefix = f"{REF_STYLE_GUIDE}\n\n" if has_refs else ""
    story_line = ""
    if story_context:
        shot_design_type = text(story_context.get("shot_design_type"))
        story_line = (
            "故事映射锚点："
            f"场景目的={text(story_context.get('scene_purpose'))}；"
            f"可见冲突={text(story_context.get('visible_conflict'))}；"
            f"无对白动作={text(story_context.get('silent_action'))}；"
            f"隐喻关系={text(story_context.get('metaphor'))}；"
            f"前状态={text(story_context.get('state_before'))}；"
            f"本段转折={text(story_context.get('turn'))}；"
            f"后状态={text(story_context.get('state_after'))}；"
            f"下段承接={text(story_context.get('bridge_to_next'))}；"
            f"语义桥接={text(story_context.get('transition_role')) or '无'}；"
            f"本镜设计类型={shot_design_type or '未指定'}。\n"
        )
        if text(story_context.get("narrative_job")):
            story_line += (
                "逐镜头导演约束："
                f"本镜新揭示={text(story_context.get('narrative_job'))}；"
                f"走位={text(story_context.get('blocking'))}；"
                f"机位动机={text(story_context.get('camera_intent'))}；"
                f"前镜承接={text(story_context.get('continuity_in'))}；"
                f"交给后镜={text(story_context.get('continuity_out'))}；"
                f"必须可见={text(story_context.get('must_show'))}；"
                f"必须避免={'、'.join(str(item) for item in arr(story_context.get('must_avoid')) if text(item))}。\n"
            )
            visual_direction = obj(story_context.get("visual_direction"))
            if visual_direction:
                story_line += (
                    "视觉导演书（必须执行）："
                    f"景别={text(visual_direction.get('shot_size'))}；"
                    f"机位={text(visual_direction.get('camera_angle'))}；"
                    f"构图={text(visual_direction.get('composition'))}；"
                    f"调度={text(visual_direction.get('staging'))}；"
                    f"光影={text(visual_direction.get('lighting'))}；"
                    f"视觉母题={text(visual_direction.get('visual_motif'))}。\n"
                )
            if character_anchor and shot_design_type in {"空间空镜", "物件空镜", "意象镜头", "意识流镜头"}:
                story_line += (
                    "非人物桥接硬约束：本镜不出现主角或数字人，不新增人物；"
                    "只表现既有空间、物件、象征性光影或非文字感知，完成前后状态的故事承接。\n"
                )
            elif character_anchor:
                story_line += (
                    "人物连续性硬约束：主角必须在本镜清晰可见，保留可辨识的脸部或完整上半身、"
                    "发型、服装与前镜一致；不得只生成屏幕、粒子、物件、空景或不含主角的环境。\n"
                )
    anchor = f"{character_anchor}，" if is_person_carrier(plan) and character_anchor else ""
    guard = screen_or_text_guard(source_text, story_beat, plan, material)
    shot_size = plan["shot_size"]
    carrier = plan["carrier_mode"]
    canvas_label = canvas_prompt_label(canvas)
    if carrier == "物件" or shot_size in ["特写", "极特写"]:
        body = (
            f"首帧画面：{canvas_label}，{plan['scene_context']}。"
            f"{anchor}{material['subject_action']}，"
            f"画面焦点直接落在{plan['visual_focus']}。"
            f"{material['space_layers']}。"
            f"{material['key_objects']}构成关键的信息支点。"
            f"{shot_size}景别，以{carrier}为主要承载，"
            f"{plan['viewpoint']}，{plan['composition']}构图。"
            f"{material['lighting_mood']}。"
        )
    elif carrier in ["空间", "建筑环境"] or shot_size in ["超远景", "远景", "全景"]:
        body = (
            f"首帧画面：{canvas_label}，{plan['scene_context']}。"
            f"{material['space_layers']}。"
            f"{anchor}{material['subject_action']}，"
            f"使{plan['visual_focus']}成为画面核心。"
            f"{material['key_objects']}构成关键的信息支点。"
            f"{shot_size}景别，以{carrier}为主要承载，"
            f"{plan['viewpoint']}，{plan['composition']}构图。"
            f"{material['lighting_mood']}。"
        )
    else:
        body = (
            f"首帧画面：{canvas_label}，{plan['scene_context']}。"
            f"{anchor}{material['subject_action']}，"
            f"画面焦点落在{plan['visual_focus']}。"
            f"{material['space_layers']}。"
            f"{material['key_objects']}构成关键的信息支点。"
            f"{shot_size}景别，以{carrier}为主要承载，"
            f"{plan['viewpoint']}，{plan['composition']}构图。"
            f"{material['lighting_mood']}。"
        )
    hard_negative = ""
    if story_context and text(story_context.get("narrative_job")):
        hard_negative = (
            "硬性画面禁令：画面内绝对不得出现任何可读文字、字母、数字、汉字、"
            "便签文字、纸张文字、屏幕 UI、聊天框、表格、图表、品牌标识或水印；"
            "所有便签、纸张、书本、屏幕、海报必须是纯色空白、无符号表面。"
        )
    return f"{prefix}{story_line}{body}{guard}{hard_negative}"


def run_shot_visual_arrangement(
    params: Any = None,
    *,
    gpt_transport: GptTransport | None = None,
    mini_transport: MiniTransport | None = None,
    feature_transport: GptTransport | None = None,
    feature_batch_size: int = FEATURE_BATCH_SIZE,
    require_classification_features: bool = False,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    debug_enabled = False
    debug_info: dict[str, Any] = {}
    try:
        raw = plain(params)
        if isinstance(raw, dict) and isinstance(raw.get("_input"), dict):
            raw = raw["_input"]
        debug_enabled = as_bool(field(raw, "debug", False))

        def mark(name: str, stage_start: float) -> None:
            if debug_enabled:
                debug_info[name] = round((time.perf_counter() - stage_start) * 1000, 1)

        stage_start = time.perf_counter()
        sequence, build_error = build_sequence(raw)
        mark("build_ms", stage_start)
        if build_error:
            if debug_enabled:
                debug_info["total_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
            return fail(build_error, debug_info if debug_enabled else None)

        stage_start = time.perf_counter()
        plan_data, gpt_error, gpt_stats = gpt_plan(sequence["gpt_groups"], gpt_transport)
        mark("gpt_ms", stage_start)
        if debug_enabled:
            debug_info.update(gpt_stats)
        if gpt_error:
            if debug_enabled:
                debug_info["total_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
            return fail(gpt_error, debug_info if debug_enabled else None)

        stage_start = time.perf_counter()
        plans, plan_error = validate_plan(plan_data, sequence["flat"])
        mark("plan_validate_ms", stage_start)
        if plan_error:
            if plans is None:
                if debug_enabled:
                    debug_info["total_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
                return fail(f"全局视觉规划模型校验失败：{plan_error}", debug_info if debug_enabled else None)
            repaired, repair_error = repair_plan_rhythm(plans, sequence["flat"])
            if repair_error:
                if debug_enabled:
                    debug_info["total_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
                return fail(
                    f"全局视觉规划模型校验失败：{plan_error}（自动纠偏未通过：{repair_error}）",
                    debug_info if debug_enabled else None,
                )
            plans = repaired

        plans = apply_visual_director_lock(plans, sequence["flat"])
        plan_map = {item["shot_id"]: item for item in plans}
        classification_features: list[dict[str, Any]] | None = None
        if require_classification_features:
            try:
                classification_features, feature_stats = run_classification_feature_batches(
                    raw,
                    transport=feature_transport or gpt_transport,
                    plans=plan_map,
                    batch_size=feature_batch_size,
                )
                if debug_enabled:
                    debug_info.update(feature_stats)
            except MediaRouteScoringV3Error as error:
                if debug_enabled:
                    debug_info["total_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
                return fail(str(error), debug_info if debug_enabled else None)

        stage_start = time.perf_counter()
        materials, mini_stats = mini_materials_parallel(sequence["groups"], plan_map, mini_transport)
        mark("mini_ms", stage_start)

        stage_start = time.perf_counter()
        material_map = {item["shot_id"]: item for item in materials}
        raw_refs = field(raw, "ref_image", None)
        if raw_refs is None:
            raw_refs = field(raw, "ref_images", [])
        ref_images = refs(raw_refs)
        character_anchor = text(field(raw, "character_anchor", DEFAULT_CHARACTER_ANCHOR)) or DEFAULT_CHARACTER_ANCHOR
        character_asset_url = text(field(raw, "character_asset_url", ""))
        canvas = resolve_canvas(field(raw, "canvas", field(raw, "aspect_ratio", None)))

        prompts: list[str] = []
        output_refs: list[dict[str, list[str]]] = []
        motion_seeds: list[str] = []
        timelines: list[dict[str, int]] = []
        durations: list[int] = []
        for record in sequence["flat"]:
            shot_id = record["shot_id"]
            prompts.append(build_prompt(
                plan_map[shot_id], material_map[shot_id], bool(ref_images),
                character_anchor, record["source_text"], record["story_beat"], canvas,
                record.get("story_context"),
            ))
            shot_refs = inject_character_asset_refs(
                ref_images, character_asset_url=character_asset_url,
                has_character=is_person_carrier(plan_map[shot_id]),
            ) if character_asset_url else list(ref_images)
            output_refs.append({"ref_image": shot_refs})
            motion_seeds.append(text(material_map[shot_id].get("motion_seed")) or fallback_motion_seed(record, plan_map[shot_id]))
            timelines.append(record["timeline"])
            durations.append(record["int_duration"])
        mark("assemble_ms", stage_start)

        if debug_enabled:
            debug_info.update(mini_stats)
            debug_info["prompt_count"] = len(prompts)
            debug_info["motion_seed_count"] = len(motion_seeds)
            debug_info["total_ms"] = round((time.perf_counter() - started_at) * 1000, 1)

        output = {
            "prompt": prompts,
            "ref_image": output_refs,
            "motion_seed": motion_seeds,
            "timelines": timelines,
            "int_duration": durations,
            "error": "",
            "debug": debug_info if debug_enabled else {},
        }
        story_values = [dict(record.get("story_context") or {}) for record in sequence["flat"]]
        if any(story_values):
            output["story_context"] = story_values
        if classification_features is not None:
            output["classification_features"] = classification_features
        return output
    except ShotVisualArrangementTransportRequired:
        raise
    except Exception as error:
        if debug_enabled:
            debug_info["total_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
        return fail(f"插件执行异常：{error}", debug_info if debug_enabled else None)


__all__ = [
    "FEATURE_BATCH_SIZE",
    "FEATURE_EVALUATOR_MODEL",
    "FEATURE_EVALUATOR_SYSTEM_PROMPT",
    "FEATURE_PARALLEL_WORKERS",
    "ShotVisualArrangementValidationError",
    "ShotVisualArrangementTransportRequired",
    "apply_visual_director_lock",
    "build_sequence",
    "build_prompt",
    "run_classification_feature_batches",
    "run_shot_visual_arrangement",
]
