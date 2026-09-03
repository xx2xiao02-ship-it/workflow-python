import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from runtime import Args
from typings.Shot_Visual_Arrangement.Shot_Visual_Arrangement import Input, Output


# 依赖包：requests
#
# 输入：
# ref_image：Array<String>，参考图链接数组。
# character_anchor：String，可选；用于跨镜头人物连续性。
# debug：Boolean，可选；true 时返回分阶段耗时与 GPT 压缩前后的字符统计。
# 未传 character_anchor 时，默认使用“同一位年轻职场人”。
# 代码同时兼容 ref_images 作为旧字段别名。
#
# 输出：
# ref_image：Array<Object>，与 prompt 按下标一一对齐。
# 每一项格式：{"ref_image": [输入的全部参考图链接]}
# motion_seed：Array<String>，与 prompt 按下标一一对应；用于后续视频提示词工具。
# debug：Object；仅 debug=true 时填入 total_ms / gpt_ms / mini_ms 等耗时信息。

# =========================
# 鉴权与模型配置
# =========================

ARK_API_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
ARK_API_KEY = '[REDACTED]'
MINI_MODEL = "ep-20260609123759-6sv2j"

# ========== 以下为 DeepSeek V4 配置（已替换原第三方 GPT） ==========
GPT_API_URL = "https://api.deepseek.com/chat/completions"
GPT_API_KEY = '[REDACTED]'
GPT_MODEL = "deepseek-v4-pro"
# 显式关闭思考模式，保证纯结果输出、格式稳定、速度更快
GPT_THINKING = {"type": "disabled"}

# 超时只影响异常等待，不会限制正常生成时长。
GPT_TIMEOUT = 100
MINI_TIMEOUT = 60

# 将 mini 按原始段落并行调用。可按方舟并发额度调整为 2~4。
MINI_PARALLEL_WORKERS = 4

# 失败时只快速重试一次，避免单次异常拖慢整条工作流。
RETRIES = 2
RETRY_DELAY_SECONDS = 0.6

# 第一轮 GPT 仅接收压缩后的全局语义，减少请求体与输出体积。
MAX_GPT_GROUP_CONTEXT_CHARS = 96
MAX_GPT_SHOT_TEXT_CHARS = 72


# =========================
# 固定约束
# =========================

SHOT_SIZES = [
    "超远景", "远景", "全景", "中景",
    "近景", "特写", "极特写"
]

CARRIER_MODES = [
    "完整人物", "人物局部", "物件", "空间", "建筑环境"
]

COMPOSITIONS = [
    "纵向主体—物件关系",
    "边缘压力切入",
    "斜向纵深",
    "遮挡或框景",
    "多人层级",
    "局部焦点与留白"
]

REF_STYLE_GUIDE = (
    "参考图仅承接画风、色彩、光影、线条、材质与人物气质。"
)

DEFAULT_CHARACTER_ANCHOR = "同一位年轻职场人"


# =========================
# 第一轮：全局编排
# =========================

