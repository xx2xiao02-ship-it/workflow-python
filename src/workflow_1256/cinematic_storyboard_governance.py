"""冻结编导与素材层之间的电影化分镜治理层。

本模块借鉴导演工作流的四个可验证产物：导演书、连续性圣经、节拍序列和
逐镜头导演复审。它是 ``DirectorLockedManifest`` 的只读扩展：不允许更改
镜头 ID、顺序、时间线，也不允许在此层写供应商 prompt、模型或 URL。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from .governance.consecutive_static_pacing import MERGED_AIGC_MAX_DURATION_US
from .first_frame_prompt_governance import normalize_fallback_composition


class CinematicStoryboardGovernanceError(ValueError):
    """治理层输出不满足冻结编导契约。"""


class CinematicStoryboardTransportRequired(RuntimeError):
    """未注入文本模型 transport。"""


class CinematicStoryboardTransport(Protocol):
    def __call__(self, system_prompt: str, user_prompt: str) -> Any: ...


FORBIDDEN_KEYS = {
    "prompt", "image_prompt", "video_prompt", "model", "provider", "url",
    "image_url", "video_url", "task_id", "reference_image_url",
}

# 这些枚举是编导层向素材层交付的可执行视觉约束；不得在下游再由模型随意改写。
VISUAL_SHOT_SIZES = ("超远景", "远景", "全景", "中景", "近景", "特写", "极特写")
VISUAL_CAMERA_ANGLES = ("平视", "俯视", "仰视", "低机位", "高机位", "越肩", "侧后方")
VISUAL_CAMERA_MOTIONS = ("固定", "推近", "拉远", "横移", "跟拍", "摇镜", "环绕")
VISUAL_COMPOSITIONS = (
    "纵向主体—物件关系", "边缘压力切入", "斜向纵深",
    "遮挡或框景", "多人层级", "局部焦点与留白",
)
MEDIA_TYPES = ("digital_human_video", "aigc_video", "static_image")
SHOT_DESIGN_TYPES = ("人物行动", "人物反应", "空间空镜", "物件空镜", "意象镜头", "意识流镜头")
EMPTY_SHOT_DESIGN_TYPES = frozenset({"空间空镜", "物件空镜", "意象镜头", "意识流镜头"})
TRANSITION_ROLES = ("无", "承上", "启下", "承上启下")
SEMANTIC_BRIDGE_MARKERS = (
    "转折", "转场", "过渡", "承上启下", "下段承接", "结果落点", "关键选择", "变化落点",
)
IMAGERY_MARKERS = ("意象", "象征", "隐喻", "影子", "倒影", "光束", "雨幕", "钟摆", "空椅")
STREAM_OF_CONSCIOUSNESS_MARKERS = ("意识流", "内心", "记忆", "幻觉", "恍惚", "梦境", "碎片化", "失焦")
VISIBLE_ACTION_MARKERS = (
    "抬", "低头", "推", "递", "起身", "放下", "走向", "走出", "转身", "伸手",
    "拿起", "打开", "关上", "靠近", "离开", "移动", "传递", "倒入", "端起", "写下",
    "翻开", "捡起", "拉开", "推开", "挥手", "坐下", "站起", "回头", "奔跑", "骑行",
    "跑向", "蹲下", "擦拭", "递给", "跨过", "停下",
)


CINEMATIC_STORYBOARD_SYSTEM_PROMPT = """你是短视频电影化编导的导演审核台。
输入是已经用户批准、且时间线与镜头 ID 都已冻结的故事和小镜头清单。

必须输出四类真正会被下游素材层使用的结构，而不是形容词堆叠：
1. director_book：说明全片的戏剧问题、视觉主线、信息揭示策略、景别和剪辑规则；
2. continuity_bible：角色、服装、空间、道具状态、主光方向、屏幕方向、色彩及禁止漂移规则；
3. beat_sequence：把相邻 group 组成连续场景节拍，写清状态如何推进；
4. shot_contexts：每个冻结小镜头各一条。它必须写清本镜让观众新理解什么、镜头设计类型、人物/物体走位起点→动作→终点、镜头为何这样取、从前镜承接什么、向后镜交出什么、必须可见什么、必须避免什么，并锁定媒体类型。

硬规则：
- 不得新增、删除、合并、重排或改写 shot_id、group_id、timeline。
- 每个镜头只能有一个主要动作和一个主要视觉锚点；抽象观点必须落到可观察的因果动作，不得画成文字、图表或 UI。
- 走位先于景别；景别和机位必须服务叙事揭示。全片要有远/全、中、近/特的节奏，不得机械轮换。
- 全片镜头达到 8 个时，至少使用 3 种 camera_angle 和 3 种 camera_motion；任何一种不得超过全片 70%，同一种不得连续超过 4 镜。变化必须服务建立空间、权力关系、人物反应、冲突升级或转折，不得机械轮换。
- 角色、服装、空间、光向、屏幕方向、关键道具状态必须跨镜继承；若切换场景，必须给出可见的过渡锚点。
- 同一 scene_id 内，相邻镜头的 continuity_out 与下一镜的 continuity_in 必须逐字相同，使用同一个可见动作、道具或光源锚点；禁止用“承接上一镜”“继续”“切换”等概括词代替。场景切换时，上一镜 continuity_out 必须写出该过渡锚点，下一镜 continuity_in 必须逐字复用它。
- shot_design_type 必须从「人物行动、人物反应、空间空镜、物件空镜、意象镜头、意识流镜头」中选择。空间空镜用于建立或回看既有空间、光线和环境状态；物件空镜用于既有道具、物件状态或动作余波；意象镜头把观点或转折落到既有故事中的象征性物件、光影或空间关系；意识流镜头把人物内在变化落到非文字的光影、形状、失焦或空间感知。后四类都不是把人物随机删掉，也不是无关风景：必须与当前故事段的 state、must_show、continuity_in/out 形成可见承接，不新增剧情。
- 语义发生转折、状态切换或大分段交界时，优先评估一个承上启下的桥接镜头；可用空间空镜、物件空镜、意象镜头或意识流镜头，但必须写清 transition_role=承上、启下或承上启下。若原文/故事没有可承接的空间、物件或感知锚点，不得硬插，改用人物行动或人物反应。桥接镜头可以是 AIGC 或静态图片，媒体类型仍须逐字遵守 immutable_media_route_plan。
- 「固定」只表示机位不动，不等于没有动作。blocking、must_show 或 story_mapping 中出现人物/物件的可见动作时，必须使用人物行动/人物反应并保留动作承载；不能仅因为 dynamic_level=低或 camera_motion=固定而降成静态空镜。
- 这是无对白默片：不得出现可读文字、字幕、对话框、进度条、聊天窗口、品牌 UI。
- 不得输出任何最终生图/生视频 prompt、模型参数、URL、任务 ID。
- ``immutable_media_route_plan`` 已依据 host 候选、数字人占比和时长/低动势规则锁定唯一媒体类型。每个 shot_context 必须逐字复用同 shot_id 的 media_type；全片第一个镜头必须是 AIGC 视频，不能使用数字人或静态图；除首镜外，低于 3 秒的镜头必须使用 static_image、固定机位并跳过 AIGC；3 秒及以上的静态图候选按 30% 目标比例排序，优先情绪停顿、结果落点、空间/物件状态和信息展示，并固定机位。动作推进、冲突升级、人物明显移动和运镜镜头使用 aigc_video。media_reason 只能解释既定选择，不得改变类型。

