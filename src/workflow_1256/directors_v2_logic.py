"""``directors_v2`` 源码逻辑的无鉴权 Python 移植。

依据用户提供的源码附件（SHA-256：
``F65F26BD1C50FEC6952CD771AECE3123011C30FDD56FE7E30EDDDC651B449662``）。

原实现会调用两个外部文本模型。本模块把这两个调用改成显式注入的
``pick_model`` 和 ``director_model``，不保存、不读取、不发送任何密钥；
其余输入读取、JSON 清洗、Unit 切分、分组补全、字段清洗、数量检查和
原文覆盖检查保持源码逻辑。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol


class DirectorsV2LogicError(RuntimeError):
    """源码逻辑在输入、模型结果或分段完整性上失败。"""


class TextModel(Protocol):
    def __call__(self, system_prompt: str, user_prompt: str) -> str: ...


MIN_SEGMENTS = 8
MAX_SEGMENTS = 12

RHYTHM_CODES = {
    "hook", "proof", "fact", "turn", "why", "load", "wrong", "method", "end", "act", "move"
}
RELATION_CODES = {
    "new", "add", "up", "cause", "turn", "why", "confirm", "wrong", "method", "end", "act", "move"
}
NEED_CODES = {"reality", "contrast", "mechanism", "emotion", "conclusion"}
ROUTE_CODES = {"scene", "symbol", "host", "logic"}
DOMAIN_CODES = {"life", "work", "public", "relationship", "system", "product", "mindset"}
VARIATION_CODES = {"space", "carrier", "viewpoint", "rhythm", "density"}

DIR_CARDS = {
    "view_dir": "观点型：突出反常识判断、认知冲突、论点递进与最终立场。",
    "story_dir": "故事型：突出人物处境、事件发展、冲突转折与结尾感悟。",
    "know_dir": "知识型：突出概念关系、因果机制、理解层级与方法清晰度。",
    "case_dir": "案例型：突出背景、问题、动作、变化、证据与复盘价值。",
    "sell_dir": "转化型：突出用户痛点、价值证明、信任建立与行动理由。",
    "emo_dir": "情绪型：突出真实处境、生活细节、身份代入与被理解感。",
    "list_dir": "清单型：突出步骤层级、方法区分、可执行动作与总结。",
    "ip_dir": "IP型：突出人格态度、身份可信度、价值观与长期一致性。",
}


def safe_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return "{}"


def compact(value: Any) -> str:
    return re.sub(r"\s+", "", safe_str(value))


def cut(value: Any, limit: int) -> str:
    return safe_str(value).strip()[:limit]


def get_arg(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    try:
        value = getattr(obj, key)
        return default if value is None else value
    except Exception:
        pass
    try:
        return obj.get(key, default)
    except Exception:
        return default


def read_text(args: Any) -> Any:
    if isinstance(args, str):
        return args

    keys = ["text", "Text", "TEXT", "prompt", "content", "script", "query", "String1", "string1"]
    for key in keys:
        value = get_arg(args, key)
        if safe_str(value).strip():
            return value

    for parent_key in ["input", "params", "arguments", "body", "data", "payload"]:
        parent = get_arg(args, parent_key)
        if parent is None:
            continue
        if isinstance(parent, str):
            try:
                parent = json.loads(parent)
            except Exception:
                if parent.strip():
                    return parent
                continue
        for key in keys:
            value = get_arg(parent, key)
            if safe_str(value).strip():
                return value
    return ""


def parse_json_obj(raw: Any) -> dict[str, Any]:
    raw_text = safe_str(raw).replace("```json", "").replace("```", "").strip()
    if not raw_text:
        return {}
    try:
        result = json.loads(raw_text)
        return result if isinstance(result, dict) else {}
    except Exception:
        pass
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start >= 0 and end > start:
        try:
            result = json.loads(raw_text[start : end + 1])
            return result if isinstance(result, dict) else {}
        except Exception:
            pass
    return {}


def clean_code_list(value: Any, allowed: set[str], default: list[str], limit: int) -> list[str]:
    if not isinstance(value, list):
        return default[:]
    result: list[str] = []
    for item in value:
        item_text = cut(item, 30)
        if item_text in allowed and item_text not in result:
            result.append(item_text)
    return result[:limit] if result else default[:]


def clean_pick(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        obj = {}
    main = cut(obj.get("dir"), 30)
    sub = cut(obj.get("sub"), 30)
    if main not in DIR_CARDS:
        main = "view_dir"
    if sub not in DIR_CARDS or sub == main:
        sub = ""
    hints: list[str] = []
    for item in obj.get("hint", [])[:2]:
        item_text = cut(item, 40)
        if item_text:
            hints.append(item_text)
    return {
        "dir": main,
        "sub": sub,
        "why": cut(obj.get("why") or "根据文案表达方式选择导演。", 80),
        "hint": hints,
    }


def clean_director_plan(obj: Any, director_type: str) -> dict[str, Any]:
    data = obj.get("d", obj) if isinstance(obj, dict) else {}
    if not isinstance(data, dict):
        data = {}
    return {
        "director_type": director_type,
        "core": cut(data.get("core"), 80),
        "tone": cut(data.get("tone"), 35),
        "emo": cut(data.get("emo"), 70),
        "goal": cut(data.get("goal"), 80),
        "open": cut(data.get("open"), 70),
        "spine": cut(data.get("spine"), 150),
        "arc": clean_code_list(data.get("arc"), RHYTHM_CODES, ["hook", "proof", "turn", "why", "method", "end"], 10),
        "expression_domains": clean_code_list(data.get("expression_domains"), DOMAIN_CODES, ["life", "work", "system"], 4),
        "variation_focus": clean_code_list(data.get("variation_focus"), VARIATION_CODES, ["space", "carrier", "viewpoint"], 4),
        "rule": cut(data.get("rule") or "相邻段改变表达域、密度或观看角度，保持全文推进。", 100),
    }


def allow_logic(text: str) -> bool:
    return (
        any(re.search(pattern, text) for pattern in [
            r"真正的问题", r"关键是", r"核心是", r"本质是", r"结论是", r"不是.+而是",
            r"最重要的是", r"归根结底", r"真正会用", r"真正要",
        ])
        or bool(re.search(r"\d+(?:\.\d+)?\s*[%％]", text))
        or len(set(re.findall(r"20\d{2}", text))) >= 2
        or bool(re.search(r"(增长|下降|提升|减少|增加|同比|环比).{0,12}\d+", text))
        or any(re.search(pattern, text) for pattern in [
            r"→", r"->", r"⇒", r"流程", r"步骤", r"链路", r"因果链", r"机制链",
            r"第一步", r"第二步", r"第三步", r"先.+再.+",
        ])
    )


def scene_first(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in [
        r"老板", r"领导", r"客户", r"同事", r"下班", r"手机", r"工作群", r"方案", r"PPT",
        r"任务", r"文件", r"审核", r"改稿", r"会议", r"工位", r"加活", r"加两个活",
    ])


def default_routes(need: str) -> list[str]:
    if need == "mechanism":
        return ["symbol", "scene"]
    if need == "conclusion":
        return ["host", "scene"]
    if need in {"emotion", "contrast"}:
        return ["scene", "symbol"]
    return ["scene"]


def clean_routes(value: Any, need: str, text: str) -> list[str]:
    routes = clean_code_list(value, ROUTE_CODES, [], 2)
    if not routes:
        routes = default_routes(need)
    if "logic" in routes and not allow_logic(text):
        routes = [item for item in routes if item != "logic"]
    if not routes:
        routes = [item for item in default_routes(need) if item != "logic"]
    if scene_first(text):
        if "scene" not in routes and need != "conclusion":
            routes = ["scene"] + routes[:1]
        elif "scene" in routes:
            routes = ["scene"] + [item for item in routes if item != "scene"]
    result: list[str] = []
    for item in routes:
        if item in ROUTE_CODES and item not in result:
            result.append(item)
    return result[:2] or ["scene"]


def normalize_relation(relation: str, rhythm: str) -> str:
    rhythm_map = {
        "hook": "new", "proof": "confirm", "fact": "add", "turn": "turn", "why": "why",
        "load": "why", "wrong": "wrong", "method": "method", "end": "end", "act": "act",
    }
    if rhythm in rhythm_map:
        return rhythm_map[rhythm]
    return relation if relation in RELATION_CODES else "move"


def build_units(text: str) -> list[dict[str, str]]:
    raw_parts = re.split(r"(?<=[。！？；\n])", text)
    units: list[dict[str, str]] = []
    pending = ""
    for part in raw_parts:
        if not part:
            continue
        if not compact(part):
            pending += part
            continue
        units.append({"id": "u%02d" % (len(units) + 1), "text": pending + part})
        pending = ""
    if pending:
        if units:
            units[-1]["text"] += pending
        else:
            units.append({"id": "u%02d" % (len(units) + 1), "text": pending})
    return [item for item in units if compact(item["text"])]


def unit_prompt_text(units: list[dict[str, str]]) -> str:
    return "\n".join(item["id"] + "=" + item["text"].replace("\n", "\\n") for item in units)


def normalize_unit_groups(raw_segments: Any, units: list[dict[str, str]]) -> list[dict[str, Any]]:
    valid_ids = [item["id"] for item in units]
    valid_set = set(valid_ids)
    if not isinstance(raw_segments, list):
        raw_segments = []
    groups: list[dict[str, Any]] = []
    assigned: dict[str, int] = {}
    last_group_index = -1
    for raw in raw_segments[:MAX_SEGMENTS]:
        if not isinstance(raw, dict):
            continue
        raw_ids = raw.get("u", raw.get("units", []))
        if not isinstance(raw_ids, list):
            raw_ids = []
        group_ids: list[str] = []
        for unit_id in raw_ids:
            unit_id = safe_str(unit_id).strip()
            if unit_id in valid_set and unit_id not in assigned:
                group_ids.append(unit_id)
                assigned[unit_id] = len(groups)
        if group_ids:
            groups.append({"raw": raw, "ids": group_ids})
            last_group_index = len(groups) - 1
    if not groups:
        return []
    for unit_id in valid_ids:
        if unit_id in assigned:
            continue
        unit_index = valid_ids.index(unit_id)
        target_group = None
        for group_index, group in enumerate(groups):
            if unit_index < valid_ids.index(group["ids"][0]):
                target_group = group_index
                break
        if target_group is None:
            target_group = last_group_index
        groups[target_group]["ids"].append(unit_id)
        assigned[unit_id] = target_group
    ordered_groups: list[dict[str, Any]] = []
    for group in groups:
        ids = sorted(group["ids"], key=valid_ids.index)
        if ids:
            ordered_groups.append({"raw": group["raw"], "ids": ids})
    return ordered_groups


def build_segment_beats(raw_obj: Any, text: str, units: list[dict[str, str]]) -> list[dict[str, Any]]:
    raw_segments = raw_obj.get("seg", raw_obj.get("segment_beats", [])) if isinstance(raw_obj, dict) else []
    unit_map = {item["id"]: item["text"] for item in units}
    groups = normalize_unit_groups(raw_segments, units)
    if not groups:
        return []
    result: list[dict[str, Any]] = []
    for group in groups:
        raw = group["raw"]
        segment_text = "".join(unit_map.get(unit_id, "") for unit_id in group["ids"])
        if not compact(segment_text):
            continue
        rhythm = cut(raw.get("r") or raw.get("rhythm") or "move", 20)
        if rhythm not in RHYTHM_CODES:
            rhythm = "move"
        relation = cut(raw.get("l") or raw.get("relation") or "move", 20)
        need = cut(raw.get("n") or raw.get("expression_need") or "reality", 20)
        if need not in NEED_CODES:
            need = "reality"
        routes = raw.get("v", raw.get("route_candidates", raw.get("routes", [])))
        result.append({
            "segment_index": len(result),
            "segment_text": segment_text,
            "rhythm": rhythm,
            "segment_goal": cut(raw.get("g") or raw.get("segment_goal") or "推进当前段理解。", 60),
            "beats": [{
                "relation": normalize_relation(relation, rhythm),
                "expression_need": need,
                "route_candidates": clean_routes(routes, need, segment_text),
            }],
        })
    return result


def segment_texts_cover_full_text(full_text: str, segments: list[dict[str, Any]]) -> bool:
    return compact(full_text) == compact("".join(item.get("segment_text", "") for item in segments))


def build_segments_compat(segment_beats: list[dict[str, Any]]) -> list[str]:
    return [safe_str(item.get("segment_text", "")) for item in segment_beats if safe_str(item.get("segment_text", "")).strip()]


MINI_PICK_SYS = """你是短视频导演路由器。

