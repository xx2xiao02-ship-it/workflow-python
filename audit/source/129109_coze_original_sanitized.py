import requests
import json
import re


PLUGIN_VERSION = "gpt55-chat-clean-retry-20260705-v6"


# =========================
# 真实配置
# =========================

ARK_API_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
ARK_API_KEY = ""
MINI_MODEL = "ep-20260609123759-6sv2j"

# 只使用已验证可返回正文的 Chat Completions 路由。
GPT_API_URL = "https://api.aibh.site/v1/chat/completions"
GPT_API_KEY = ""
GPT_MODEL = "gpt-5.5"


# =========================
# 运行配置
# =========================

# 插件总时限 180 秒：Mini 35 秒 + GPT 单次 100 秒，不重试。
ARK_TIMEOUT = 35
GPT_TIMEOUT = 100
MIN_SEGMENTS = 8
MAX_SEGMENTS = 12

RHYTHM_CODES = {
    "hook", "proof", "fact", "turn", "why",
    "load", "wrong", "method", "end", "act", "move"
}

RELATION_CODES = {
    "new", "add", "up", "cause", "turn", "why",
    "confirm", "wrong", "method", "end", "act", "move"
}

NEED_CODES = {
    "reality", "contrast", "mechanism", "emotion", "conclusion"
}

ROUTE_CODES = {"scene", "symbol", "host", "logic"}

DOMAIN_CODES = {
    "life", "work", "public", "relationship",
    "system", "product", "mindset"
}

VARIATION_CODES = {
    "space", "carrier", "viewpoint", "rhythm", "density"
}

DIR_CARDS = {
    "view_dir": "观点型：突出反常识判断、认知冲突、论点递进与最终立场。",
    "story_dir": "故事型：突出人物处境、事件发展、冲突转折与结尾感悟。",
    "know_dir": "知识型：突出概念关系、因果机制、理解层级与方法清晰度。",
    "case_dir": "案例型：突出背景、问题、动作、变化、证据与复盘价值。",
    "sell_dir": "转化型：突出用户痛点、价值证明、信任建立与行动理由。",
    "emo_dir": "情绪型：突出真实处境、生活细节、身份代入与被理解感。",
    "list_dir": "清单型：突出步骤层级、方法区分、可执行动作与总结。",
    "ip_dir": "IP型：突出人格态度、身份可信度、价值观与长期一致性。"
}


# =========================
# 基础函数
# =========================

def safe_str(value):
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


def safe_json(value):
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return "{}"


def compact(value):
    return re.sub(r"\s+", "", safe_str(value))


def cut(value, limit):
    return safe_str(value).strip()[:limit]


def get_arg(obj, key, default=None):
    if obj is None:
        return default

    if isinstance(obj, dict):
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


def read_text(args):
    if isinstance(args, str):
        return args

    keys = [
        "text", "Text", "TEXT",
        "prompt", "content", "script",
        "query", "String1", "string1"
    ]

    for key in keys:
        value = get_arg(args, key)

        if safe_str(value).strip():
            return value

    for parent_key in [
        "input", "params", "arguments",
        "body", "data", "payload"
    ]:
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


def parse_json_obj(raw):
    raw = safe_str(raw)
    raw = raw.replace("```json", "").replace("```", "").strip()

    if not raw:
        return {}

    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")

    if start >= 0 and end > start:
        try:
            result = json.loads(raw[start:end + 1])
            return result if isinstance(result, dict) else {}
        except Exception:
            pass

    return {}


def clean_code_list(value, allowed, default, limit):
    if not isinstance(value, list):
        return default[:]

    result = []

    for item in value:
        item = cut(item, 30)

        if item in allowed and item not in result:
            result.append(item)

    return result[:limit] if result else default[:]


# =========================
# 模型调用
# =========================

def call_ark(system_prompt, user_prompt):
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + ARK_API_KEY
    }

    payload = {
        "model": MINI_MODEL,
        "instructions": system_prompt,
        "input": user_prompt,
        "temperature": 0.1,
        "max_output_tokens": 380,
        "thinking": {
            "type": "disabled"
        }
    }

    response = requests.post(
        ARK_API_URL,
        headers=headers,
        json=payload,
        timeout=ARK_TIMEOUT
    )

    if response.status_code >= 400:
        raise Exception(
            "Ark API错误: "
            + str(response.status_code)
            + " "
            + response.text[:500]
        )

    data = response.json()

    if data.get("output_text"):
        return safe_str(data["output_text"]).strip()

    texts = []

    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue

        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue

            text = content.get("text") or content.get("output_text")

            if text:
                texts.append(safe_str(text))

    return "\n".join(texts).strip()