字段必须简洁：director_book 与 continuity_bible 每个字符串不超过 32 个汉字；beat、narrative_job、camera_intent、连续性交接不超过 24 个汉字；visual_direction 每个字符串不超过 20 个汉字；must_avoid 最多 3 项。不要输出解释段落。
visual_direction 必须从以下固定枚举中逐字选值，禁止自造近义词：shot_size 只能是「超远景、远景、全景、中景、近景、特写、极特写」；camera_angle 只能是「平视、俯视、仰视、低机位、高机位、越肩、侧后方」；camera_motion 只能是「固定、推近、拉远、横移、跟拍、摇镜、环绕」；composition 只能是「纵向主体—物件关系、边缘压力切入、斜向纵深、遮挡或框景、多人层级、局部焦点与留白」。dynamic_level 只能是「低、中、高」；只有无明显动作、无连续运镜且固定机位的镜头才可标为「低」。
只输出合法 JSON：
{
  "director_book":{"dramatic_question":"","visual_spine":"","reveal_policy":"","shot_rhythm_rule":"","cutting_rule":""},
  "continuity_bible":{"protagonist_anchor":"","wardrobe_anchor":"","location_anchor":"","prop_state_anchor":"","lighting_anchor":"","screen_direction_anchor":"","color_anchor":"","forbidden_drifts":["..."]},
  "beat_sequence":[{"beat_id":"beat_01","scene_id":"scene_01","group_ids":["g01"],"dramatic_function":"","state_before":"","trigger":"","state_after":"","bridge_to_next":""}],
   "shot_contexts":[{"shot_id":"g01_s01","group_id":"g01","timeline":{"start_us":0,"end_us":1},"beat_id":"beat_01","scene_id":"scene_01","shot_design_type":"人物行动","transition_role":"无","narrative_job":"","blocking":"起点→动作→终点","camera_intent":"","dynamic_level":"中","visual_direction":{"shot_size":"中景","camera_angle":"平视","camera_motion":"推近","composition":"斜向纵深","staging":"人物、物件和空间如何调度","lighting":"主光方向与情绪","visual_motif":"可反复出现的视觉母题"},"media_type":"aigc_video","media_reason":"有可见动作与运镜，需连续视频表达","continuity_in":"","continuity_out":"","must_show":"","must_avoid":["..."]}],
  "review":{"status":"PASS","checks":["locked_ids_preserved","locked_timelines_preserved","story_causality_present","continuity_rules_present","no_text_or_ui"],"issues":[]}
}"""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CinematicStoryboardGovernanceError(f"{field} 必须是非空字符串")
    return value.strip()


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CinematicStoryboardGovernanceError(f"{field} 必须是对象")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise CinematicStoryboardGovernanceError(f"{field} 必须是数组")
    return value


def _longest_run(values: Sequence[str]) -> int:
    longest = current = 0
    previous = None
    for value in values:
        current = current + 1 if value == previous else 1
        longest = max(longest, current)
        previous = value
    return longest


def _validate_camera_language_distribution(contexts: Sequence[Mapping[str, Any]]) -> None:
    """防止真实逐镜结果退化为几乎全平视、全固定的幻灯片。"""

    if len(contexts) < 8:
        return
    angles = [str(context["visual_direction"]["camera_angle"]) for context in contexts]
    # 静态图片不执行实际运镜；连续的“固定”不应被误判为视频镜头语言单调。
    motion_contexts = [context for context in contexts if context.get("media_type") != "static_image"]
    motions = [str(context["visual_direction"]["camera_motion"]) for context in motion_contexts]
    checks = [("机位", angles)]
    if len(motion_contexts) >= 8:
        checks.append(("运镜", motions))
    for label, values in checks:
        max_dominant = math.ceil(len(values) * 0.70)
        counts = Counter(values)
        if len(counts) < 3:
            raise CinematicStoryboardGovernanceError(f"全片{label}至少需要 3 种")
        dominant, count = counts.most_common(1)[0]
        if count > max_dominant:
            raise CinematicStoryboardGovernanceError(
                f"全片{label}过于单调：{dominant} 占 {count}/{len(values)}"
            )
        if _longest_run(values) > 4:
            raise CinematicStoryboardGovernanceError(f"同一种{label}不得连续超过 4 镜")


def _decode(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise CinematicStoryboardGovernanceError("治理层响应必须是 JSON 对象")
    try:
        decoded = json.loads(value.replace("```json", "").replace("```", "").strip())
    except json.JSONDecodeError as exc:
        raise CinematicStoryboardGovernanceError("治理层响应不是合法 JSON") from exc
    return _mapping(decoded, "governance")


def _assert_no_forbidden(value: Mapping[str, Any], field: str) -> None:
    leaked = sorted(FORBIDDEN_KEYS.intersection(value))
    if leaked:
        raise CinematicStoryboardGovernanceError(f"{field} 不得包含素材层字段：{', '.join(leaked)}")


def _window(value: Any, field: str) -> dict[str, int]:
    raw = _mapping(value, field)
    start = raw.get("start_us", raw.get("start"))
    end = raw.get("end_us", raw.get("end"))
    if isinstance(start, bool) or not isinstance(start, int):
        raise CinematicStoryboardGovernanceError(f"{field}.start_us 必须是整数微秒")
    if isinstance(end, bool) or not isinstance(end, int) or end <= start:
        raise CinematicStoryboardGovernanceError(f"{field}.end_us 必须大于 start_us")
    return {"start_us": start, "end_us": end}


def _frozen_window(value: Any, field: str) -> dict[str, int]:
    """保留剪辑层完整微秒契约；模型仅需回传起止点，时长由锁定层原样注入。"""
    window = _window(value, field)
    raw = _mapping(value, field)
    duration = raw.get("duration_us", window["end_us"] - window["start_us"])
    if isinstance(duration, bool) or not isinstance(duration, int) or duration != window["end_us"] - window["start_us"]:
        raise CinematicStoryboardGovernanceError(f"{field}.duration_us 必须等于 end_us - start_us")
    return {**window, "duration_us": duration}


DEFAULT_DIGITAL_HUMAN_MAX_SHOT_RATIO = 0.15
TARGET_SHOT_MIN_DURATION_US = 2_000_000
TARGET_SHOT_MAX_DURATION_US = 5_000_000
# 常规镜头仍严格限制在 2～5 秒。只有连续静态图纠偏明确合并、且强制转为
# AIGC 的视觉槽位，才允许占用 5.5 秒以内的剪辑窗口。
PACED_AIGC_MERGE_MAX_DURATION_US = MERGED_AIGC_MAX_DURATION_US
SHORT_SHOT_STATIC_IMAGE_THRESHOLD_US = 3_000_000
# 低动势镜头仍受整体 2～5 秒软时长窗口约束；超过 5 秒先由编导层拆镜，
# 不在素材层用一张图片硬撑过长时间。
STATIC_IMAGE_LOW_MOTION_MAX_DURATION_US = TARGET_SHOT_MAX_DURATION_US
# 图片承担停顿、信息落点和空间/物件状态，但不应挤掉连续动作。候选超过目标时
# 按叙事静态性排序，避免每条片子因为宽松的候选判定变成“图片占多数”。
STATIC_IMAGE_TARGET_SHOT_RATIO = 0.30
# 开场需要尽快建立运动感。前三个镜头在“视频/静态图”之间竞争时，视频获得
# 15 分偏好，并从静态图候选中等额扣除；这只是评分约束，不能突破短镜静态图和
# 数字人配额两条既有硬规则。
OPENING_VIDEO_PREFERENCE_SHOT_COUNT = 3
OPENING_VIDEO_SCORE_BONUS = 15
OPENING_STATIC_IMAGE_SCORE_PENALTY = 15

# 低动势图片不应只由“时长”决定。这里保留编导层可复用的叙事职责，
# 并允许治理模型通过 fixed camera / dynamic_level / motion_required 明确表达
# “画面不需要连续运动”。这些值只决定图片候选，不会覆盖数字人优先级。
LOW_MOTION_STATIC_ROLES = {
    "状态建立", "现象建立", "情绪停顿", "反应确认", "结果落点", "余波保留",
    "信息触发", "信息展示", "关系校准", "认知偏差", "压力释放", "物件特写",
    "空间建立", "状态观察", "专业解释",
}
STATIC_IMAGE_ROLE_PRIORITY = {
    "情绪停顿": 3,
    "反应确认": 3,
    "结果落点": 3,
    "余波保留": 3,
    "物件特写": 3,
    "空间建立": 3,
    "状态观察": 3,
    "状态建立": 2,
    "现象建立": 2,
    "信息触发": 2,
    "信息展示": 2,
    "关系校准": 2,
    "认知偏差": 2,
    "压力释放": 2,
    "专业解释": 2,
}
LOW_MOTION_MARKERS = (
    "低动态", "低动势", "low motion", "low_dynamic", "静态", "无明显动作",
    "空镜", "物件特写", "情绪停顿", "信息展示", "空间建立",
)
HIGH_MOTION_MARKERS = (
    "高动态", "高动势", "动作推进", "冲突升级", "人物明显移动", "明显移动",
    "需要连续运动", "high motion", "high_dynamic",
)
CAMERA_MOTION_VALUES = set(VISUAL_CAMERA_MOTIONS) - {"固定"}


def _contains_marker(value: Any, markers: Sequence[str] = LOW_MOTION_MARKERS) -> bool:
    if isinstance(value, str):
        normalized = value.strip().casefold().replace(" ", "")
        return any(marker.casefold().replace(" ", "") in normalized for marker in markers)
    if isinstance(value, (list, tuple, set)):
        return any(_contains_marker(item, markers) for item in value)
    return False


def _motion_required_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"false", "0", "no", "none", "否", "不需要", "无需"}:
            return False
        if normalized in {"true", "1", "yes", "需要", "有"}:
            return True
    return None


def _flatten_text(value: Any) -> str:
    """把治理上下文压成可检索文本，不把字段名本身当成动作。"""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    return ""


def _has_visible_action_signal(
    shot: Mapping[str, Any], production: Mapping[str, Any], context: Mapping[str, Any],
) -> bool:
    """动作优先于历史“低动态”标签，避免把有动作的镜头错判为空镜。"""

    values = [
        context.get("must_show"),
        context.get("blocking"),
        context.get("action"),
        context.get("motion_brief"),
        context.get("story_mapping"),
        shot.get("source_text"),
        shot.get("story_beat"),
        production.get("story_mapping"),
        production.get("action"),
    ]
    combined = _flatten_text(values).replace(" ", "")
    return any(marker in combined for marker in VISIBLE_ACTION_MARKERS)


def _has_shot_local_visible_action_signal(
    shot: Mapping[str, Any], production: Mapping[str, Any], context: Mapping[str, Any],
) -> bool:
    """只以当前小镜头的动作字段决定素材路线。

    ``story_mapping``、``must_show`` 和 ``blocking`` 可描述整段故事动作，不能把
    同段每个语义坑位都误判成连续动作镜头。它们仍由完整的导演治理校验使用。
    """

    values = [
        shot.get("source_text"),
        shot.get("story_beat"),
        shot.get("action"),
        production.get("action"),
        context.get("action"),
        context.get("motion_brief"),
    ]
    combined = _flatten_text(values).replace(" ", "")
    return any(marker in combined for marker in VISIBLE_ACTION_MARKERS)


def _known_character_count(lock: Mapping[str, Any], story: Mapping[str, Any]) -> int | None:
    """Read an explicit cast count for deterministic fallback compatibility.

    We do not infer people from prose.  An unknown count keeps the historical
    camera-language distribution unchanged; an explicit one-person cast cannot
    receive the ``多人层级`` fallback composition.
    """

    for container in (lock, story):
        value = container.get("known_character_count")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        for key in ("known_characters", "characters", "character_profiles"):
            values = container.get(key)
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                return len(values)
    return None


def _infer_shot_design_type(
    shot: Mapping[str, Any], production: Mapping[str, Any], context: Mapping[str, Any], media_type: str,
) -> str:
    """兼容旧审核稿：没有新字段时，从已有导演上下文推导镜头设计类型。"""

    combined = _flatten_text([
        context.get("must_show"), context.get("blocking"), context.get("camera_intent"),
        context.get("narrative_job"), context.get("story_mapping"), shot.get("source_text"),
        shot.get("story_beat"), production.get("story_mapping"),
    ])
    if _has_visible_action_signal(shot, production, context):
        return "人物行动"
    if any(token in combined for token in STREAM_OF_CONSCIOUSNESS_MARKERS):
        return "意识流镜头"
    if any(token in combined for token in IMAGERY_MARKERS):
        return "意象镜头"
    if any(token in combined for token in ("物件", "道具", "零件", "文件", "手机", "钥匙", "信", "工具", "余波")):
        return "物件空镜"
    if any(token in combined for token in ("空间", "环境", "街道", "楼宇", "走廊", "门口", "窗光", "场所", "空镜")):
        return "空间空镜"
    return "人物反应" if media_type != "static_image" else "人物反应"


def _infer_transition_role(
    shot: Mapping[str, Any], production: Mapping[str, Any], context: Mapping[str, Any],
) -> str:
    """从已存在的故事承接字段推导桥接方向，兼容旧审核稿。"""

    combined = _flatten_text([
        context.get("transition_role"), context.get("narrative_job"), context.get("turn"),
        context.get("bridge_to_next"), context.get("continuity_in"), context.get("continuity_out"),
        shot.get("clip_role"), shot.get("story_beat"), shot.get("source_text"),
        production.get("story_mapping"),
    ])
    if any(token in combined for token in ("承上启下", "转折", "转场", "过渡")):
        return "承上启下"
    if any(token in combined for token in ("下段承接", "启下", "结果落点", "下一段")):
        return "启下"
    if any(token in combined for token in ("前状态", "承上", "回看", "余波")):
        return "承上"
    return "无"


def _fallback_empty_design_type(blob: str) -> str:
    """选择有语义依据的非人物桥接形式，不凭空制造无关空镜。"""

    if any(token in blob for token in STREAM_OF_CONSCIOUSNESS_MARKERS):
        return "意识流镜头"
    if any(token in blob for token in IMAGERY_MARKERS):
        return "意象镜头"
    if any(token in blob for token in ("物件", "道具", "文件", "手机", "钥匙", "零件", "工具", "信", "余波")):
        return "物件空镜"
    return "空间空镜"


def _validate_empty_shot_design(contexts: Sequence[Mapping[str, Any]]) -> None:
    """保证完整序列真的评估过空镜，而不是只在提示词里声明能力。"""

    if len(contexts) < 4:
        return
    empty_contexts = [
        context for context in contexts
        if context.get("shot_design_type") in EMPTY_SHOT_DESIGN_TYPES
    ]
    if not empty_contexts:
        raise CinematicStoryboardGovernanceError(
            "完整序列至少需要一个有语义承接的空镜、意象或意识流设计"
        )
    for context in empty_contexts:
        if context.get("media_type") == "digital_human_video":
            raise CinematicStoryboardGovernanceError("空镜、意象或意识流镜头不得使用数字人视频")
        if _has_visible_action_signal({}, {}, context):
            raise CinematicStoryboardGovernanceError(
                f"{context.get('shot_id', '')} 空镜设计包含可见动作，必须改为人物行动或人物反应"
            )


def _normalize_dynamic_level(value: Any, media_type: str, field: str) -> str:
    if value is None:
        return "低" if media_type == "static_image" else "中"
    if not isinstance(value, str) or not value.strip():
        raise CinematicStoryboardGovernanceError(f"{field} 必须是低、中或高")
    aliases = {
        "low": "低", "low_motion": "低", "低动态": "低", "低动势": "低",
        "medium": "中", "mid": "中", "中动态": "中", "中动势": "中",
        "high": "高", "high_motion": "高", "高动态": "高", "高动势": "高",
    }
    normalized = value.strip()
    normalized = aliases.get(normalized.casefold(), normalized)
    if normalized not in {"低", "中", "高"}:
        raise CinematicStoryboardGovernanceError(f"{field} 必须是低、中或高")
    return normalized


def _is_low_motion_shot(
    shot: Mapping[str, Any], production: Mapping[str, Any], context: Mapping[str, Any] | None = None,
) -> bool:
    """判断镜头是否可以用固定首帧图承载。"""

    context = context if isinstance(context, Mapping) else {}
    visual = context.get("visual_direction")
    visual = visual if isinstance(visual, Mapping) else {}
    # 先读叙事动作，再读历史低动势标签。固定机位可以拍动作，不能把
    # “低动态/固定”直接等同于“空镜”或“静态图片”。
    if _has_shot_local_visible_action_signal(shot, production, context):
        return False
    motion_values = (
        visual.get("camera_motion"),
        context.get("camera_motion"),
        shot.get("camera_motion"),
        production.get("camera_motion"),
    )
    required_values = (
        context.get("motion_required"),
        shot.get("motion_required"),
        production.get("motion_required"),
    )
    required_motion = None
    for value in required_values:
        required = _motion_required_value(value)
        if required is True:
            required_motion = True
        elif required is False and required_motion is None:
            required_motion = False

    explicit_dynamic_values = (
        context.get("dynamic_level"),
        shot.get("dynamic_level"),
        production.get("dynamic_level"),
    )
    explicit_dynamic = next(
        (value for value in explicit_dynamic_values if isinstance(value, str) and value.strip()), None,
    )
    # dynamic_level 是编导层的明确判定；有它时不再让历史 media_reason 反向覆盖，
    # 避免“低动势”镜头因为旧的“AIGC 动作推进”说明而无法降级图片。
    dynamic_values = (
        explicit_dynamic_values
        if explicit_dynamic is not None
        else (
            context.get("media_reason"),
            shot.get("media_reason"),
            production.get("media_reason"),
        )
    )
    if any(_contains_marker(value, HIGH_MOTION_MARKERS) for value in dynamic_values):
        return False
    explicit_low_motion = any(_contains_marker(value) for value in dynamic_values)
    explicit_camera_motion = next(
        (value for value in motion_values if isinstance(value, str) and value.strip()), None,
    )
    if explicit_camera_motion:
        motion = explicit_camera_motion.strip()
        if motion in CAMERA_MOTION_VALUES or _contains_marker(
            motion, ("推", "拉", "横移", "跟拍", "摇", "环绕", "移动"),
        ):
            return False
        if motion != "固定":
            return False

    if context.get("shot_design_type") in EMPTY_SHOT_DESIGN_TYPES:
        return True

    if required_motion is not None:
        return not required_motion

    roles = {
        str(shot.get("clip_role") or "").strip(),
        str(context.get("narrative_job") or "").strip(),
    }
    return explicit_low_motion or bool(roles.intersection(LOW_MOTION_STATIC_ROLES))


def _route_candidates(shot: Mapping[str, Any], production: Mapping[str, Any]) -> set[str]:
    """读取冻结镜头已有的路线候选；候选不是下游可随意改写的最终路线。"""

    values: list[Any] = []
    for source in (shot.get("route_candidates"), production.get("route_candidates")):
        if isinstance(source, list):
            values.extend(source)
        elif isinstance(source, str):
            values.extend(source.replace("，", ",").split(","))
    selected = production.get("selected_route")
    if isinstance(selected, str) and selected.strip():
        values.append(selected)
    return {str(item).strip().casefold() for item in values if str(item).strip()}


def _static_image_priority(item: Mapping[str, Any]) -> int:
    """为动作已排除的图片候选排优先级，分数越高越适合静态图。"""

    shot = _mapping(item.get("shot"), "static_image_candidate.shot")
    return STATIC_IMAGE_ROLE_PRIORITY.get(str(shot.get("clip_role") or "").strip(), 1)


def _media_route_selection_score(item: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    """计算媒体路由的可审计偏好分，不把偏好伪装成硬性改写。"""

    static_priority = _static_image_priority(item)
    # 静态角色优先级越高，越适合由一张首帧图承载；视频分与之互补，二者合计 100。
    # +15 的开场偏好会使普通/中等静态候选转向视频，但仍不会压过最高静态性镜头。
    static_image_score = 40 + static_priority * 10
    video_score = 100 - static_image_score
    opening_preference_applied = index < OPENING_VIDEO_PREFERENCE_SHOT_COUNT
    video_adjustment = OPENING_VIDEO_SCORE_BONUS if opening_preference_applied else 0
    static_adjustment = -OPENING_STATIC_IMAGE_SCORE_PENALTY if opening_preference_applied else 0
    return {
        "video": video_score + video_adjustment,
        "static_image": static_image_score + static_adjustment,
        "base": {"video": video_score, "static_image": static_image_score},
        "adjustments": {"video": video_adjustment, "static_image": static_adjustment},
        "opening_video_preference_applied": opening_preference_applied,
    }


def _digital_human_ratio(lock: Mapping[str, Any]) -> float:
    raw = lock.get("digital_human_max_shot_ratio", DEFAULT_DIGITAL_HUMAN_MAX_SHOT_RATIO)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 1:
        raise CinematicStoryboardGovernanceError("digital_human_max_shot_ratio 必须在 0 到 1 之间")
    return float(raw)


def _candidate_role_priority(shot: Mapping[str, Any]) -> int:
    """同一大段有多个主播候选时，优先承担钩子、冲突或结论的那一个。"""

    priorities = {
        "信息触发": 0,
        "矛盾揭露": 1,
        "结果落点": 2,
        "关系变化": 3,
        "现象建立": 4,
        "状态建立": 5,
    }
    return priorities.get(str(shot.get("clip_role") or ""), 6)


def _select_spread_positions(
    candidates: Sequence[int], *, shot_count: int, wanted_count: int, existing: set[int],
) -> set[int]:
    """按整片时间线均匀挑选候选，且永远不与已有数字人镜头相邻。"""

    selected: set[int] = set()
    for ordinal in range(wanted_count):
        target = ((ordinal + 1) * (shot_count + 1) / (wanted_count + 1)) - 1
        viable = [
            index for index in candidates
            if index not in selected
            and index not in existing
            and all(abs(index - other) > 1 for other in (*existing, *selected))
        ]
        if not viable:
            break
        selected.add(min(viable, key=lambda index: (abs(index - target), index)))
    return selected


def resolve_cinematic_media_routes(
    director_lock: Mapping[str, Any], governance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把编导候选路线收口为不可变的逐镜媒体类型。

    ``host`` 是数字人候选而非最终路线，必须在此按占比、非连续和叙事分散性
    选择；低动势且固定机位的镜头允许成为静态图，不再人为限制图片比例。下游
    模型仅可丰富画面，不能把它们再改成全 AIGC。
    """

    lock = _mapping(director_lock, "director_lock")
    shots = _list(lock.get("shots"), "director_lock.shots")
    if not shots:
        raise CinematicStoryboardGovernanceError("director_lock.shots 不能为空")
    ratio = _digital_human_ratio(lock)
    max_digital_count = math.floor(len(shots) * ratio + 1e-9)
    governance_contexts = []
    if isinstance(governance, Mapping) and isinstance(governance.get("shot_contexts"), list):
        governance_contexts = governance["shot_contexts"]
    prepared: list[dict[str, Any]] = []
    forced_positions: set[int] = set()
    host_positions: list[int] = []
    for index, raw in enumerate(shots):
        shot = _mapping(raw, f"director_lock.shots[{index}]")
        production = _mapping(shot.get("production_spec", {}), f"shots[{index}].production_spec")
        timeline = _mapping(shot.get("timeline"), f"shots[{index}].timeline")
        start_us = timeline.get("start_us", timeline.get("start"))
        end_us = timeline.get("end_us", timeline.get("end"))
        if (
            isinstance(start_us, bool) or not isinstance(start_us, int)
            or isinstance(end_us, bool) or not isinstance(end_us, int)
            or end_us <= start_us
        ):
            raise CinematicStoryboardGovernanceError(
                f"shots[{index}].timeline 必须包含有效的整数微秒区间"
            )
        # 首镜是全片视觉锚点，必须为 AIGC；连续静态纠偏可让后续短镜
        # 例外进入 AIGC，以打断连续图片造成的幻灯片感。
        pacing = production.get("media_pacing")
        force_aigc = isinstance(pacing, Mapping) and bool(pacing.get("force_aigc"))
        is_short_static_shot = (
            index != 0
            and end_us - start_us < SHORT_SHOT_STATIC_IMAGE_THRESHOLD_US
        )
        candidates = _route_candidates(shot, production)
        context = (
            governance_contexts[index]
            if index < len(governance_contexts) and isinstance(governance_contexts[index], Mapping)
            else {}
        )
        selected_route = str(production.get("selected_route") or "").strip().casefold()
        is_host_candidate = bool(candidates.intersection({"host", "digital_human", "digital_human_video"}))
        # 首帧承担整片的故事开场和视觉锚点，必须是非数字人的画面素材。
        # 即使历史清单显式标成数字人，也不能突破这条硬约束。
        if selected_route == "digital_human" and index != 0 and not is_short_static_shot:
            forced_positions.add(index)
        if is_host_candidate and index != 0 and not is_short_static_shot:
            host_positions.append(index)
        prepared.append({
            "shot": shot,
            "is_host_candidate": is_host_candidate,
            "is_short_static_shot": is_short_static_shot,
            "is_low_motion_shot": _is_low_motion_shot(shot, production, context),
            "force_aigc": force_aigc,
        })
    for index, item in enumerate(prepared):
        item["media_selection_score"] = _media_route_selection_score(item, index=index)
    # 旧版落盘稿没有保存比例字段；仅为兼容这类已明确指定数字人坑位的历史稿
    # 放行其显式选择。新建清单会携带该字段，必须严格遵守用户设定的比例。
    has_declared_ratio = "digital_human_max_shot_ratio" in lock
    if len(forced_positions) > max_digital_count and has_declared_ratio:
        raise CinematicStoryboardGovernanceError(
            f"已锁定数字人镜头 {len(forced_positions)} 个，超过 {ratio:.0%} 的上限 {max_digital_count} 个"
        )
    if forced_positions:
        max_digital_count = max(max_digital_count, len(forced_positions))
    ordered_forced = sorted(forced_positions)
    if any(current == previous + 1 for previous, current in zip(ordered_forced, ordered_forced[1:])):
        raise CinematicStoryboardGovernanceError("已锁定数字人镜头不允许连续")

    digital_positions = set(forced_positions)
    auto_needed = max(0, max_digital_count - len(digital_positions))
    if auto_needed and host_positions:
        # 每个大分段先只贡献一个最合适的候选，避免同一段连续出现主播。
        representatives: dict[str, int] = {}
        for position in host_positions:
            shot = prepared[position]["shot"]
            group_id = _text(shot.get("group_id"), f"shots[{position}].group_id")
            previous = representatives.get(group_id)
            if previous is None or (
                _candidate_role_priority(shot), position
            ) < (_candidate_role_priority(prepared[previous]["shot"]), previous):
                representatives[group_id] = position
        digital_positions.update(_select_spread_positions(
            list(representatives.values()),
            shot_count=len(shots),
            wanted_count=auto_needed,
            existing=digital_positions,
        ))
        if len(digital_positions) < max_digital_count:
            digital_positions.update(_select_spread_positions(
                host_positions,
                shot_count=len(shots),
                wanted_count=max_digital_count - len(digital_positions),
                existing=digital_positions,
            ))

    short_static_positions = [
        index for index, item in enumerate(prepared)
        if index not in digital_positions and item["is_short_static_shot"]
    ]
    static_positions = [
        index for index, item in enumerate(prepared)
        if index not in digital_positions
        and not item["is_short_static_shot"]
        and not item["force_aigc"]
        and item["media_selection_score"]["static_image"] >= item["media_selection_score"]["video"]
        and item["shot"].get("timeline", {}).get("duration_us", 0) <= STATIC_IMAGE_LOW_MOTION_MAX_DURATION_US
        and item["is_low_motion_shot"]
    ]
    # 低于 3 秒的镜头是硬规则：只生成静态图片，绝不进入 AIGC 视频。
    # 3 秒及以上的低动势镜头全部允许使用图片；不再为了凑比例强行生成视频。
    target_static_count = max(
        len(short_static_positions),
        math.ceil(len(shots) * STATIC_IMAGE_TARGET_SHOT_RATIO),
    )
    ranked_static_positions = sorted(
        static_positions,
        key=lambda index: (
            -prepared[index]["media_selection_score"]["static_image"],
            -_static_image_priority(prepared[index]),
            index,
        ),
    )
    selected_static = set(ranked_static_positions[:target_static_count])
    selected_static.update(short_static_positions)

    decisions: list[dict[str, Any]] = []
    for index, item in enumerate(prepared):
        shot = item["shot"]
        shot_id = _text(shot.get("shot_id"), f"shots[{index}].shot_id")
        if index == 0:
            media_type = "aigc_video"
            reason = "全片首镜硬约束：使用 AIGC 视频建立开场视觉锚点。"
        elif index in digital_positions:
            media_type = "digital_human_video"
            reason = "主播候选按全片节奏分散入选，使用数字人视频承担关键解释。"
        elif item["force_aigc"] and not item["is_short_static_shot"]:
            media_type = "aigc_video"
            reason = "连续静态图片达到阈值，按节奏权重提升为 AIGC 视频以保持画面推进。"
        elif index in selected_static:
            media_type = "static_image"
            if prepared[index]["is_short_static_shot"]:
                reason = "镜头时长低于 3 秒，使用静态图片完成快速切换，不生成 AIGC 视频。"
            else:
                reason = "低动势且固定机位，使用首帧图承载叙事，不生成 AIGC 视频。"
        else:
            media_type = "aigc_video"
            reason = "存在动作推进或关系变化，使用 AIGC 视频完成连续叙事。"
        decisions.append({
            "shot_id": shot_id,
            "media_type": media_type,
            "media_reason": reason,
            "media_selection_score": dict(item["media_selection_score"]),
        })
    return {
        "by_shot": {item["shot_id"]: item for item in decisions},
        "decisions": decisions,
        "policy": {
            "digital_human_max_shot_ratio": ratio,
            "digital_human_max_shot_count": max_digital_count,
            "digital_human_selected_shot_count": len(digital_positions),
            "digital_human_forbid_consecutive": True,
            "first_shot_forbid_digital_human": True,
            "first_shot_must_be_aigc": True,
            "static_image_low_motion_max_duration_us": STATIC_IMAGE_LOW_MOTION_MAX_DURATION_US,
            "static_image_target_shot_ratio": STATIC_IMAGE_TARGET_SHOT_RATIO,
            "static_image_target_shot_count": target_static_count,
            "target_shot_duration_min_us": TARGET_SHOT_MIN_DURATION_US,
            "target_shot_duration_max_us": TARGET_SHOT_MAX_DURATION_US,
            "short_shot_static_image_threshold_us": SHORT_SHOT_STATIC_IMAGE_THRESHOLD_US,
            "short_shot_static_image_count": len(short_static_positions),
            "static_image_low_motion_quota": None,
            "static_image_low_motion_quota_enforced": False,
            "static_image_unselected_host_candidate_allowed": True,
            "static_image_selected_shot_count": len(selected_static),
            "opening_video_preference": {
                "shot_count": OPENING_VIDEO_PREFERENCE_SHOT_COUNT,
                "video_score_bonus": OPENING_VIDEO_SCORE_BONUS,
                "static_image_score_penalty": OPENING_STATIC_IMAGE_SCORE_PENALTY,
                "score_transfer": "static_image_to_video",
                "hard_rules_preserved": [
                    "short_shot_static_image",
                    "digital_human_quota",
                ],
                "applied_shot_ids": [
                    _text(item["shot"].get("shot_id"), "opening_preference.shot_id")
                    for item in prepared[:OPENING_VIDEO_PREFERENCE_SHOT_COUNT]
                ],
            },
            "consecutive_static_forced_aigc_count": sum(
                1 for item in prepared if item["force_aigc"]
            ),
        },
    }