根据完整文案，选择主导演dir和副导演sub。

dir/sub只允许：
view_dir、story_dir、know_dir、case_dir、sell_dir、emo_dir、list_dir、ip_dir。

view_dir：观点判断、反常识、认知冲突。
story_dir：人物事件、经历、冲突。
know_dir：概念解释、机制、知识。
case_dir：案例、复盘、变化。
sell_dir：需求、价值、信任、转化。
emo_dir：真实处境、情绪共鸣、身份代入。
list_dir：步骤、清单、方法归纳。
ip_dir：人格态度、价值观表达。

只输出JSON：
{
  "dir":"view_dir",
  "sub":"emo_dir",
  "why":"一句话判断",
  "hint":["提示1","提示2"]
}""".strip()


def build_director_and_segment_sys(pick: Mapping[str, Any]) -> str:
    return """你是短视频总导演兼分段导演。

主导演：%s
副导演：%s
路由理由：%s

输入中每个原文单元都有ID，例如u01、u02。

你绝对不能复写原文。
你只输出每一段包含的 Unit ID 数组 u。

一次输出：
1. d：全文导演策略；
2. seg：原文连续语义分段。

一、d字段：
core、tone、emo、goal、open、spine、arc、
expression_domains、variation_focus、rule。

字段限制：
core≤50字；tone≤20字；emo≤45字；goal≤50字；
open≤45字；spine≤100字；rule≤60字。