def _response_json_utf8(response):
    """
    强制按 UTF-8 读取响应字节，避免中转站未声明 charset 时
    requests 把中文错误按 latin-1 解码。
    """
    raw = response.content or b""
    last_error = None

    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return json.loads(raw.decode(encoding)), encoding
        except Exception as error:
            last_error = error

    try:
        return response.json(), "requests_json"
    except Exception:
        pass

    preview = raw[:800].decode("utf-8", errors="replace")
    raise Exception(
        "GPT 返回非JSON或编码无法解析: "
        + preview
        + " | decode_error="
        + safe_str(last_error)
    )


def _extract_text_value(value):
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        parts = []

        for item in value:
            item_text = _extract_text_value(item)

            if item_text:
                parts.append(item_text)

        return "\n".join(parts).strip()

    if isinstance(value, dict):
        for key in ("text", "value", "content", "output_text"):
            item_text = _extract_text_value(value.get(key))

            if item_text:
                return item_text

    return ""


def _count_mojibake_marks(text):
    if not isinstance(text, str):
        return 0

    return sum(
        text.count(char)
        for char in (
            "æ", "å", "ç", "è", "é", "ê",
            "Ã", "Â", "Ð", "Ñ", "¤", "¦", "§"
        )
    )


def _count_cjk(text):
    if not isinstance(text, str):
        return 0

    return sum(
        1
        for char in text
        if "\u4e00" <= char <= "\u9fff"
    )


def fix_mojibake_text(text):
    """
    仅在文本明显像 UTF-8 被 latin-1 错解时修复。
    正常中文无法 latin-1 编码，会原样保留。
    """
    text = safe_str(text)

    if _count_mojibake_marks(text) < 2:
        return text

    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except Exception:
        return text

    if (
        _count_mojibake_marks(fixed) < _count_mojibake_marks(text)
        and _count_cjk(fixed) >= _count_cjk(text)
    ):
        return fixed

    return text


def _response_shape(data):
    if not isinstance(data, dict):
        return "响应根节点不是对象，类型=%s" % type(data).__name__

    parts = [
        "top_keys=%s" % list(data.keys())[:20]
    ]

    choices = data.get("choices")

    if isinstance(choices, list) and choices:
        choice = choices[0]

        if isinstance(choice, dict):
            parts.append(
                "choices[0]_keys=%s" % list(choice.keys())[:20]
            )
            parts.append(
                "finish_reason=%s" % safe_str(choice.get("finish_reason"))
            )

            message = choice.get("message")

            if isinstance(message, dict):
                parts.append(
                    "message_keys=%s" % list(message.keys())[:20]
                )

    usage = data.get("usage")

    if isinstance(usage, dict):
        parts.append(
            "usage=%s" % cut(safe_json(usage), 350)
        )

    return "；".join(parts)


def call_gpt(system_prompt, user_prompt):
    """
    生产版：
    - 仅调用 /v1/chat/completions；
    - 请求体严格保持 model + messages；
    - 不传 response_format；
    - 不传 max_tokens / max_completion_tokens；
    - 不做第二次请求或自动重试。
    """
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "Authorization": "Bearer " + GPT_API_KEY,
        "User-Agent": "Mozilla/5.0"
    }

    payload = {
        "model": GPT_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": user_prompt
            }
        ]
    }

    try:
        response = requests.post(
            GPT_API_URL,
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=GPT_TIMEOUT
        )
    except requests.exceptions.Timeout:
        raise Exception(
            "GPT Chat Completions 请求超时（%s秒），未进行重试。"
            % GPT_TIMEOUT
        )
    except Exception as error:
        raise Exception(
            "GPT Chat Completions 请求异常: " + safe_str(error)
        )

    if response.status_code >= 400:
        body = (response.content or b"").decode(
            "utf-8",
            errors="replace"
        )

        raise Exception(
            "GPT Chat Completions API错误: "
            + str(response.status_code)
            + " "
            + body[:800]
        )

    data, decode_mode = _response_json_utf8(response)
    shape = _response_shape(data)

    if not isinstance(data, dict):
        raise Exception(
            "GPT Chat Completions 响应不是对象。"
            + shape
        )

    api_error = data.get("error")

    if api_error:
        if isinstance(api_error, dict):
            api_error = (
                api_error.get("message")
                or api_error.get("detail")
                or safe_json(api_error)
            )

        raise Exception(
            "GPT Chat Completions 业务错误: "
            + cut(api_error, 800)
        )

    choices = data.get("choices", [])

    if not isinstance(choices, list) or not choices:
        raise Exception(
            "GPT Chat Completions 未返回 choices。"
            + shape
        )

    message = choices[0].get("message", {})

    if not isinstance(message, dict):
        raise Exception(
            "GPT Chat Completions 的 choices[0].message 不是对象。"
            + shape
        )

    content = _extract_text_value(message.get("content"))
    content = fix_mojibake_text(content)

    if not content:
        refusal = _extract_text_value(message.get("refusal"))

        if refusal:
            raise Exception(
                "GPT 拒绝返回正文: " + cut(refusal, 800)
            )

        raise Exception(
            "GPT Chat Completions 请求成功但未返回可见正文。"
            + shape
        )

    return content.strip()