GPT_SYSTEM_PROMPT = r"""
# 全局镜头编排

看完整序列后，统一为每条镜头确定：场景、景别、承载、视角、构图、视觉重点。不要逐条孤立决定。

场景：工作不等于工位。会议/协作可用办公区、会议室、看板、走廊、电梯厅；系统压力可用楼宇、通道、空间环境；下班/回家/吃饭/深夜优先通勤和私人生活空间。
景别：超远景=城市/楼宇/系统压力；远景=环境距离/通勤；全景=人物与空间；中景=人物与任务关系；近景=停顿/疲惫；特写=提醒/文件/手部；极特写=即时压力点。
三条及以上：至少两种景别，至少一条非完整人物承载；不得连续三条同景别；不得全部完整人物。
同场景连续镜头：景别、承载、视角、构图至少变化一项。

构图必须落实：
多人层级=至少两人且前后景职责明确；
边缘压力切入=压力从边缘压入；
斜向纵深=通道、桌面、看板或空间延伸；
遮挡或框景=门框、肩背、物件边缘等遮挡；
纵向主体—物件关系=人物与关键物件有前后或上下关系；
局部焦点与留白=局部动作、物件或表情成为中心。

镜头语法：
完整人物多用全景/中景；
人物局部多用近景/特写；
物件多用近景/特写；
空间或建筑多用远景/全景；
提醒、打断、侵入、追加、堆积优先边缘压力切入、遮挡或局部焦点；
楼宇、走廊、电梯、地铁、通道优先斜向纵深。

输入使用压缩字段：
q = 镜头组数组；
每组：g=组号，c=该段上下文，s=[[镜头id,镜头事件]...]。

只输出紧凑 JSON，禁止解释：
{
  "p": [
    ["镜头id","时间+具体场景","景别","承载方式","视角短语","构图","视觉重点短句"]
  ]
}

景别只能是：超远景、远景、全景、中景、近景、特写、极特写。
承载只能是：完整人物、人物局部、物件、空间、建筑环境。
构图只能是：纵向主体—物件关系、边缘压力切入、斜向纵深、遮挡或框景、多人层级、局部焦点与留白。
不得改写、删除、合并、重排镜头；不得输出 prompt、光影、时长、时间线。
"""

# =========================
# 第二轮：mini 补画面原始资料
# =========================

MINI_SYSTEM_PROMPT = r"""
# 首帧与动态种子资料补全器

当前输入是一小组已锁定镜头。只为每条补五项：
subject_action：首帧可见动作/姿势/物件状态；
space_layers：前中后景，或局部前后关系；
key_objects：1~3个具体可见对象；
lighting_mood：具体光源、明暗关系和情绪；
motion_seed：从首帧自然向后发展的单句动态种子。

规则：
- subject_action 只写可直接画出的事实：松开键盘、手停在半空、手机亮起、任务卡覆盖排期等。
- 禁止“呈现压力、处于停顿状态、提醒物件、关键物件”等抽象/占位词。
- 提醒/消息/待办用手机、电脑屏幕、任务卡片、文件、餐具旁亮起的设备等自然载体。
- key_objects 只写真实对象，不写“完整人物、压力来源、工作状态”。
- 不写品牌、可读文字、复杂 UI、字幕；不改写事件；不重复景别、视角、构图、承载方式。
- space_layers 必须服务给定构图。lighting_mood 写清环境光与局部亮点关系。
- motion_seed 只描述首帧后的轻微、连续、可执行动态：状态延续 → 轻微变化 → 停住或落点。
- motion_seed 不写镜头切换、推拉摇移、转场、换场、换人、增加关键物件、剧情跳跃或新的结果。
- 每个字段控制在 12~42 个汉字内。

只输出 JSON：
{
  "materials": [
    {
      "shot_id": "与输入一致",
      "subject_action": "中文短句",
      "space_layers": "中文短句",
      "key_objects": "中文短句",
      "lighting_mood": "中文短句",
      "motion_seed": "中文单句"
    }
  ]
}
"""


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
                    "motion_seed": {"type": "string"}
                },
                "required": [
                    "shot_id",
                    "subject_action",
                    "space_layers",
                    "key_objects",
                    "lighting_mood",
                    "motion_seed"
                ]
            }
        }
    },
    "required": ["materials"]
}


# =========================
# 基础工具
# =========================

def text(value):
    return "" if value is None else str(value).strip()


def compact_gpt_text(value, limit):
    value = re.sub(r"\s+", "", text(value))
    return value if len(value) <= limit else value[:limit] + "…"


def plain(value):
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


def decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value.strip())
        except Exception:
            return value
    return value


def obj(value):
    value = decode(plain(value))
    return value if isinstance(value, dict) else {}


def arr(value):
    value = decode(plain(value))
    return value if isinstance(value, list) else []


def field(data, key, default=None):
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def integer(value):
    try:
        number = int(float(value))
        return number if number > 0 else None
    except Exception:
        return None