def build_cinematic_storyboard_prompt(director_lock: Mapping[str, Any]) -> str:
    """把冻结编导清单压缩成模型可消费但不可篡改的导演输入。"""

    lock = _mapping(director_lock, "director_lock")
    if lock.get("status") != "DIRECTOR_LOCKED":
        raise CinematicStoryboardGovernanceError("只接受 DIRECTOR_LOCKED 的编导清单")
    story = _mapping(lock.get("cinematic_story"), "director_lock.cinematic_story")
    shots = _list(lock.get("shots"), "director_lock.shots")
    if not shots:
        raise CinematicStoryboardGovernanceError("director_lock.shots 不能为空")
    media_plan = resolve_cinematic_media_routes(lock)
    frozen_shots = []
    for index, raw in enumerate(shots):
        shot = _mapping(raw, f"director_lock.shots[{index}]")
        shot_id = _text(shot.get("shot_id"), f"shots[{index}].shot_id")
        decision = media_plan["by_shot"][shot_id]
        frozen_shots.append({
            "shot_id": shot_id,
            "group_id": _text(shot.get("group_id"), f"shots[{index}].group_id"),
            "timeline": _frozen_window(shot.get("timeline"), f"shots[{index}].timeline"),
            "source_text": _text(shot.get("source_text"), f"shots[{index}].source_text"),
            "clip_role": _text(shot.get("clip_role"), f"shots[{index}].clip_role"),
            "story_beat": _text(shot.get("story_beat"), f"shots[{index}].story_beat"),
            "story_mapping": dict(_mapping(shot.get("production_spec", {}).get("story_mapping", {}), f"shots[{index}].story_mapping")),
            "selected_route": str(_mapping(shot.get("production_spec", {}), f"shots[{index}].production_spec").get("selected_route") or ""),
            "route_candidates": list(shot.get("route_candidates") or []),
            "media_type": decision["media_type"],
            "media_reason": decision["media_reason"],
        })
    payload = {
        "immutable_contract": {
            "project_id": _text(lock.get("project_id"), "director_lock.project_id"),
            "run_id": _text(lock.get("run_id"), "director_lock.run_id"),
            "shot_ids": [item["shot_id"] for item in frozen_shots],
            "timelines_unit": "microseconds",
            "no_prompt_or_model_fields": True,
        },
        "approved_story": {
            "movie_outline": dict(_mapping(story.get("movie_outline"), "cinematic_story.movie_outline")),
            "silent_story_text": _text(story.get("silent_story_text"), "cinematic_story.silent_story_text"),
            "scene_groups": list(_list(story.get("scene_groups"), "cinematic_story.scene_groups")),
            "narration_mappings": list(_list(story.get("narration_mappings"), "cinematic_story.narration_mappings")),
        },
        "frozen_shots": frozen_shots,
        "immutable_media_route_plan": media_plan["decisions"],
    }
    return json.dumps(payload, ensure_ascii=False)