# =========================
# 字段清洗
# =========================

def empty_director_plan():
    return {
        "director_type": "view_dir",
        "core": "",
        "tone": "",
        "emo": "",
        "goal": "",
        "open": "",
        "spine": "",
        "arc": [
            "hook", "proof", "turn",
            "why", "method", "end"
        ],
        "expression_domains": [
            "life", "work", "system"
        ],
        "variation_focus": [
            "space", "carrier", "viewpoint"
        ],
        "rule": ""
    }


def clean_pick(obj):
    if not isinstance(obj, dict):
        obj = {}

    main = cut(obj.get("dir"), 30)
    sub = cut(obj.get("sub"), 30)

    if main not in DIR_CARDS:
        main = "view_dir"

    if sub not in DIR_CARDS or sub == main:
        sub = ""

    hints = []

    for item in obj.get("hint", [])[:2]:
        item = cut(item, 40)

        if item:
            hints.append(item)

    return {
        "dir": main,
        "sub": sub,
        "why": cut(
            obj.get("why") or "根据文案表达方式选择导演。",
            80
        ),
        "hint": hints
    }


def clean_director_plan(obj, director_type):
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
        "arc": clean_code_list(
            data.get("arc"),
            RHYTHM_CODES,
            [
                "hook", "proof", "turn",
                "why", "method", "end"
            ],
            10
        ),
        "expression_domains": clean_code_list(
            data.get("expression_domains"),
            DOMAIN_CODES,
            ["life", "work", "system"],
            4
        ),
        "variation_focus": clean_code_list(
            data.get("variation_focus"),
            VARIATION_CODES,
            ["space", "carrier", "viewpoint"],
            4
        ),
        "rule": cut(
            data.get("rule")
            or "相邻段改变表达域、密度或观看角度，保持全文推进。",
            100
        )
    }


def is_strong_viewpoint(text):
    patterns = [
        r"真正的问题",
        r"关键是",
        r"核心是",
        r"本质是",
        r"结论是",
        r"不是.+而是",
        r"最重要的是",
        r"归根结底",
        r"真正会用",
        r"真正要"
    ]

    return any(re.search(pattern, text) for pattern in patterns)


def has_explicit_data(text):
    if re.search(r"\d+(?:\.\d+)?\s*[%％]", text):
        return True

    if len(set(re.findall(r"20\d{2}", text))) >= 2:
        return True

    if re.search(
        r"(增长|下降|提升|减少|增加|同比|环比).{0,12}\d+",
        text
    ):
        return True

    return False


def has_explicit_logic_chain(text):
    patterns = [
        r"→", r"->", r"⇒",
        r"流程", r"步骤", r"链路",
        r"因果链", r"机制链",
        r"第一步", r"第二步", r"第三步",
        r"先.+再.+"
    ]

    return any(re.search(pattern, text) for pattern in patterns)


def allow_logic(text):
    return (
        is_strong_viewpoint(text)
        or has_explicit_data(text)
        or has_explicit_logic_chain(text)
    )


def default_routes(need):
    if need == "mechanism":
        return ["symbol", "scene"]

    if need == "conclusion":
        return ["host", "scene"]

    if need in {"emotion", "contrast"}:
        return ["scene", "symbol"]

    return ["scene"]


def scene_first(text):
    patterns = [
        r"老板", r"领导", r"客户", r"同事",
        r"下班", r"手机", r"工作群",
        r"方案", r"PPT", r"任务", r"文件",
        r"审核", r"改稿", r"会议", r"工位",
        r"加活", r"加两个活"
    ]

    return any(re.search(pattern, text) for pattern in patterns)