def as_bool(value):
    if isinstance(value, bool):
        return value
    return text(value).lower() in ("1", "true", "yes", "on", "是")


def timeline(value):
    value = obj(value)
    try:
        start = int(float(value.get("start")))
        end = int(float(value.get("end")))
        return {"start": start, "end": end} if end > start else None
    except Exception:
        return None


def refs(value):
    values = arr(value)
    if not values and value:
        values = [value]

    result = []
    seen = set()

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


def json_value(value):
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


def fail(message, debug_info=None):
    return {
        "prompt": [],
        "ref_image": [],
        "motion_seed": [],
        "timelines": [],
        "int_duration": [],
        "error": text(message),
        "debug": debug_info or {}
    }


# =========================
# 网络调用
# =========================

def request_json(url, headers, payload, timeout):
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=timeout
        )
    except requests.exceptions.Timeout:
        return {"ok": False, "status": 408, "error": "请求超时"}
    except Exception as error:
        return {"ok": False, "status": 500, "error": str(error)}

    if not 200 <= response.status_code < 300:
        return {
            "ok": False,
            "status": response.status_code,
            "error": response.text[:800]
        }

    try:
        return {"ok": True, "data": response.json()}
    except Exception as error:
        return {"ok": False, "status": 500, "error": f"JSON 解析失败：{error}"}


def post_retry(url, headers, payload, timeout):
    last = None
    delay = RETRY_DELAY_SECONDS

    for _ in range(RETRIES):
        result = request_json(url, headers, payload, timeout)

        if result.get("ok"):
            return result

        last = result

        if result.get("status") not in (408, 429, 500, 502, 503, 504):
            break

        time.sleep(delay)
        delay *= 2

    return last or {"ok": False, "error": "未知请求失败"}


def chat_text(data):
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


def response_text(data):
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


# =========================
# 输入拆解
# =========================

def build_sequence(raw):
    segments = arr(field(raw, "segments", []))
    code_list = arr(field(raw, "Code_list", []))

    if not segments or not code_list:
        return None, "segments 与 Code_list 均不能为空。"

    if len(segments) != len(code_list):
        return None, "segments 与 Code_list 长度不一致。"

    # GPT 第一轮只保留：组级上下文 + 每条 source_text。
    # story_beat / clip_role 保留在内部，供 mini 生成首帧资料使用。
    gpt_groups = []
    flat = []
    groups = []

    for group_index, segment in enumerate(segments):
        segment = text(segment)
        code_group = obj(code_list[group_index])
        shots = arr(code_group.get("shots"))
        durations = arr(code_group.get("int_duration"))
        timelines = arr(code_group.get("timelines"))

        if not segment or not shots:
            return None, f"第 {group_index + 1} 组 segments 或 shots 为空。"

        if len(shots) != len(durations) or len(shots) != len(timelines):
            return None, (
                f"第 {group_index + 1} 组 shots、int_duration、timelines 长度不一致。"
            )

        gpt_shots = []
        group_records = []

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

            # 第一轮仅保留最短可用镜头事件，避免 story_beat 重复占用上下文。
            gpt_shots.append([
                shot_id,
                compact_gpt_text(source_text, MAX_GPT_SHOT_TEXT_CHARS)
            ])

            record = {
                "shot_id": shot_id,
                "source_text": source_text,
                "clip_role": clip_role,
                "story_beat": story_beat,
                "int_duration": int_duration,
                "timeline": shot_timeline
            }

            group_records.append(record)
            flat.append(record)

        gpt_groups.append({
            "g": f"g{group_index + 1:02d}",
            "c": compact_gpt_text(
                segment,
                MAX_GPT_GROUP_CONTEXT_CHARS
            ),
            "s": gpt_shots
        })

        groups.append(group_records)

    return {
        "gpt_groups": gpt_groups,
        "flat": flat,
        "groups": groups
    }, ""


# =========================
# DeepSeek V4：全局视觉规划
# =========================