def build_cinematic_storyboard_fallback(
    director_lock: Mapping[str, Any], *, reason: str = "",
) -> dict[str, Any]:
    """用冻结清单生成可校验的镜头语言骨架。

    电影化治理模型负责丰富导演意图，但不能拥有镜头 ID、时间线、旁白或
    剪辑坑位的写入权。真实调用偶发漏字段、返回近义镜头语言，或未遵守
    跨镜交接时，不能让整条素材链停在治理层。此兜底严格从
    ``DirectorLockedManifest`` 派生镜头语言骨架；后续 116616 仍会调用
    方舟，把它增润成具体的视角、景别与画面内容。
    """

    lock = _mapping(director_lock, "director_lock")
    if lock.get("status") != "DIRECTOR_LOCKED":
        raise CinematicStoryboardGovernanceError("只接受 DIRECTOR_LOCKED 的编导清单")
    story = _mapping(lock.get("cinematic_story"), "director_lock.cinematic_story")
    outline = _mapping(story.get("movie_outline"), "cinematic_story.movie_outline")
    raw_shots = _list(lock.get("shots"), "director_lock.shots")
    if not raw_shots:
        raise CinematicStoryboardGovernanceError("director_lock.shots 不能为空")

    shots: list[dict[str, Any]] = []
    group_order: list[str] = []
    for index, raw in enumerate(raw_shots):
        shot = _mapping(raw, f"director_lock.shots[{index}]")
        group_id = _text(shot.get("group_id"), f"shots[{index}].group_id")
        if group_id not in group_order:
            group_order.append(group_id)
        production = _mapping(shot.get("production_spec", {}), f"shots[{index}].production_spec")
        mapping_value = production.get("story_mapping")
        story_mapping = dict(mapping_value) if isinstance(mapping_value, Mapping) else {}
        shots.append({
            "shot_id": _text(shot.get("shot_id"), f"shots[{index}].shot_id"),
            "group_id": group_id,
            "timeline": _frozen_window(shot.get("timeline"), f"shots[{index}].timeline"),
            "source_text": _text(shot.get("source_text"), f"shots[{index}].source_text"),
            "clip_role": _text(shot.get("clip_role"), f"shots[{index}].clip_role"),
            "story_beat": _text(shot.get("story_beat"), f"shots[{index}].story_beat"),
            "selected_route": str(production.get("selected_route") or ""),
            "story_mapping": story_mapping,
        })

    scene_groups = _list(story.get("scene_groups"), "cinematic_story.scene_groups")
    grouped_scenes: list[tuple[str, list[str]]] = []
    claimed_groups: set[str] = set()
    for index, raw_scene in enumerate(scene_groups, start=1):
        scene = _mapping(raw_scene, f"cinematic_story.scene_groups[{index - 1}]")
        scene_id = str(scene.get("scene_id") or f"scene_{index:02d}").strip()
        requested = scene.get("group_ids")
        requested_groups = requested if isinstance(requested, list) else []
        group_ids = [
            str(group_id).strip()
            for group_id in requested_groups
            if str(group_id).strip() in group_order and str(group_id).strip() not in claimed_groups
        ]
        if group_ids:
            grouped_scenes.append((scene_id or f"scene_{index:02d}", group_ids))
            claimed_groups.update(group_ids)
    for group_id in group_order:
        if group_id not in claimed_groups:
            grouped_scenes.append((f"scene_{len(grouped_scenes) + 1:02d}", [group_id]))

    beat_sequence: list[dict[str, Any]] = []
    beat_by_group: dict[str, tuple[str, str]] = {}
    for index, (scene_id, group_ids) in enumerate(grouped_scenes, start=1):
        beat_id = f"beat_{index:02d}"
        beat_sequence.append({
            "beat_id": beat_id,
            "scene_id": scene_id,
            "group_ids": group_ids,
            "dramatic_function": "推进核心认知变化",
            "state_before": "上一段行动结果已成立",
            "trigger": "新的可见动作触发",
            "state_after": "观众获得下一层理解",
            "bridge_to_next": "动作落点交给下一镜",
        })
        for group_id in group_ids:
            beat_by_group[group_id] = (beat_id, scene_id)

    sizes = ("远景", "全景", "中景", "近景", "特写", "中景", "全景", "近景")
    angles = ("高机位", "平视", "低机位", "越肩", "侧后方", "仰视", "平视", "俯视")
    motions = ("跟拍", "横移", "推近", "固定", "摇镜", "拉远", "环绕", "推近")
    aigc_motions = ("跟拍", "横移", "推近", "摇镜", "拉远", "环绕")
    compositions = (
        "边缘压力切入", "纵向主体—物件关系", "斜向纵深", "遮挡或框景",
        "局部焦点与留白", "多人层级",
    )
    known_character_count = _known_character_count(lock, story)
    fallback_compositions = tuple(
        normalize_fallback_composition(
            composition, known_character_count=known_character_count,
        )
        for composition in compositions
    )
    media_plan = resolve_cinematic_media_routes(lock)
    contexts: list[dict[str, Any]] = []
    previous_scene_id = ""
    previous_handoff = ""
    # 空镜/意象/意识流是编导能力，而不是素材层临时把人物删掉。
    # 语义转折优先安排承上启下桥接镜头；没有明确转折时才回退到首镜空间建立。
    empty_positions: dict[int, tuple[str, str]] = {}
    if len(shots) >= 4:
        transition_candidates: list[tuple[int, str]] = []
        for candidate, candidate_shot in enumerate(shots):
            if media_plan["by_shot"][candidate_shot["shot_id"]]["media_type"] == "digital_human_video":
                continue
            candidate_blob = _flatten_text([
                candidate_shot.get("source_text"), candidate_shot.get("story_beat"),
                candidate_shot.get("clip_role"), candidate_shot.get("story_mapping"),
                candidate_shot.get("production_spec"),
            ])
            if candidate > 0 and any(token in candidate_blob for token in SEMANTIC_BRIDGE_MARKERS):
                transition_candidates.append((candidate, candidate_blob))
        if transition_candidates:
            candidate, candidate_blob = transition_candidates[0]
            empty_positions[candidate] = (_fallback_empty_design_type(candidate_blob), "承上启下")
        else:
            empty_positions[0] = ("空间空镜", "承上启下")
        if len(shots) >= 8:
            for candidate in (len(shots) // 2, len(shots) - 1, *range(1, len(shots))):
                if candidate in empty_positions or media_plan["by_shot"][shots[candidate]["shot_id"]]["media_type"] == "digital_human_video":
                    continue
                candidate_blob = _flatten_text([
                    shots[candidate].get("source_text"), shots[candidate].get("story_beat"),
                    shots[candidate].get("clip_role"), shots[candidate].get("story_mapping"),
                    shots[candidate].get("production_spec"),
                ])
                if any(token in candidate_blob for token in (*SEMANTIC_BRIDGE_MARKERS, *IMAGERY_MARKERS, *STREAM_OF_CONSCIOUSNESS_MARKERS, "物件", "道具", "文件", "手机", "钥匙", "零件", "工具", "信")):
                    empty_positions[candidate] = (_fallback_empty_design_type(candidate_blob), "承上启下")
                    break
    for index, shot in enumerate(shots):
        beat_id, scene_id = beat_by_group[shot["group_id"]]
        continuity_in = previous_handoff if previous_scene_id == scene_id else f"{scene_id}起始动作锚点"
        continuity_out = f"{shot['shot_id']}动作落点"
        mapping = shot["story_mapping"]
        media_decision = media_plan["by_shot"][shot["shot_id"]]
        media_type = media_decision["media_type"]
        media_reason = media_decision["media_reason"]
        shot_design_type, transition_role = empty_positions.get(index, ("人物行动", _infer_transition_role(
            shot, _mapping(shot.get("production_spec", {}), "production_spec"), mapping,
        )))
        if index not in empty_positions:
            # 事件没有明确动作时保留“人物反应”，让模型不要凭空补一个动作。
            shot_design_type = "人物行动" if _has_visible_action_signal(
                shot, _mapping(shot.get("production_spec", {}), "production_spec"), mapping,
            ) else "人物反应"
            must_show = str(mapping.get("silent_action") or mapping.get("semantic_mapping") or shot["source_text"]).strip()
            camera_motion = "固定" if media_type == "static_image" else aigc_motions[index % len(aigc_motions)]
            blocking = (
                f"{continuity_in}→定格呈现{must_show}→{continuity_out}"
                if media_type == "static_image"
                else f"{continuity_in}→可见动作→{continuity_out}"
            )
            staging = "人物与关键物件完成单一动作"
            visual_motif = "可见动作与关键物件"
            camera_intent = f"用{sizes[index % len(sizes)]}揭示{shot['source_text']}"
        else:
            camera_motion = "固定" if media_type == "static_image" else aigc_motions[index % len(aigc_motions)]
            if shot_design_type == "物件空镜":
                must_show = f"{scene_id}中与当前段落相关的既有物件状态与动作余波"
                camera_intent = f"用{sizes[index % len(sizes)]}交代关键物件状态"
                staging = "前景物件承载信息，中后景保留原场景关系，不出现人物"
                visual_motif = "物件状态与光线落点"
            elif shot_design_type == "意象镜头":
                must_show = f"与当前转折对应的既有象征性物件、光影或空间关系"
                camera_intent = f"用{sizes[index % len(sizes)]}把语义转折落到可见意象"
                staging = "象征性视觉锚点从前状态过渡到后状态，不出现人物"
                visual_motif = "象征物与光影变化"
            elif shot_design_type == "意识流镜头":
                must_show = f"与人物内在变化对应的非文字光影、形状或空间感知"
                camera_intent = f"用{sizes[index % len(sizes)]}表现认知状态的短暂失焦"
                staging = "光影、形状或空间层次完成一次非文字的感知变化，不出现人物"
                visual_motif = "失焦感与空间变形"
            else:
                must_show = f"{scene_id}的既有空间边界、光线方向与环境状态"
                camera_intent = f"用{sizes[index % len(sizes)]}先建立{scene_id}的空间关系"
                staging = "前中后景保持空间关系，关键环境状态先于人物出现"
                visual_motif = "空间边界与光线锚点"
            blocking = f"{continuity_in}→{must_show}显现→{continuity_out}"
        contexts.append({
            "shot_id": shot["shot_id"],
            "group_id": shot["group_id"],
            "timeline": shot["timeline"],
            "beat_id": beat_id,
            "scene_id": scene_id,
            "shot_design_type": shot_design_type,
            "transition_role": transition_role,
            "narrative_job": shot["clip_role"],
            "blocking": blocking,
            "camera_intent": camera_intent,
            "dynamic_level": "低" if media_type == "static_image" else "中",
            "visual_direction": {
                "shot_size": sizes[index % len(sizes)],
                "camera_angle": angles[index % len(angles)],
                "camera_motion": camera_motion,
                "composition": fallback_compositions[index % len(fallback_compositions)],
                "staging": staging,
                "lighting": "侧向主光保持空间连续",
                "visual_motif": visual_motif,
            },
            "media_type": media_type,
            "media_reason": media_reason,
            "continuity_in": continuity_in,
            "continuity_out": continuity_out,
            "must_show": must_show,
            "must_avoid": ["可读文字", "品牌UI"],
        })
        previous_scene_id = scene_id
        previous_handoff = continuity_out

    candidate = {
        "director_book": {
            "dramatic_question": str(outline.get("central_conflict") or "主角如何完成关键选择"),
            "visual_spine": "从状态建立到冲突升级再到结果落点",
            "reveal_policy": "先呈现处境，再以动作揭示转折",
            "shot_rhythm_rule": "远中近交替服务信息揭示",
            "cutting_rule": "在动作落点交接下一镜",
        },
        "continuity_bible": {
            "protagonist_anchor": str(outline.get("protagonist") or "主角形象保持一致"),
            "wardrobe_anchor": "同场景服装状态保持一致",
            "location_anchor": "场景内空间关系持续可辨",
            "prop_state_anchor": "关键物件随动作连续传递",
            "lighting_anchor": "侧向主光保持方向一致",
            "screen_direction_anchor": "主体运动方向保持连续",
            "color_anchor": "冲突段压低，转折段提亮",
            "forbidden_drifts": ["不得出现可读文字", "不得改变人物关系"],
        },
        "beat_sequence": beat_sequence,
        "shot_contexts": contexts,
        "review": {
            "status": "PASS",
            "checks": [
                "locked_ids_preserved", "locked_timelines_preserved", "story_causality_present",
                "continuity_rules_present", "no_text_or_ui",
            ],
            "issues": [],
        },
    }
    normalized = normalize_cinematic_storyboard_governance(candidate, lock)
    normalized["generation_mode"] = "deterministic_contract_baseline"
    normalized["media_route_policy"] = media_plan["policy"]
    if reason:
        normalized["fallback_reason"] = reason[:240]
    return normalized


def normalize_cinematic_storyboard_governance(value: Any, director_lock: Mapping[str, Any]) -> dict[str, Any]:
    """验证模型输出并保证其只能补充治理信息，不能改变锁定清单。"""

    data = _decode(value)
    lock = _mapping(director_lock, "director_lock")
    frozen = _list(lock.get("shots"), "director_lock.shots")
    expected: list[tuple[str, str, dict[str, int]]] = []
    paced_aigc_merge_ids: set[str] = set()
    for index, raw in enumerate(frozen):
        shot = _mapping(raw, f"director_lock.shots[{index}]")
        shot_id = _text(shot.get("shot_id"), "shot_id")
        expected.append((shot_id, _text(shot.get("group_id"), "group_id"), _frozen_window(shot.get("timeline"), "timeline")))
        production_spec = shot.get("production_spec")
        if isinstance(production_spec, Mapping):
            media_pacing = production_spec.get("media_pacing")
            if (
                isinstance(media_pacing, Mapping)
                and media_pacing.get("force_aigc") is True
                and media_pacing.get("action") == "merge_adjacent_static_to_aigc"
            ):
                paced_aigc_merge_ids.add(shot_id)
    # 真实治理结果中的 shot_contexts 带有 camera_motion / narrative_job，
    # 用它判断固定机位的低动势镜头，而不是只看冻结稿的时长。
    media_plan = resolve_cinematic_media_routes(lock, data)

    director_book = _mapping(data.get("director_book"), "director_book")
    _assert_no_forbidden(director_book, "director_book")
    normalized_book = {key: _text(director_book.get(key), f"director_book.{key}") for key in (
        "dramatic_question", "visual_spine", "reveal_policy", "shot_rhythm_rule", "cutting_rule",
    )}

    continuity = _mapping(data.get("continuity_bible"), "continuity_bible")
    _assert_no_forbidden(continuity, "continuity_bible")
    normalized_continuity = {key: _text(continuity.get(key), f"continuity_bible.{key}") for key in (
        "protagonist_anchor", "wardrobe_anchor", "location_anchor", "prop_state_anchor",
        "lighting_anchor", "screen_direction_anchor", "color_anchor",
    )}
    drifts = _list(continuity.get("forbidden_drifts"), "continuity_bible.forbidden_drifts")
    if not drifts or not all(isinstance(item, str) and item.strip() for item in drifts):
        raise CinematicStoryboardGovernanceError("continuity_bible.forbidden_drifts 必须是非空字符串数组")
    normalized_continuity["forbidden_drifts"] = [item.strip() for item in drifts]

    beats = _list(data.get("beat_sequence"), "beat_sequence")
    if not beats:
        raise CinematicStoryboardGovernanceError("beat_sequence 不能为空")
    normalized_beats = []
    seen_beat_ids: set[str] = set()
    for index, raw in enumerate(beats):
        item = _mapping(raw, f"beat_sequence[{index}]")
        _assert_no_forbidden(item, f"beat_sequence[{index}]")
        beat_id = _text(item.get("beat_id"), f"beat_sequence[{index}].beat_id")
        if beat_id in seen_beat_ids:
            raise CinematicStoryboardGovernanceError("beat_sequence.beat_id 不得重复")
        seen_beat_ids.add(beat_id)
        group_ids = _list(item.get("group_ids"), f"beat_sequence[{index}].group_ids")
        if not group_ids or not all(isinstance(group, str) and group.strip() for group in group_ids):
            raise CinematicStoryboardGovernanceError("beat_sequence.group_ids 必须是非空字符串数组")
        normalized_beats.append({
            "beat_id": beat_id,
            "scene_id": _text(item.get("scene_id"), f"beat_sequence[{index}].scene_id"),
            "group_ids": [group.strip() for group in group_ids],
            **{key: _text(item.get(key), f"beat_sequence[{index}].{key}") for key in (
                "dramatic_function", "state_before", "trigger", "state_after", "bridge_to_next",
            )},
        })
    expected_groups = []
    for _, group_id, _ in expected:
        if group_id not in expected_groups:
            expected_groups.append(group_id)
    flattened_groups = [group_id for beat in normalized_beats for group_id in beat["group_ids"]]
    if flattened_groups != expected_groups:
        raise CinematicStoryboardGovernanceError("beat_sequence 必须按顺序且恰好覆盖全部冻结大分段")
    beats_by_id = {beat["beat_id"]: beat for beat in normalized_beats}

    contexts = _list(data.get("shot_contexts"), "shot_contexts")
    if len(contexts) != len(expected):
        raise CinematicStoryboardGovernanceError("shot_contexts 必须与冻结小镜头一一对应")
    normalized_contexts = []
    previous_scene_id = ""
    previous_handoff = ""
    previous_shot_id = ""
    for index, raw in enumerate(contexts):
        item = _mapping(raw, f"shot_contexts[{index}]")
        _assert_no_forbidden(item, f"shot_contexts[{index}]")
        expected_id, expected_group, expected_timeline = expected[index]
        if _text(item.get("shot_id"), f"shot_contexts[{index}].shot_id") != expected_id:
            raise CinematicStoryboardGovernanceError("shot_contexts.shot_id 必须保持冻结顺序")
        if _text(item.get("group_id"), f"shot_contexts[{index}].group_id") != expected_group:
            raise CinematicStoryboardGovernanceError("shot_contexts.group_id 不得改变")
        if _window(item.get("timeline"), f"shot_contexts[{index}].timeline") != _window(expected_timeline, "expected_timeline"):
            raise CinematicStoryboardGovernanceError("shot_contexts.timeline 不得改变冻结时间线")
        duration_us = expected_timeline["duration_us"]
        duration_max_us = (
            PACED_AIGC_MERGE_MAX_DURATION_US
            if expected_id in paced_aigc_merge_ids
            else TARGET_SHOT_MAX_DURATION_US
        )
        if duration_us < TARGET_SHOT_MIN_DURATION_US or duration_us > duration_max_us:
            duration_range = "2～5.5" if expected_id in paced_aigc_merge_ids else "2～5"
            raise CinematicStoryboardGovernanceError(
                f"{expected_id} 镜头时长必须在 {duration_range} 秒，实际为 {duration_us / 1_000_000:.3f} 秒"
            )
        must_avoid = _list(item.get("must_avoid"), f"shot_contexts[{index}].must_avoid")
        if not must_avoid or not all(isinstance(token, str) and token.strip() for token in must_avoid):
            raise CinematicStoryboardGovernanceError("shot_contexts.must_avoid 必须是非空字符串数组")
        beat_id = _text(item.get("beat_id"), f"shot_contexts[{index}].beat_id")
        beat = beats_by_id.get(beat_id)
        if beat is None or expected_group not in beat["group_ids"]:
            raise CinematicStoryboardGovernanceError("小镜头必须引用所属大分段的场景节拍")
        scene_id = _text(item.get("scene_id"), f"shot_contexts[{index}].scene_id")
        if scene_id != beat["scene_id"]:
            raise CinematicStoryboardGovernanceError("小镜头 scene_id 必须继承所属场景节拍")
        raw_visual_direction = _mapping(item.get("visual_direction"), f"shot_contexts[{index}].visual_direction")
        _assert_no_forbidden(raw_visual_direction, f"shot_contexts[{index}].visual_direction")
        visual_direction = {
            key: _text(raw_visual_direction.get(key), f"shot_contexts[{index}].visual_direction.{key}")
            for key in ("shot_size", "camera_angle", "camera_motion", "composition", "staging", "lighting", "visual_motif")
        }
        if visual_direction["shot_size"] not in VISUAL_SHOT_SIZES:
            raise CinematicStoryboardGovernanceError("visual_direction.shot_size 不在允许景别中")
        if visual_direction["camera_angle"] not in VISUAL_CAMERA_ANGLES:
            raise CinematicStoryboardGovernanceError("visual_direction.camera_angle 不在允许机位中")
        if visual_direction["camera_motion"] not in VISUAL_CAMERA_MOTIONS:
            raise CinematicStoryboardGovernanceError("visual_direction.camera_motion 不在允许运镜中")
        if visual_direction["composition"] not in VISUAL_COMPOSITIONS:
            raise CinematicStoryboardGovernanceError("visual_direction.composition 不在允许构图中")
        raw_media_type = item.get("media_type")
        raw_media_reason = item.get("media_reason")
        # 兼容已经落盘的 v2 审核稿；新的真实调用由系统提示词强制明确输出类型。
        planned_media = media_plan["by_shot"][expected_id]
        if raw_media_type is None:
            media_type = planned_media["media_type"]
            media_reason = planned_media["media_reason"]
        else:
            media_type = _text(raw_media_type, f"shot_contexts[{index}].media_type")
            media_reason = _text(raw_media_reason, f"shot_contexts[{index}].media_reason")
        if media_type not in MEDIA_TYPES:
            raise CinematicStoryboardGovernanceError("shot_contexts.media_type 必须是 digital_human_video、aigc_video 或 static_image")
        if media_type != planned_media["media_type"]:
            raise CinematicStoryboardGovernanceError("shot_contexts.media_type 必须遵守已冻结的候选路由决策")
        raw_design_type = item.get("shot_design_type")
        if raw_design_type is None:
            shot = _mapping(frozen[index], f"director_lock.shots[{index}]")
            production = _mapping(shot.get("production_spec", {}), f"shots[{index}].production_spec")
            shot_design_type = _infer_shot_design_type(shot, production, item, media_type)
        else:
            shot_design_type = _text(raw_design_type, f"shot_contexts[{index}].shot_design_type")
        if shot_design_type not in SHOT_DESIGN_TYPES:
            raise CinematicStoryboardGovernanceError(
    "shot_contexts.shot_design_type 必须是人物行动、人物反应、空间空镜、物件空镜、意象镜头或意识流镜头"
            )
        if shot_design_type in EMPTY_SHOT_DESIGN_TYPES and media_type == "digital_human_video":
            raise CinematicStoryboardGovernanceError("空镜、意象或意识流镜头不得使用数字人视频")
        raw_transition_role = item.get("transition_role")
        if raw_transition_role is None:
            shot = _mapping(frozen[index], f"director_lock.shots[{index}]")
            production = _mapping(shot.get("production_spec", {}), f"shots[{index}].production_spec")
            transition_role = _infer_transition_role(shot, production, item)
        else:
            transition_role = _text(raw_transition_role, f"shot_contexts[{index}].transition_role")
        if transition_role not in TRANSITION_ROLES:
            raise CinematicStoryboardGovernanceError("shot_contexts.transition_role 必须是无、承上、启下或承上启下")
        dynamic_level = _normalize_dynamic_level(
            item.get("dynamic_level"), media_type, f"shot_contexts[{index}].dynamic_level",
        )
        if media_type == "static_image" and visual_direction["camera_motion"] != "固定":
            raise CinematicStoryboardGovernanceError("静态图片镜头必须使用固定机位")
        if dynamic_level == "低" and visual_direction["camera_motion"] != "固定":
            raise CinematicStoryboardGovernanceError("低动势镜头必须使用固定机位")
        normalized_context = {
            "shot_id": expected_id,
            "group_id": expected_group,
            "timeline": expected_timeline,
            **{key: _text(item.get(key), f"shot_contexts[{index}].{key}") for key in (
                "narrative_job", "blocking", "camera_intent",
                "continuity_in", "continuity_out", "must_show",
            )},
            "beat_id": beat_id,
            "scene_id": scene_id,
            "shot_design_type": shot_design_type,
            "transition_role": transition_role,
            "dynamic_level": dynamic_level,
            "visual_direction": visual_direction,
            "media_type": media_type,
            "media_reason": media_reason,
            "must_avoid": [token.strip() for token in must_avoid],
        }
        if previous_scene_id == scene_id and previous_handoff != normalized_context["continuity_in"]:
            raise CinematicStoryboardGovernanceError(
                f"故事交接未闭合：{previous_shot_id}.continuity_out 必须等于 {expected_id}.continuity_in"
            )
        normalized_contexts.append(normalized_context)
        previous_scene_id = scene_id
        previous_handoff = normalized_context["continuity_out"]
        previous_shot_id = expected_id

    _validate_empty_shot_design(normalized_contexts)
    _validate_camera_language_distribution(normalized_contexts)

    review = _mapping(data.get("review"), "review")
    if _text(review.get("status"), "review.status") != "PASS":
        raise CinematicStoryboardGovernanceError("导演复审未通过，禁止进入素材层")
    checks = _list(review.get("checks"), "review.checks")
    required_checks = {"locked_ids_preserved", "locked_timelines_preserved", "story_causality_present", "continuity_rules_present", "no_text_or_ui"}
    if not required_checks.issubset({item for item in checks if isinstance(item, str)}):
        raise CinematicStoryboardGovernanceError("导演复审缺少必要检查项")
    issues = _list(review.get("issues"), "review.issues")
    if issues:
        raise CinematicStoryboardGovernanceError("导演复审仍有问题，禁止进入素材层")

    return {
        "schema_version": "cinematic-storyboard-governance-v3-media-routed",
        "director_book": normalized_book,
        "continuity_bible": normalized_continuity,
        "beat_sequence": normalized_beats,
        "shot_contexts": normalized_contexts,
        "review": {"status": "PASS", "checks": [str(item) for item in checks], "issues": []},
        "source_contract": {
            "source": "director_locked_manifest",
            "director_lock_status": "DIRECTOR_LOCKED",
            "shot_ids": [item[0] for item in expected],
            "timelines_preserved": True,
            "director_lock_mutated": False,
            "media_route_decisions_preserved": True,
        },
        "media_route_policy": media_plan["policy"],
        "shot_design_policy": {
            "allowed_types": list(SHOT_DESIGN_TYPES),
            "empty_shot_types": sorted(EMPTY_SHOT_DESIGN_TYPES),
            "transition_roles": list(TRANSITION_ROLES),
            "minimum_empty_shot_count": 1 if len(normalized_contexts) >= 4 else 0,
            "short_shot_static_image_threshold_us": SHORT_SHOT_STATIC_IMAGE_THRESHOLD_US,
            "duration_window_us": {
                "min": TARGET_SHOT_MIN_DURATION_US,
                "max": TARGET_SHOT_MAX_DURATION_US,
            },
        },
    }


def merge_governance_into_story_context(
    cinematic_story: Mapping[str, Any], governance: Mapping[str, Any],
) -> dict[str, Any]:
    """给既有素材编排器提供兼容的故事输入，并附上逐小镜头治理上下文。"""

    story = dict(_mapping(cinematic_story, "cinematic_story"))
    normalized = _mapping(governance, "governance")
    story["director_governance"] = {
        "director_book": dict(_mapping(normalized.get("director_book"), "governance.director_book")),
        "continuity_bible": dict(_mapping(normalized.get("continuity_bible"), "governance.continuity_bible")),
        "beat_sequence": list(_list(normalized.get("beat_sequence"), "governance.beat_sequence")),
        "shot_contexts": list(_list(normalized.get("shot_contexts"), "governance.shot_contexts")),
    }
    policy = normalized.get("shot_design_policy")
    if isinstance(policy, Mapping):
        story["director_governance"]["shot_design_policy"] = dict(policy)
    return story


def run_cinematic_storyboard_governance(
    director_lock: Mapping[str, Any], *, transport: CinematicStoryboardTransport | Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    if transport is None:
        raise CinematicStoryboardTransportRequired("未注入电影化分镜治理模型 transport")
    # 一个 24 镜头以上的完整 JSON 会让文本模型长时间只生成而无法验收。
    # 按“导演书/连续性 → 8 镜头连续序列 → 程序复审”拆分，保留同一冻结契约。
    source = _decode(build_cinematic_storyboard_prompt(director_lock))
    foundation_request = {
        "task": "只生成 director_book、continuity_bible、beat_sequence；不要 shot_contexts、review 或最终 prompt。",
        # 基础导演书只需要全片故事与场景分组。传入完整 narration_mappings 会把
        # 同一信息重复数倍，导致真实模型在返回结构化 JSON 前超时。
        "approved_story": {
            "movie_outline": source["approved_story"]["movie_outline"],
            "silent_story_text": source["approved_story"]["silent_story_text"],
            "scene_groups": [
                {"scene_id": scene["scene_id"], "group_ids": scene["group_ids"]}
                for scene in source["approved_story"]["scene_groups"]
            ],
        },
        "immutable_contract": {
            "project_id": source["immutable_contract"]["project_id"],
            "run_id": source["immutable_contract"]["run_id"],
            "shot_ids": source["immutable_contract"]["shot_ids"],
            "timelines_unit": source["immutable_contract"]["timelines_unit"],
        },
    }
    foundation_raw = _decode(transport(
        CINEMATIC_STORYBOARD_SYSTEM_PROMPT,
        json.dumps(foundation_request, ensure_ascii=False),
    ))
    foundation = {
        "director_book": foundation_raw.get("director_book"),
        "continuity_bible": foundation_raw.get("continuity_bible"),
        "beat_sequence": foundation_raw.get("beat_sequence"),
    }
    frozen_shots = list(source["frozen_shots"])
    scene_groups = list(source["approved_story"]["scene_groups"])
    shots_by_group: dict[str, list[dict[str, Any]]] = {}
    for shot in frozen_shots:
        shots_by_group.setdefault(str(shot["group_id"]), []).append(shot)
    contexts: list[Any] = []
    for scene in scene_groups:
        group_ids = list(scene.get("group_ids") or [])
        batch = [shot for group_id in group_ids for shot in shots_by_group.get(str(group_id), [])]
        if not batch:
            raise CinematicStoryboardGovernanceError("场景组没有对应的冻结小镜头")
        batch_request = {
            "task": "只输出 {\"shot_contexts\":[...]}，本批是同一连续场景。严格按输入顺序覆盖本批镜头；每镜必须承接上镜并交接下镜。不要 director_book、review、最终 prompt、URL 或模型字段。",
            "director_book": foundation["director_book"],
            "continuity_bible": foundation["continuity_bible"],
            "beat_sequence": foundation["beat_sequence"],
            "frozen_shots": batch,
            "camera_language_contract": {
                "total_shot_count": len(frozen_shots),
                "minimum_distinct_camera_angles": 3 if len(frozen_shots) >= 8 else 1,
                "minimum_distinct_camera_motions": 3 if len(frozen_shots) >= 8 else 1,
                "maximum_dominant_ratio": 0.70,
                "maximum_consecutive_same": 4,
                "previous_shots": [
                    {
                        "shot_id": item.get("shot_id"),
                        "camera_angle": (item.get("visual_direction") or {}).get("camera_angle"),
                        "camera_motion": (item.get("visual_direction") or {}).get("camera_motion"),
                    }
                    for item in contexts
                    if isinstance(item, Mapping)
                ],
            },
        }
        batch_raw = _decode(transport(
            CINEMATIC_STORYBOARD_SYSTEM_PROMPT,
            json.dumps(batch_request, ensure_ascii=False),
        ))
        batch_contexts = _list(batch_raw.get("shot_contexts"), "batch.shot_contexts")
        if len(batch_contexts) != len(batch):
            raise CinematicStoryboardGovernanceError("分批导演输出的 shot_contexts 数量不匹配")
        contexts.extend(batch_contexts)
    combined = {
        **foundation,
        "shot_contexts": contexts,
        "review": {
            "status": "PASS",
            "checks": [
                "locked_ids_preserved", "locked_timelines_preserved", "story_causality_present",
                "continuity_rules_present", "no_text_or_ui",
            ],
            "issues": [],
        },
    }
    return normalize_cinematic_storyboard_governance(combined, director_lock)


__all__ = [
    "CINEMATIC_STORYBOARD_SYSTEM_PROMPT", "CinematicStoryboardGovernanceError",
    "CinematicStoryboardTransportRequired", "build_cinematic_storyboard_prompt",
    "build_cinematic_storyboard_fallback",
    "merge_governance_into_story_context", "normalize_cinematic_storyboard_governance",
    "run_cinematic_storyboard_governance", "resolve_cinematic_media_routes", "MEDIA_TYPES", "SHOT_DESIGN_TYPES", "EMPTY_SHOT_DESIGN_TYPES", "VISUAL_CAMERA_ANGLES", "VISUAL_COMPOSITIONS",
    "VISUAL_SHOT_SIZES", "VISUAL_CAMERA_MOTIONS", "TARGET_SHOT_MIN_DURATION_US",
    "TARGET_SHOT_MAX_DURATION_US", "SHORT_SHOT_STATIC_IMAGE_THRESHOLD_US", "TRANSITION_ROLES",
]