def clean_routes(value, need, text):
    routes = clean_code_list(
        value,
        ROUTE_CODES,
        [],
        2
    )

    if not routes:
        routes = default_routes(need)

    if "logic" in routes and not allow_logic(text):
        routes = [
            item for item in routes
            if item != "logic"
        ]

    if not routes:
        routes = [
            item for item in default_routes(need)
            if item != "logic"
        ]

    if scene_first(text):
        if "scene" not in routes and need != "conclusion":
            routes = ["scene"] + routes[:1]
        elif "scene" in routes:
            routes = ["scene"] + [
                item for item in routes
                if item != "scene"
            ]

    result = []

    for item in routes:
        if item in ROUTE_CODES and item not in result:
            result.append(item)

    return result[:2] or ["scene"]


def normalize_relation(relation, rhythm):
    rhythm_map = {
        "hook": "new",
        "proof": "confirm",
        "fact": "add",
        "turn": "turn",
        "why": "why",
        "load": "why",
        "wrong": "wrong",
        "method": "method",
        "end": "end",
        "act": "act"
    }

    if rhythm in rhythm_map:
        return rhythm_map[rhythm]

    return relation if relation in RELATION_CODES else "move"


# =========================
# 原文 Unit 切分与重组
# =========================

def build_units(text):
    raw_parts = re.split(r"(?<=[。！？；\n])", text)

    units = []
    pending = ""

    for part in raw_parts:
        if not part:
            continue

        if not compact(part):
            pending += part
            continue

        units.append(pending + part)
        pending = ""

    if pending:
        if units:
            units[-1] += pending
        else:
            units.append(pending)

    result = []

    for index, unit_text in enumerate(units):
        if compact(unit_text):
            result.append({
                "id": "u%02d" % (index + 1),
                "text": unit_text
            })

    return result


def unit_prompt_text(units):
    return "\n".join(
        "%s=%s" % (
            item["id"],
            item["text"].replace("\n", "\\n")
        )
        for item in units
    )


def normalize_unit_groups(raw_segments, units):
    valid_ids = [item["id"] for item in units]
    valid_set = set(valid_ids)

    if not isinstance(raw_segments, list):
        raw_segments = []

    groups = []
    assigned = {}
    last_group_index = -1

    for raw in raw_segments[:MAX_SEGMENTS]:
        if not isinstance(raw, dict):
            continue

        raw_ids = raw.get("u", raw.get("units", []))

        if not isinstance(raw_ids, list):
            raw_ids = []

        group_ids = []

        for unit_id in raw_ids:
            unit_id = safe_str(unit_id).strip()

            if (
                unit_id in valid_set
                and unit_id not in assigned
            ):
                group_ids.append(unit_id)
                assigned[unit_id] = len(groups)

        if group_ids:
            groups.append({
                "raw": raw,
                "ids": group_ids
            })
            last_group_index = len(groups) - 1

    if not groups:
        return []

    for unit_id in valid_ids:
        if unit_id in assigned:
            continue

        unit_index = valid_ids.index(unit_id)

        target_group = None

        for group_index, group in enumerate(groups):
            first_index = valid_ids.index(group["ids"][0])

            if unit_index < first_index:
                target_group = group_index
                break

        if target_group is None:
            target_group = last_group_index

        groups[target_group]["ids"].append(unit_id)
        assigned[unit_id] = target_group

    ordered_groups = []

    for group in groups:
        ids = sorted(
            group["ids"],
            key=lambda item: valid_ids.index(item)
        )

        if ids:
            ordered_groups.append({
                "raw": group["raw"],
                "ids": ids
            })

    return ordered_groups


def unit_text_by_ids(ids, unit_map):
    return "".join(
        unit_map.get(unit_id, "")
        for unit_id in ids
    )


def build_segment_beats(raw_obj, text, units):
    raw_segments = raw_obj.get(
        "seg",
        raw_obj.get("segment_beats", [])
    ) if isinstance(raw_obj, dict) else []

    unit_map = {
        item["id"]: item["text"]
        for item in units
    }

    groups = normalize_unit_groups(
        raw_segments,
        units
    )

    if not groups:
        return []

    result = []

    for index, group in enumerate(groups):
        raw = group["raw"]
        segment_text = unit_text_by_ids(
            group["ids"],
            unit_map
        )

        if not compact(segment_text):
            continue

        rhythm = cut(
            raw.get("r") or raw.get("rhythm") or "move",
            20
        )

        if rhythm not in RHYTHM_CODES:
            rhythm = "move"

        relation = cut(
            raw.get("l") or raw.get("relation") or "move",
            20
        )

        need = cut(
            raw.get("n")
            or raw.get("expression_need")
            or "reality",
            20
        )

        if need not in NEED_CODES:
            need = "reality"

        routes = raw.get(
            "v",
            raw.get(
                "route_candidates",
                raw.get("routes", [])
            )
        )

        result.append({
            "segment_index": len(result),
            "segment_text": segment_text,
            "rhythm": rhythm,
            "segment_goal": cut(
                raw.get("g")
                or raw.get("segment_goal")
                or "推进当前段理解。",
                60
            ),
            "beats": [{
                "relation": normalize_relation(
                    relation,
                    rhythm
                ),
                "expression_need": need,
                "route_candidates": clean_routes(
                    routes,
                    need,
                    segment_text
                )
            }]
        })

    return result