def gpt_plan(groups):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GPT_API_KEY}"
    }

    # separators 去除无意义空格，进一步压缩网络请求体。
    user_content = json.dumps(
        {"q": groups},
        ensure_ascii=False,
        separators=(",", ":")
    )

    payload = {
        "model": GPT_MODEL,
        "thinking": GPT_THINKING,
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": GPT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ]
    }

    result = post_retry(GPT_API_URL, headers, payload, GPT_TIMEOUT)

    if not result.get("ok"):
        # 兼容不接受 response_format / temperature 的网关。
        fallback_payload = dict(payload)
        fallback_payload.pop("response_format", None)
        fallback_payload.pop("temperature", None)
        result = post_retry(
            GPT_API_URL,
            headers,
            fallback_payload,
            GPT_TIMEOUT
        )

    if not result.get("ok"):
        return None, f"DeepSeek V4 调用失败：{result.get('error', '')}", {}

    output_content = chat_text(result.get("data"))
    data = json_value(output_content)

    stats = {
        "gpt_input_chars": len(user_content),
        "gpt_system_chars": len(GPT_SYSTEM_PROMPT),
        "gpt_output_chars": len(output_content),
        "gpt_group_count": len(groups),
        "gpt_shot_count": sum(
            len(item.get("s", []))
            for item in groups
        )
    }

    if not isinstance(data, dict):
        return None, "DeepSeek V4 未返回 JSON。", stats

    return data, "", stats


def validate_plan(data, records):
    payload = obj(data)
    values = arr(payload.get("p"))

    # 兼容旧格式，便于必要时回退测试。
    if not values:
        values = arr(payload.get("plans"))

    ids = [record["shot_id"] for record in records]

    if len(values) != len(ids):
        return None, "DeepSeek V4 输出镜头数不一致。"

    mapping = {}

    for raw in values:
        if isinstance(raw, list):
            if len(raw) != 7:
                return None, "DeepSeek V4 紧凑输出字段数不正确。"

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

        plan = {
            "shot_id": shot_id,
            "scene_context": scene_context,
            "shot_size": shot_size,
            "carrier_mode": carrier_mode,
            "viewpoint": viewpoint,
            "composition": composition,
            "visual_focus": visual_focus
        }

        if not shot_id or shot_id in mapping:
            return None, "DeepSeek V4 输出存在空或重复 shot_id。"

        if not scene_context or not viewpoint or not visual_focus:
            return None, f"{shot_id} 缺少必要规划字段。"

        if shot_size not in SHOT_SIZES:
            return None, f"{shot_id}.shot_size 不合法。"

        if carrier_mode not in CARRIER_MODES:
            return None, f"{shot_id}.carrier_mode 不合法。"

        if composition not in COMPOSITIONS:
            return None, f"{shot_id}.composition 不合法。"

        mapping[shot_id] = plan

    if set(mapping) != set(ids):
        return None, "DeepSeek V4 输出的 shot_id 与输入不一致。"

    ordered = [mapping[shot_id] for shot_id in ids]

    if len(ordered) >= 3:
        sizes = [plan["shot_size"] for plan in ordered]

        if len(set(sizes)) < 2:
            return None, "全局规划只使用了一种景别。"

        if all(plan["carrier_mode"] == "完整人物" for plan in ordered):
            return None, "全局规划全部使用完整人物承载。"

        for index in range(len(sizes) - 2):
            if sizes[index] == sizes[index + 1] == sizes[index + 2]:
                return None, "存在连续三条相同景别。"

    return ordered, ""


# =========================
# mini：首帧原始资料
# =========================

