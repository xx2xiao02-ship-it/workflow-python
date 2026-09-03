"""8364 节点 102833“全文视觉意图导演”的契约适配器。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class VisualIntentValidationError(ValueError):
    """节点输入或模型响应不符合 8364 YAML 契约。"""


class VisualIntentTransportRequired(RuntimeError):
    """未注入模型 transport。"""


@dataclass(frozen=True)
class VisualIntentRequest:
    segments: list[str]
    director_plan: Mapping[str, Any]


class VisualIntentTransport(Protocol):
    def __call__(self, request: VisualIntentRequest) -> Mapping[str, Any]: ...


def _parse(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise VisualIntentValidationError("全文视觉意图导演输入 JSON 无效") from exc


def _resolve(params: Any) -> Mapping[str, Any]:
    value = _parse(params)
    if not isinstance(value, Mapping):
        raise VisualIntentValidationError("全文视觉意图导演输入必须是对象")
    for key in ("params", "_input"):
        if isinstance(value.get(key), Mapping):
            value = value[key]
    return value


def build_request(params: Any) -> VisualIntentRequest:
    value = _resolve(params)
    segments = _parse(value.get("segments"))
    director_plan = _parse(value.get("director_plan"))
    if not isinstance(segments, list) or not all(isinstance(item, str) for item in segments):
        raise VisualIntentValidationError("segments 必须是字符串数组")
    if not segments:
        raise VisualIntentValidationError("segments 不能为空")
    if not isinstance(director_plan, Mapping):
        raise VisualIntentValidationError("director_plan 必须是对象")
    return VisualIntentRequest(list(segments), dict(director_plan))


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise VisualIntentValidationError(f"{path} 必须是字符串")
    return value


def normalize_response(response: Mapping[str, Any], *, segment_count: int) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise VisualIntentValidationError("全文视觉意图导演响应必须是对象")

    raw_items = response.get("items")
    if not isinstance(raw_items, list):
        raise VisualIntentValidationError("items 必须是数组")
    items: list[dict[str, str]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise VisualIntentValidationError(f"items[{index}] 必须是对象")
        items.append({
            "visual_core": _string(item.get("visual_core"), f"items[{index}].visual_core"),
            "visual_story": _string(item.get("visual_story"), f"items[{index}].visual_story"),
        })
    if len(items) != segment_count:
        raise VisualIntentValidationError("items 与 segments 数量必须一致")

    raw_cues = response.get("music_cues")
    if not isinstance(raw_cues, list):
        raise VisualIntentValidationError("music_cues 必须是数组")
    music_cues: list[dict[str, Any]] = []
    for index, cue in enumerate(raw_cues):
        if not isinstance(cue, Mapping):
            raise VisualIntentValidationError(f"music_cues[{index}] 必须是对象")
        energy = cue.get("energy")
        if isinstance(energy, bool) or not isinstance(energy, int):
            raise VisualIntentValidationError(f"music_cues[{index}].energy 必须是整数")
        music_cues.append({
            "emotion": _string(cue.get("emotion"), f"music_cues[{index}].emotion"),
            "energy": energy,
            "music_direction": _string(
                cue.get("music_direction"), f"music_cues[{index}].music_direction"
            ),
            "music_role": _string(cue.get("music_role"), f"music_cues[{index}].music_role"),
            "transition_to_next": _string(
                cue.get("transition_to_next"), f"music_cues[{index}].transition_to_next"
            ),
        })

    return {
        "items": items,
        "music_cues": music_cues,
        "reasoning_content": _string(response.get("reasoning_content"), "reasoning_content"),
    }


def run_visual_intent(
    params: Any,
    *,
    transport: VisualIntentTransport
    | Callable[[VisualIntentRequest], Mapping[str, Any]]
    | None = None,
) -> dict[str, Any]:
    request = build_request(params)
    if transport is None:
        raise VisualIntentTransportRequired(
            "未配置 102833 全文视觉意图导演模型 transport"
        )
    return normalize_response(transport(request), segment_count=len(request.segments))


__all__ = [
    "VisualIntentRequest",
    "VisualIntentTransportRequired",
    "VisualIntentValidationError",
    "build_request",
    "normalize_response",
    "run_visual_intent",
]
