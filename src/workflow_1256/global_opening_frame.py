"""整篇视频总首帧的编导层契约与模型调用边界。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class GlobalOpeningFrameValidationError(ValueError):
    """总首帧编导结果不符合契约。"""


class GlobalOpeningFrameTransportRequired(RuntimeError):
    """未注入真实总首帧编导模型。"""


GlobalOpeningFrameTransport = Callable[[str, str], Any]
REQUIRED_FIELDS = (
    "visual_thesis",
    "dramatic_conflict",
    "spectacle_anchor",
    "composition",
    "lighting_mood",
    "image_prompt",
)


GLOBAL_OPENING_FRAME_SYSTEM_PROMPT = """你是整条短视频的总首帧视觉编导，不是普通分镜提示词编写器。

你必须根据完整原文和已经审核的电影故事，设计一张概括整篇视频的“电影海报式首帧”。它只是一张单独的、连续空间中的电影静帧，不是多宫格、不是分镜表、不是拼贴，也不是把每段文案逐句罗列在画面里。

画面必须同时具备：主角锚点、整篇文案的核心冲突、一个可一眼读懂的视觉隐喻、具有奇观性的尺度或空间、以及能引出后续视频的悬念。需要把抽象知识观点转换成主角正在经历的具体处境和可见关系。

硬性约束：
- 主角姓名和形象必须遵守“固定主角锚点”；不得擅自改名或增加第二位主角。
- 画幅必须使用输入的 aspect_ratio。
- 必须是一个连续场景，一个决定性瞬间；禁止分屏、拼贴、多宫格、海报边框和多个互不相连的场景。
- 默认无文字、无标题、无字幕、无数字特写、无 UI、无水印；数字和抽象观点用物件、尺度、动作和光线表达。
- image_prompt 必须是可以直接交给图像生成模型的中文提示词，包含主体、空间、冲突、构图、光线、风格和禁止项。

只输出合法 JSON：
{
  "visual_thesis":"一眼看懂的整篇视觉命题",
  "dramatic_conflict":"画面中正在发生的核心冲突",
  "spectacle_anchor":"负责制造奇观感的具体视觉锚点",
  "composition":"主体、前景、中景、背景和视线动线",
  "lighting_mood":"光线和情绪",
  "must_include":["必须出现的视觉锚点"],
  "avoid":["必须避免的内容"],
  "image_prompt":"可直接用于图像生成模型的完整中文提示词"
}"""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GlobalOpeningFrameValidationError(f"{field} 必须是非空字符串")
    return value.strip()


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise GlobalOpeningFrameValidationError(f"{field} 必须是非空字符串数组")
    return [item.strip() for item in value]


def _decode(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise GlobalOpeningFrameValidationError("总首帧编导响应必须是 JSON 对象或 JSON 字符串")
    cleaned = value.strip().replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GlobalOpeningFrameValidationError("总首帧编导响应不是合法 JSON") from exc
    if not isinstance(result, Mapping):
        raise GlobalOpeningFrameValidationError("总首帧编导响应根节点必须是对象")
    return result


def build_global_opening_frame_prompt(
    source_text: str,
    cinematic_story: Mapping[str, Any],
    *,
    aspect_ratio: str = "9:16",
    protagonist_name: str = "林夏",
) -> str:
    source = _text(source_text, "source_text")
    if not isinstance(cinematic_story, Mapping) or not cinematic_story:
        raise GlobalOpeningFrameValidationError("cinematic_story 必须是非空对象")
    ratio = _text(aspect_ratio, "aspect_ratio")
    protagonist = _text(protagonist_name, "protagonist_name")
    return (
        f"固定主角锚点：{protagonist}\n"
        f"项目画幅：{ratio}\n\n"
        "完整原文（必须整体理解，不得只看某一段）：\n"
        + source
        + "\n\n已经生成的电影故事（用于保持叙事连续性）：\n"
        + json.dumps(dict(cinematic_story), ensure_ascii=False, indent=2)
        + "\n\n请输出整篇视频总首帧的 JSON。"
    )


def normalize_global_opening_frame(value: Any) -> dict[str, Any]:
    data = _decode(value)
    result = {field: _text(data.get(field), field) for field in REQUIRED_FIELDS}
    result["must_include"] = _string_list(data.get("must_include"), "must_include")
    result["avoid"] = _string_list(data.get("avoid"), "avoid")
    return result


def run_global_opening_frame_writer(
    source_text: str,
    cinematic_story: Mapping[str, Any],
    *,
    aspect_ratio: str = "9:16",
    protagonist_name: str = "林夏",
    transport: GlobalOpeningFrameTransport | None = None,
) -> dict[str, Any]:
    if transport is None:
        raise GlobalOpeningFrameTransportRequired(
            "未注入总首帧编导模型；当前不会擅自调用外部模型"
        )
    prompt = build_global_opening_frame_prompt(
        source_text,
        cinematic_story,
        aspect_ratio=aspect_ratio,
        protagonist_name=protagonist_name,
    )
    result = normalize_global_opening_frame(transport(GLOBAL_OPENING_FRAME_SYSTEM_PROMPT, prompt))
    result["aspect_ratio"] = aspect_ratio
    result["protagonist_name"] = protagonist_name
    return result


__all__ = [
    "GLOBAL_OPENING_FRAME_SYSTEM_PROMPT",
    "GlobalOpeningFrameTransportRequired",
    "GlobalOpeningFrameValidationError",
    "build_global_opening_frame_prompt",
    "normalize_global_opening_frame",
    "run_global_opening_frame_writer",
]