def mini_materials_batch(records, plans):
    sequence = []

    for record, plan in zip(records, plans):
        sequence.append({
            "shot_id": record["shot_id"],
            "source_text": record["source_text"],
            "story_beat": record["story_beat"],
            "scene_context": plan["scene_context"],
            "shot_size": plan["shot_size"],
            "carrier_mode": plan["carrier_mode"],
            "viewpoint": plan["viewpoint"],
            "composition": plan["composition"],
            "visual_focus": plan["visual_focus"]
        })

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ARK_API_KEY}"
    }

    payload = {
        "model": MINI_MODEL,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": MINI_SYSTEM_PROMPT}]
            },
            {
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": json.dumps({"sequence": sequence}, ensure_ascii=False)
                }]
            }
        ],
        "thinking": {"type": "disabled"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "frame_materials",
                "strict": True,
                "schema": MINI_SCHEMA
            }
        }
    }

    result = post_retry(ARK_API_URL, headers, payload, MINI_TIMEOUT)

    if not result.get("ok"):
        # 仅在结构化输出不兼容时降级一次，避免重复长请求。
        payload.pop("thinking", None)
        payload.pop("text", None)
        result = post_retry(ARK_API_URL, headers, payload, MINI_TIMEOUT)

    if not result.get("ok"):
        return None, f"豆包 2.0 mini 调用失败：{result.get('error', '')}"

    data = json_value(response_text(result.get("data")))
    return (data, "") if isinstance(data, dict) else (None, "豆包 2.0 mini 未返回 JSON。")


def mini_materials_parallel(group_records, plan_map):
    batches = []

    for records in group_records:
        plans = [plan_map[item["shot_id"]] for item in records]
        batches.append((records, plans))

    result_map = {}
    fallback_batches = 0
    success_batches = 0

    if not batches:
        return [], {
            "mini_batches": 0,
            "mini_success_batches": 0,
            "mini_fallback_batches": 0
        }

    worker_count = min(MINI_PARALLEL_WORKERS, len(batches))

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(mini_materials_batch, records, plans): (records, plans)
            for records, plans in batches
        }

        for future in as_completed(futures):
            records, plans = futures[future]

            try:
                data, error = future.result()
            except Exception as exc:
                data, error = None, f"mini 并行任务异常：{exc}"

            if error:
                rows = [
                    {"shot_id": record["shot_id"], **fallback_material(record, plan)}
                    for record, plan in zip(records, plans)
                ]
                fallback_batches += 1
            else:
                rows = normalize_materials(data, records, plans)
                success_batches += 1

            for row in rows:
                result_map[row["shot_id"]] = row

    ordered = []

    for records, _ in batches:
        for record in records:
            ordered.append(result_map[record["shot_id"]])

    return ordered, {
        "mini_batches": len(batches),
        "mini_success_batches": success_batches,
        "mini_fallback_batches": fallback_batches,
        "mini_parallel_workers": worker_count
    }





def fallback_motion_seed(record, plan):
    focus = text(plan.get("visual_focus"))

    if plan.get("carrier_mode") == "物件":
        return (
            f"关键物件保持首帧状态并轻微变化，"
            f"人物视线或手部随之停住，最后落在{focus}。"
        )

    if plan.get("carrier_mode") in ("空间", "建筑环境"):
        return (
            f"空间关系保持稳定，人物或环境出现轻微持续变化，"
            f"最后停在{focus}的余波中。"
        )

    return (
        f"人物保持首帧姿势，完成一个轻微动作变化，"
        f"最后停在{focus}的状态。"
    )


def fallback_material(record, plan):
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

    return {
        "subject_action": action,
        "space_layers": layers,
        "key_objects": f"与“{focus}”相关的关键物件和环境信息支点",
        "lighting_mood": "环境自然光与局部功能光共同塑造克制而真实的情绪",
        "motion_seed": fallback_motion_seed(record, plan)
    }


def normalize_materials(data, records, plans):
    raw_values = arr(obj(data).get("materials"))
    raw_map = {}

    for raw in raw_values:
        item = obj(raw)
        shot_id = text(item.get("shot_id"))
        if shot_id:
            raw_map[shot_id] = {
                "subject_action": text(item.get("subject_action")),
                "space_layers": text(item.get("space_layers")),
                "key_objects": text(item.get("key_objects")),
                "lighting_mood": text(item.get("lighting_mood")),
                "motion_seed": text(item.get("motion_seed"))
            }

    result = []

    for record, plan in zip(records, plans):
        base = fallback_material(record, plan)
        current = raw_map.get(record["shot_id"], {})

        for key in base:
            if not current.get(key):
                current[key] = base[key]

        result.append({
            "shot_id": record["shot_id"],
            **current
        })

    return result