arc只允许：
hook、proof、fact、turn、why、load、wrong、method、end、act、move。

expression_domains只允许：
life、work、public、relationship、system、product、mindset。

variation_focus只允许：
space、carrier、viewpoint、rhythm、density。

二、seg规则：
- 输出8到12个连续segment。
- 每个Unit ID必须出现一次且只出现一次。
- Unit ID顺序必须与输入原文顺序一致。
- 禁止逐句机械拆分。
- 普通补充句、同一观点解释、同一情绪加强，合并在同一段。
- 仅在事实证明、转折、机制解释、认知负荷、错误用法、方法、结论、行动时切段。
- 每个segment固定一个beat，不输出beats数组。
- g写15字以内的理解任务。
- 不输出画面、人物动作、道具、空间、镜头、构图、Prompt、最终visual_type。

r只允许：
hook、proof、fact、turn、why、load、wrong、method、end、act、move。

l只允许：
new、add、up、cause、turn、why、confirm、wrong、method、end、act、move。

n只允许：
reality、contrast、mechanism、emotion、conclusion。

v只允许：
scene、symbol、host、logic。

logic仅允许独立强观点、明确数据、明确流程链或机制链。
普通效率对比、老板加活、改稿、焦虑疲惫不用logic。

输出JSON：
{
  "d":{
    "core":"核心判断",
    "tone":"表达气质",
    "emo":"情绪路径",
    "goal":"观众变化",
    "open":"开场策略",
    "spine":"推进链",
    "arc":["hook","proof","turn","why","wrong","method","end","act"],
    "expression_domains":["work","mindset","system"],
    "variation_focus":["viewpoint","density","rhythm"],
    "rule":"变化原则"
  },
  "seg":[
    {
      "u":["u01","u02"],
      "r":"hook",
      "g":"建立共鸣",
      "l":"new",
      "n":"emotion",
      "v":["scene","host"]
    }
  ]
}
""" % (
        DIR_CARDS.get(pick["dir"], DIR_CARDS["view_dir"]),
        DIR_CARDS.get(pick["sub"], "无"),
        pick["why"],
    )


def ensure_director_plan_ready(plan: Mapping[str, Any]) -> None:
    required_fields = ("core", "tone", "emo", "goal", "open", "spine")
    missing = [key for key in required_fields if not safe_str(plan.get(key)).strip()]
    if missing:
        raise DirectorsV2LogicError("GPT 导演策略字段不完整：" + ", ".join(missing))


def run_directors_v2_logic(
    args: Any,
    *,
    pick_model: Callable[[str, str], str] | TextModel,
    director_model: Callable[[str, str], str] | TextModel,
) -> dict[str, Any]:
    """运行源码逻辑；两个模型调用必须由调用方注入。"""

    text = safe_str(read_text(args)).strip()
    if not text:
        raise ValueError("text 为空，请检查入参是否传入 text。")

    units = build_units(text)
    if not units:
        raise DirectorsV2LogicError("原文无法切分为有效 Unit。")

    pick_raw = pick_model(MINI_PICK_SYS, "完整文案：\n" + text)
    pick_obj = parse_json_obj(pick_raw)
    if not pick_obj:
        raise DirectorsV2LogicError("Mini 路由模型未返回有效 JSON。")
    pick = clean_pick(pick_obj)

    director_raw = director_model(
        build_director_and_segment_sys(pick),
        "原文Unit列表：\n" + unit_prompt_text(units),
    )
    if not safe_str(director_raw).strip():
        raise DirectorsV2LogicError("GPT 未返回正文。")
    director_obj = parse_json_obj(director_raw)
    if not director_obj:
        raise DirectorsV2LogicError("GPT 未返回可解析的 JSON 对象。")
    if not isinstance(director_obj.get("d"), dict):
        raise DirectorsV2LogicError("GPT 输出缺少 d 导演策略对象。")
    if not isinstance(director_obj.get("seg"), list):
        raise DirectorsV2LogicError("GPT 输出缺少 seg 分段数组。")

    director_plan = clean_director_plan(director_obj, pick["dir"])
    ensure_director_plan_ready(director_plan)
    segment_beats = build_segment_beats(director_obj, text, units)
    if not segment_beats:
        raise DirectorsV2LogicError("GPT 未返回有效 Unit 分组。")
    segment_count = len(segment_beats)
    if segment_count < MIN_SEGMENTS or segment_count > MAX_SEGMENTS:
        raise DirectorsV2LogicError(
            "GPT 分段数量不符合要求：%s，要求 %s-%s 段。" % (segment_count, MIN_SEGMENTS, MAX_SEGMENTS)
        )
    if not segment_texts_cover_full_text(text, segment_beats):
        raise DirectorsV2LogicError("Unit 分组重建后未完整覆盖原文。")
    segments = build_segments_compat(segment_beats)
    if len(segments) != segment_count:
        raise DirectorsV2LogicError("segments 与 segment_beats 数量不一致。")
    return {
        "ok": True,
        "director_plan": director_plan,
        "segment_beats": segment_beats,
        "segments": segments,
    }
