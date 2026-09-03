"""素材生产的模型适配与提示词编译契约。

这里不调用任何外部服务。它只把已经锁定的逐镜脚本转换成稳定、可审计的
首帧和图生视频请求，避免控制台、Image 2、Seedance 三处各自拼接提示词。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import re

from .first_frame_prompt_governance import govern_static_frame_fields


SUPPORTED_ASPECT_RATIOS = frozenset({"9:16", "16:9", "1:1", "3:2", "2:3"})

_EXPLICIT_CAMERA_MOTIONS = ("推近", "拉远", "横移", "跟拍", "摇镜", "环绕", "推镜", "拉镜", "移动镜头")
_LOW_DYNAMIC_MARKERS = ("低动态", "低动势", "low", "静态", "空镜", "物件特写", "情绪停顿", "信息展示", "无明显动作")


def resolve_camera_fixed(shot: Mapping[str, object]) -> bool:
    """按冻结镜头信息决定 Seedance 是否开启固定镜头。

    时长只是保护条件，不能单独决定运镜：短动作镜头也可能需要运动，
    长空镜仍应保持稳定。显式 camera_fixed 优先，其次使用低动态和导演运镜意图。
    """
    explicit = shot.get("camera_fixed")
    if isinstance(explicit, bool):
        return explicit

    duration_ms = shot.get("duration_ms")
    if duration_ms is None:
        try:
            duration_ms = float(shot.get("duration_s") or 0) * 1000
        except (TypeError, ValueError):
            duration_ms = 0
    try:
        duration_ms = float(duration_ms)
    except (TypeError, ValueError):
        duration_ms = 0
    if 0 < duration_ms < 3000:
        return True

    dynamic_text = " ".join(
        str(shot.get(key) or "") for key in ("dynamic_level", "media_reason", "shot_content", "semantic_anchor")
    ).lower()
    if any(marker.lower() in dynamic_text for marker in _LOW_DYNAMIC_MARKERS):
        return True

    camera = shot.get("camera_motion")
    camera_values: list[str] = []
    if isinstance(camera, str):
        camera_values.append(camera)
    elif isinstance(camera, list):
        for item in camera:
            if isinstance(item, Mapping):
                camera_values.append(str(item.get("camera_motion") or ""))
            else:
                camera_values.append(str(item))
    camera_text = " ".join(camera_values)
    return not any(marker in camera_text for marker in _EXPLICIT_CAMERA_MOTIONS)


@dataclass(frozen=True)
class AssetModelProfile:
    """模型能力声明；鉴权与 HTTP 细节留在相应 transport 内。"""

    key: str
    display_name: str
    asset_kind: str
    supports_reference_images: bool
    supports_grid: bool = False


MODEL_PROFILES: dict[str, AssetModelProfile] = {
    "image2": AssetModelProfile("image2", "Image 2", "first_frame", True, True),
    "seedance_1_5_pro": AssetModelProfile("seedance_1_5_pro", "Seedance 1.5 Pro", "video", True),
    "seedance_1_0_pro": AssetModelProfile("seedance_1_0_pro", "Seedance 1.0 Pro", "video", True),
}


def normalize_aspect_ratio(value: object, *, default: str = "9:16") -> str:
    ratio = str(value or default).strip().replace(" ", "")
    return ratio if ratio in SUPPORTED_ASPECT_RATIOS else default


def _extract_prompt_value(raw: str, label: str) -> str:
    match = re.search(
        rf"(?:^|[\n，,；;。])\s*{re.escape(label)}\s*[=:：]\s*",
        raw,
    )
    if match is None:
        return ""
    value = raw[match.end():]
    boundary = re.search(r"(?:\n\s*【[^】]+】|[；;。])", value)
    if boundary is not None:
        value = value[:boundary.start()]
    return value.strip()


def _prompt_section(raw: str, prefix: str, end_prefix: str = "") -> str:
    value = raw.split(prefix, 1)[-1] if prefix in raw else ""
    if end_prefix and end_prefix in value:
        value = value.split(end_prefix, 1)[0]
    return value.strip()


def _prompt_field(raw: str, name: str) -> str:
    """读取结构化提示词中一个标签到下一个标签之间的内容。"""

    marker = f"【{name}】"
    if marker not in raw:
        return ""
    return raw.split(marker, 1)[1].split("【", 1)[0].strip()


def _shot_visual_direction_value(shot: Mapping[str, object], key: str) -> str:
    """读取编导逐镜字段；兼容旧脚本把视觉导演书嵌套在 visual_direction。"""

    direct = str(shot.get(key) or "").strip()
    if direct:
        return direct
    direction = shot.get("visual_direction")
    if isinstance(direction, Mapping):
        return str(direction.get(key) or "").strip()
    return ""


def _shot_camera_motion_text(shot: Mapping[str, object]) -> str:
    """把逐镜运镜数组压缩成首帧模型可执行的一行。"""

    camera_motion = shot.get("camera_motion")
    if isinstance(camera_motion, list):
        parts: list[str] = []
        for item in camera_motion:
            if isinstance(item, Mapping):
                camera = str(item.get("camera_motion") or "").strip()
                action = str(item.get("action") or "").strip()
                value = "，".join(part for part in (camera, action) if part)
            else:
                value = str(item or "").strip()
            if value and value not in parts:
                parts.append(value)
        return "；".join(parts)
    return str(camera_motion or "").strip()


def _strip_scene_ratio(scene: str) -> str:
    """场景字段不重复携带旧画幅，避免旧 16:9 文本覆盖本次画幅。"""

    return re.sub(r"^\s*\d+\s*:\s*\d+\s*(?:\([^)]*\))?\s*[，,、 ]*", "", scene).strip()


def _camera_language_fields(raw: str) -> dict[str, str]:
    """从旧版合并的“镜头语言”字段恢复景别、视角和构图。"""

    value = _prompt_field(raw, "镜头语言")
    if not value:
        # 兼容历史逐镜脚本把字段写在“视觉导演书（必须执行）”段落中。
        # 这些脚本仍可能被素材页重新预览，不能因缺少新版标签而回退到默认中景。
        legacy_match = re.search(
            r"视觉导演书(?:（[^）]*）)?\s*[:：]\s*(.*?)(?="
            r"人物连续性硬约束|首帧画面|硬性画面禁令|$)",
            raw,
            flags=re.S,
        )
        if legacy_match:
            value = legacy_match.group(1).strip()
    if not value:
        return {}
    fields: dict[str, str] = {}
    for label in ("景别", "视角", "机位", "构图"):
        match = re.search(
            rf"(?:^|[，,；;])\s*{label}\s*[=:：]\s*([^，,；;]+)",
            value,
        )
        if match:
            fields[label] = match.group(1).strip()
    if fields:
        return fields
    # 旧模板没有字段名时，约定顺序仍是：景别、机位/视角、构图。
    parts = [part.strip() for part in re.split(r"[，,；;]", value) if part.strip()]
    if parts:
        fields["景别"] = parts[0]
    if len(parts) > 1:
        fields["视角"] = parts[1]
    if len(parts) > 2:
        fields["构图"] = "，".join(parts[2:])
    return fields


def compile_first_frame_prompt_audit(
    shot: Mapping[str, object], *, aspect_ratio: object = "9:16",
) -> dict[str, object]:
    """编译静态首帧并返回不可变源数据之外的审计信息。"""

    raw = str(shot.get("image2_prompt") or shot.get("first_frame_prompt") or "").strip()
    if not raw:
        return {
            "prompt": "",
            "warnings": [],
            "source_prompt": "",
            "compiled_fields": {},
        }
    visual_guidance = shot.get("visual_guidance")

    # A compiled prompt normally starts with ``【画幅】``.  Tests and old
    # persisted snapshots can begin directly at ``【场景】``; both are the same
    # structured contract and must not fall through to legacy prose parsing.
    structured = "【场景】" in raw and "【主体与动作】" in raw
    camera_language = _camera_language_fields(raw)
    first_frame = _prompt_section(raw, "首帧画面：", "硬性画面禁令：")
    scene = _prompt_field(raw, "场景") if structured else first_frame.split("。", 1)[0].strip()
    scene = _strip_scene_ratio(scene)
    scene = re.sub(r"^\s*(?:\d+\s*:\s*\d+\s*)?(?:竖版|横版|方形)\s*[，,、 ]*", "", scene)
    action = (
        _prompt_field(raw, "主体与动作") if structured else _extract_prompt_value(raw, "必须可见")
    ) or (
        _extract_prompt_value(raw, "无对白动作")
        if not structured else ""
    ) or (
        str(shot.get("shot_content") or "").strip()
    )
    action = action or (
        _extract_prompt_value(raw, "无对白动作")
        if structured else ""
    ) or (
        str(shot.get("must_show") or "").strip()
    )
    shot_size = (
        _shot_visual_direction_value(shot, "shot_size")
        or _extract_prompt_value(raw, "景别")
        or camera_language.get("景别", "")
        or "中景"
    )
    viewpoint = (
        _shot_visual_direction_value(shot, "viewpoint")
        or _shot_visual_direction_value(shot, "camera_angle")
        or _extract_prompt_value(raw, "机位")
        or _extract_prompt_value(raw, "视角")
        or camera_language.get("视角", "")
        or camera_language.get("机位", "")
        or "平视"
    )
    composition = (
        _shot_visual_direction_value(shot, "composition")
        or _extract_prompt_value(raw, "构图")
        or camera_language.get("构图", "")
        or "主体明确、单一动作"
    )
    staging = (
        _shot_visual_direction_value(shot, "staging")
        or _extract_prompt_value(raw, "调度")
    )
    motion = (
        _shot_camera_motion_text(shot)
        or _prompt_field(raw, "镜头动势")
        or _prompt_field(raw, "运镜")
    )
    lighting = (
        _prompt_field(raw, "光线与氛围") if structured else _extract_prompt_value(raw, "光影")
    ) or "自然电影光，明暗层次清晰"
    negative = (
        _prompt_field(raw, "负面约束")
        if structured else _prompt_section(raw, "硬性画面禁令：")
    ) or "无可读文字、字母、数字、品牌标识、水印、界面 UI、字幕、拼贴分格"
    governed = govern_static_frame_fields(
        scene=scene or str(shot.get("semantic_anchor") or "叙事场景"),
        action=action,
        motion=motion,
        staging=staging,
        shot_size=shot_size,
        frame_intent=shot.get("semantic_anchor") or shot.get("shot_content"),
        key_prop=shot.get("key_prop") or shot.get("key_props"),
    )
    # An empty projected action is meaningful: it means the source was an
    # internal continuity reference that cannot be rendered truthfully.
    action = str(governed["action"] or "").strip()
    motion = str(governed["static_camera_description"] or "").strip()
    staging = str(governed["staging"] or "").strip()
    reference = shot.get("first_frame_reference") or {}
    has_reference = isinstance(reference, Mapping) and bool(reference.get("urls"))
    continuity = (
        "严格保持参考图的画风、色彩、角色脸型、发型与服装一致"
        if has_reference else "主角形象、服装与同组镜头保持连续一致"
    )
    ratio = normalize_aspect_ratio(aspect_ratio)
    ratio_label = {"9:16": "竖版", "16:9": "横版", "1:1": "方形"}.get(ratio, "画幅")
    fields = [
        ("画幅", f"{ratio} {ratio_label}"),
        ("参考一致性", continuity),
        ("主体与动作", action),
        ("场景", scene or str(shot.get("semantic_anchor") or "叙事场景")),
        # 这些字段必须逐镜显式输出，不能再只写成一段不可核对的“镜头语言”。
        ("镜头语言", f"景别={shot_size}；视角={viewpoint}；构图={composition}"),
        ("镜头动势", motion or "固定镜头，保持首帧构图稳定"),
        ("调度", staging),
        ("光线与氛围", lighting),
        ("画面质量", "电影感构图，主体清晰，画面层次明确，单一核心动作"),
        ("负面约束", negative),
    ]
    prompt = "\n".join(f"【{name}】{value.strip('。； ')}" for name, value in fields if value)
    # Visual guidance is an explicit user input contract. Historical task
    # snapshots may contain model output without provenance; carrying that
    # output into a new first-frame request would silently resurrect stale
    # attention instructions.
    if (
        isinstance(visual_guidance, Mapping)
        and str(visual_guidance.get("source") or "").strip() == "explicit_user"
    ):
        reference_usage = str(visual_guidance.get("reference_usage") or "").strip()
        positive = str(visual_guidance.get("positive_prompt") or "").strip()
        negative_guidance = str(visual_guidance.get("negative_prompt") or "").strip()
        additions = []
        if reference_usage:
            additions.append(f"【参考图增强约束】{reference_usage}")
        if positive:
            additions.append(f"【视觉大模型增强】{positive}")
        if negative_guidance:
            additions.append(f"【视觉大模型负面约束】{negative_guidance}")
        if additions:
            prompt += "\n" + "\n".join(additions)
    return {
        "prompt": prompt,
        "warnings": list(governed["warnings"]),
        "source_prompt": raw,
        "compiled_fields": {
            "scene": scene or str(shot.get("semantic_anchor") or "叙事场景"),
            "action": action,
            "static_camera_description": motion,
            "staging": staging,
            "shot_size": shot_size,
            "viewpoint": viewpoint,
            "composition": composition,
            "lighting": lighting,
        },
    }


def compile_first_frame_prompt(shot: Mapping[str, object], *, aspect_ratio: object = "9:16") -> str:
    """编译 Image 2 的单镜提示词，并显式传递编导镜头语言。"""

    return str(compile_first_frame_prompt_audit(shot, aspect_ratio=aspect_ratio)["prompt"])


def compile_video_prompt(shot: Mapping[str, object]) -> str:
    """编译 Seedance 图生视频提示词，仅保留动作、镜头和连续性执行信息。"""
    content = str(shot.get("shot_content") or "").strip()
    motion_seed = str(shot.get("motion_seed") or "").strip()
    camera = shot.get("camera_motion")
    camera_text = "、".join(str(item).strip() for item in camera if str(item).strip()) if isinstance(camera, list) else ""
    parts = [part for part in (content, motion_seed, camera_text) if part]
    return "；".join(parts)


def build_first_frame_request(shot: Mapping[str, object], *, aspect_ratio: object) -> dict[str, Any]:
    """返回 provider-neutral 的首帧请求，供宫格编排器再组合。"""
    reference = shot.get("first_frame_reference") or {}
    urls = list(reference.get("urls") or []) if isinstance(reference, Mapping) else []
    compiled = compile_first_frame_prompt_audit(shot, aspect_ratio=aspect_ratio)
    return {
        "model": "image2",
        "aspect_ratio": normalize_aspect_ratio(aspect_ratio),
        "prompt": compiled["prompt"],
        "reference_urls": [str(url) for url in urls if str(url).strip()],
        "prompt_audit": {
            "warnings": list(compiled["warnings"]),
            "compiled_fields": dict(compiled["compiled_fields"]),
        },
    }


def build_video_request(shot: Mapping[str, object]) -> dict[str, Any]:
    """返回 provider-neutral 的图生视频请求；时长仍以剪辑坑位毫秒为准。"""
    return {
        # auto 的真实优先级由 SeedanceHTTPTransport 执行；这里的模型值
        # 代表素材层默认选择，不能把 1.0 直接固化成唯一能力。
        "model": "seedance_1_5_pro",
        "prompt": compile_video_prompt(shot),
        "duration_ms": int(round(float(shot.get("duration_s") or 0) * 1000)),
        "camera_fixed": resolve_camera_fixed(shot),
    }


__all__ = [
    "AssetModelProfile", "MODEL_PROFILES", "SUPPORTED_ASPECT_RATIOS",
    "normalize_aspect_ratio", "resolve_camera_fixed", "compile_first_frame_prompt", "compile_video_prompt",
    "build_first_frame_request", "build_video_request",
]