# =========================
# 代码拼 prompt + 扁平化输出
# =========================

def screen_or_text_guard(source_text, story_beat, plan, material):
    combined = " ".join([
        text(source_text),
        text(story_beat),
        text(plan.get("visual_focus")),
        text(material.get("subject_action")),
        text(material.get("key_objects"))
    ])

    screen_words = [
        "手机", "屏幕", "电脑", "弹窗", "消息",
        "提醒", "待办", "界面", "通知"
    ]

    card_words = [
        "任务卡", "看板", "文件", "排期",
        "工单", "表格", "卡片"
    ]

    if plan.get("carrier_mode") == "物件" and any(word in combined for word in screen_words):
        return (
            "屏幕仅表现亮起、弹窗或提醒状态，"
            "不要求具体可读文字、复杂界面或品牌标识。"
        )

    if any(word in combined for word in card_words):
        return (
            "任务卡片、看板或文件仅表现排期、堆叠或状态变化，"
            "不要求具体可读文字。"
        )

    return ""




def is_person_carrier(plan):
    return plan.get("carrier_mode") in ["完整人物", "人物局部"]


def build_prompt(
    plan,
    material,
    has_refs,
    character_anchor,
    source_text,
    story_beat
):
    prefix = f"{REF_STYLE_GUIDE}\n\n" if has_refs else ""

    anchor = ""
    if is_person_carrier(plan) and character_anchor:
        anchor = f"{character_anchor}，"

    guard = screen_or_text_guard(
        source_text,
        story_beat,
        plan,
        material
    )

    shot_size = plan["shot_size"]
    carrier = plan["carrier_mode"]

    # 模板 A：物件触发与局部压力
    if carrier == "物件" or shot_size in ["特写", "极特写"]:
        body = (
            f"首帧画面：9:16竖版，{plan['scene_context']}。"
            f"{anchor}{material['subject_action']}，"
            f"画面焦点直接落在{plan['visual_focus']}。"
            f"{material['space_layers']}。"
            f"{material['key_objects']}构成关键的信息支点。"
            f"{shot_size}景别，以{carrier}为主要承载，"
            f"{plan['viewpoint']}，{plan['composition']}构图。"
            f"{material['lighting_mood']}。"
        )

    # 模板 B：空间建立、场景转场与环境压力
    elif carrier in ["空间", "建筑环境"] or shot_size in ["超远景", "远景", "全景"]:
        body = (
            f"首帧画面：9:16竖版，{plan['scene_context']}。"
            f"{material['space_layers']}。"
            f"{anchor}{material['subject_action']}，"
            f"使{plan['visual_focus']}成为画面核心。"
            f"{material['key_objects']}构成关键的信息支点。"
            f"{shot_size}景别，以{carrier}为主要承载，"
            f"{plan['viewpoint']}，{plan['composition']}构图。"
            f"{material['lighting_mood']}。"
        )

    # 模板 C：人物状态、关系推进与情绪停顿
    else:
        body = (
            f"首帧画面：9:16竖版，{plan['scene_context']}。"
            f"{anchor}{material['subject_action']}，"
            f"画面焦点落在{plan['visual_focus']}。"
            f"{material['space_layers']}。"
            f"{material['key_objects']}构成关键的信息支点。"
            f"{shot_size}景别，以{carrier}为主要承载，"
            f"{plan['viewpoint']}，{plan['composition']}构图。"
            f"{material['lighting_mood']}。"
        )

    return f"{prefix}{body}{guard}"