def segment_texts_cover_full_text(full_text, segments):
    combined = "".join(
        item.get("segment_text", "")
        for item in segments
    )

    return compact(full_text) == compact(combined)


def build_segments_compat(segment_beats):
    return [
        safe_str(item.get("segment_text", ""))
        for item in segment_beats
        if safe_str(item.get("segment_text", "")).strip()
    ]


def actual_rhythm_list(segment_beats):
    result = []

    for item in segment_beats:
        rhythm = item.get("rhythm", "")

        if rhythm in RHYTHM_CODES and rhythm not in result:
            result.append(rhythm)

    return result


# =========================
# Mini：只做导演路由
# =========================

MINI_PICK_SYS = """
你是短视频导演路由器。

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
}
""".strip()


# =========================
# GPT：用 Unit ID 做导演与分段
# =========================

def build_director_and_segment_sys(pick):
    return """
你是短视频总导演兼分段导演。

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
        pick["why"]
    )


# =========================
# 输出
# =========================


def ensure_director_plan_ready(plan):
    required_fields = ("core", "tone", "emo", "goal", "open", "spine")
    missing = [key for key in required_fields if not safe_str(plan.get(key)).strip()]

    if missing:
        raise RuntimeError(
            "GPT 导演策略字段不完整：" + ", ".join(missing)
        )


def handler(args):
    """
    成功时仅返回业务结果。
    任意模型空回复、JSON 无效、分段不完整或请求异常均直接抛错，
    由 Coze 节点自己的失败重试机制处理。
    """
    text = safe_str(read_text(args)).strip()

    if not text:
        raise ValueError("text 为空，请检查入参是否传入 text。")

    if not ARK_API_KEY or ARK_API_KEY == "YOUR_ARK_API_KEY":
        raise RuntimeError("ARK_API_KEY 未配置。")

    if not GPT_API_KEY or GPT_API_KEY == "YOUR_GPT_API_KEY":
        raise RuntimeError("GPT_API_KEY 未配置。")

    units = build_units(text)

    if not units:
        raise RuntimeError("原文无法切分为有效 Unit。")

    pick_raw = call_ark(
        MINI_PICK_SYS,
        "完整文案：\n" + text
    )

    pick_obj = parse_json_obj(pick_raw)

    if not pick_obj:
        raise RuntimeError("Mini 路由模型未返回有效 JSON。")

    pick = clean_pick(pick_obj)

    gpt_raw = call_gpt(
        build_director_and_segment_sys(pick),
        "原文Unit列表：\n" + unit_prompt_text(units)
    )

    if not safe_str(gpt_raw).strip():
        raise RuntimeError("GPT 未返回正文。")

    gpt_obj = parse_json_obj(gpt_raw)

    if not gpt_obj:
        raise RuntimeError("GPT 未返回可解析的 JSON 对象。")

    if not isinstance(gpt_obj.get("d"), dict):
        raise RuntimeError("GPT 输出缺少 d 导演策略对象。")

    if not isinstance(gpt_obj.get("seg"), list):
        raise RuntimeError("GPT 输出缺少 seg 分段数组。")

    director_plan = clean_director_plan(
        gpt_obj,
        pick["dir"]
    )
    ensure_director_plan_ready(director_plan)

    segment_beats = build_segment_beats(
        gpt_obj,
        text,
        units
    )

    if not segment_beats:
        raise RuntimeError("GPT 未返回有效 Unit 分组。")

    segment_count = len(segment_beats)

    if segment_count < MIN_SEGMENTS or segment_count > MAX_SEGMENTS:
        raise RuntimeError(
            "GPT 分段数量不符合要求：%s，要求 %s-%s 段。"
            % (segment_count, MIN_SEGMENTS, MAX_SEGMENTS)
        )

    if not segment_texts_cover_full_text(text, segment_beats):
        raise RuntimeError("Unit 分组重建后未完整覆盖原文。")

    segments = build_segments_compat(segment_beats)

    if len(segments) != segment_count:
        raise RuntimeError("segments 与 segment_beats 数量不一致。")

    return {
        "ok": True,
        "director_plan": director_plan,
        "segment_beats": segment_beats,
        "segments": segments
    }