def handler(args: Args[Input]) -> Output:
    started_at = time.perf_counter()
    debug_enabled = False
    debug_info = {}

    try:
        raw = plain(getattr(args, "input", None))

        if isinstance(raw, dict) and isinstance(raw.get("_input"), dict):
            raw = raw["_input"]

        debug_enabled = as_bool(field(raw, "debug", False))

        def mark(name, stage_start):
            if debug_enabled:
                debug_info[name] = round(
                    (time.perf_counter() - stage_start) * 1000,
                    1
                )

        stage_start = time.perf_counter()
        sequence, build_error = build_sequence(raw)
        mark("build_ms", stage_start)

        if build_error:
            if debug_enabled:
                debug_info["total_ms"] = round(
                    (time.perf_counter() - started_at) * 1000,
                    1
                )
            return fail(build_error, debug_info if debug_enabled else None)

        stage_start = time.perf_counter()
        plan_data, gpt_error, gpt_stats = gpt_plan(sequence["gpt_groups"])
        mark("gpt_ms", stage_start)

        if debug_enabled:
            debug_info.update(gpt_stats)

        if gpt_error:
            if debug_enabled:
                debug_info["total_ms"] = round(
                    (time.perf_counter() - started_at) * 1000,
                    1
                )
            return fail(gpt_error, debug_info if debug_enabled else None)

        stage_start = time.perf_counter()
        plans, plan_error = validate_plan(plan_data, sequence["flat"])
        mark("plan_validate_ms", stage_start)

        if plan_error:
            if debug_enabled:
                debug_info["total_ms"] = round(
                    (time.perf_counter() - started_at) * 1000,
                    1
                )
            return fail(
                f"DeepSeek V4 全局规划校验失败：{plan_error}",
                debug_info if debug_enabled else None
            )

        plan_map = {item["shot_id"]: item for item in plans}

        stage_start = time.perf_counter()
        materials, mini_stats = mini_materials_parallel(
            sequence["groups"],
            plan_map
        )
        mark("mini_ms", stage_start)

        stage_start = time.perf_counter()
        material_map = {item["shot_id"]: item for item in materials}

        raw_refs = field(raw, "ref_image", None)
        if raw_refs is None:
            raw_refs = field(raw, "ref_images", [])
        ref_images = refs(raw_refs)

        character_anchor = text(
            field(raw, "character_anchor", DEFAULT_CHARACTER_ANCHOR)
        ) or DEFAULT_CHARACTER_ANCHOR

        prompts = []
        output_refs = []
        motion_seeds = []
        timelines = []
        durations = []

        for record in sequence["flat"]:
            shot_id = record["shot_id"]

            prompts.append(
                build_prompt(
                    plan=plan_map[shot_id],
                    material=material_map[shot_id],
                    has_refs=bool(ref_images),
                    character_anchor=character_anchor,
                    source_text=record["source_text"],
                    story_beat=record["story_beat"]
                )
            )

            output_refs.append({
                "ref_image": list(ref_images)
            })

            motion_seeds.append(
                text(material_map[shot_id].get("motion_seed"))
                or fallback_motion_seed(record, plan_map[shot_id])
            )

            timelines.append(record["timeline"])
            durations.append(record["int_duration"])

        mark("assemble_ms", stage_start)

        if debug_enabled:
            debug_info.update(mini_stats)
            debug_info["prompt_count"] = len(prompts)
            debug_info["motion_seed_count"] = len(motion_seeds)
            debug_info["total_ms"] = round(
                (time.perf_counter() - started_at) * 1000,
                1
            )

        return {
            "prompt": prompts,
            "ref_image": output_refs,
            "motion_seed": motion_seeds,
            "timelines": timelines,
            "int_duration": durations,
            "error": "",
            "debug": debug_info if debug_enabled else {}
        }

    except Exception as error:
        if debug_enabled:
            debug_info["total_ms"] = round(
                (time.perf_counter() - started_at) * 1000,
                1
            )
        return fail(
            f"插件执行异常：{error}",
            debug_info if debug_enabled else None
        )

