"""8364 工作流到 CapCut Mate 的节点级字段映射。

客户端只负责 HTTP；本模块负责按工作流节点契约投影响应。所有写入节点都
要求服务端返回新的有效 ``draft_url``，不会在缺失时偷偷回填旧地址。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .add_audios import run_add_audios
from .add_keyframes import run_add_keyframes
from .aigc_animation import run_aigc_animation
from .capcut_mate_transport import CapCutMateClient
from .create_draft import run_create_draft
from .digital_human import run_digital_human
from .save_draft import run_save_draft


class CapCutNodeContractError(ValueError):
    """CapCut Mate 响应无法投影到 8364 节点契约。"""


CAPCUT_NODE_ENDPOINTS = {
    "182422": "create_draft",
    "143635": "save_draft",
    "115723": "video_infos",
    "152553": "video_infos",
    "165901": "audio_timelines",
    "111882": "caption_infos",
    "116592": "audio_infos",
    "191683": "audio_infos",
    "135313": "add_audios",
    "110576": "add_audios",
    "174651": "add_videos",
    "116930": "add_videos",
    "179989": "add_captions",
    "115137": "add_keyframes",
    "1962357": "add_keyframes",
    "187358": "add_effects",
    "1922689": "add_effects",
    "151394": "keyframes_infos",
    "1904923": "keyframes_infos",
    "197721": "effect_infos",
    "1765956": "effect_infos",
}


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CapCutNodeContractError(f"{field} 必须是非空字符串")
    return value


def _string_array(value: Any, field: str, *, default: list[str] | None = None) -> list[str]:
    if value is None and default is not None:
        return list(default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CapCutNodeContractError(f"{field} 必须是字符串数组")
    return list(value)


def _draft_response(response: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    draft_url = _required_string(response.get("draft_url"), "draft_url")
    result: dict[str, Any] = {"draft_url": draft_url}
    for field in fields:
        if field.endswith("_ids"):
            result[field] = _string_array(response.get(field), field, default=[])
        elif field == "segment_infos":
            value = response.get(field, [])
            if not isinstance(value, list):
                raise CapCutNodeContractError("segment_infos 必须是数组")
            result[field] = list(value)
        else:
            result[field] = response.get(field, "")
            if not isinstance(result[field], str):
                raise CapCutNodeContractError(f"{field} 必须是字符串")
    return result


def _info_response(response: Mapping[str, Any]) -> dict[str, str]:
    infos = response.get("infos")
    if not isinstance(infos, str):
        raise CapCutNodeContractError("CapCut Mate infos 必须是 JSON 字符串")
    return {"infos": infos}


def _timeline_response(response: Mapping[str, Any]) -> dict[str, Any]:
    timelines = response.get("timelines")
    all_timelines = response.get("all_timelines", timelines)
    if not isinstance(timelines, list) or not isinstance(all_timelines, list):
        raise CapCutNodeContractError("timelines/all_timelines 必须是数组")
    return {"all_timelines": list(all_timelines), "timelines": list(timelines)}


class CapCutNodeAdapter:
    """把 CapCut Mate 客户端绑定到 8364 节点级输入输出。"""

    def __init__(self, client: CapCutMateClient) -> None:
        self.client = client

    def create_draft(self, params: Any) -> dict[str, str]:
        return run_create_draft(params, transport=self.client.create_draft)

    def save_draft(self, params: Any) -> dict[str, str]:
        return run_save_draft(params, transport=self.client.save_draft)

    def add_audios(self, params: Any) -> dict[str, Any]:
        return run_add_audios(params, executor=self.client.add_audios)

    def add_videos(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return run_aigc_animation(
            params,
            executor=lambda draft_url, video_infos, options: self.client.add_videos(
                draft_url,
                video_infos,
                **dict(options),
            ),
        )

    def add_digital_human(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """按“数字人*”(116930)的独立契约调用同一 add_videos 接口。"""

        return run_digital_human(
            params,
            executor=lambda draft_url, video_infos, options: self.client.add_videos(
                draft_url,
                video_infos,
                **dict(options),
            ),
        )

    def add_captions(self, params: Mapping[str, Any]) -> dict[str, Any]:
        draft_url = _required_string(params.get("draft_url"), "draft_url")
        captions = params.get("captions", "")
        captions = captions if isinstance(captions, str) else json.dumps(captions, ensure_ascii=False)
        passthrough = {
            key: params[key]
            for key in (
                "text_color", "border_color", "alignment", "alpha", "font", "font_size",
                "letter_spacing", "line_spacing", "scale_x", "scale_y", "transform_x",
                "transform_y", "style_text", "underline", "italic", "bold", "has_shadow",
                "shadow_info", "text_effect",
            )
            if key in params
        }
        response = self.client.add_captions(draft_url, captions, **passthrough)
        return _draft_response(response, ("segment_ids", "segment_infos", "text_ids", "track_id"))

    def add_keyframes(self, params: Mapping[str, Any]) -> dict[str, str]:
        return run_add_keyframes(params, executor=self.client.add_keyframes)

    def add_effects(self, params: Mapping[str, Any]) -> dict[str, Any]:
        draft_url = _required_string(params.get("draft_url"), "draft_url")
        effect_infos = params.get("effect_infos", "")
        effect_infos = effect_infos if isinstance(effect_infos, str) else json.dumps(effect_infos, ensure_ascii=False)
        response = self.client.add_effects(draft_url, effect_infos)
        return _draft_response(response, ("effect_ids", "segment_ids", "track_id"))

    def keyframes_infos(self, params: Mapping[str, Any]) -> dict[str, str]:
        """运行关键帧信息节点，保留 CapCut Mate 的 JSON 字符串输出。"""
        return self.infos("keyframes_infos", params)

    def infos(self, endpoint: str, params: Mapping[str, Any]) -> dict[str, str]:
        allowed = {"video_infos", "audio_infos", "caption_infos", "effect_infos", "keyframes_infos"}
        if endpoint not in allowed:
            raise CapCutNodeContractError(f"不支持的信息节点 endpoint：{endpoint}")
        return _info_response(self.client.call(endpoint, params))

    def audio_infos(self, params: Mapping[str, Any]) -> dict[str, str]:
        return self.infos("audio_infos", params)

    def caption_infos(self, params: Mapping[str, Any]) -> dict[str, str]:
        return self.infos("caption_infos", params)

    def audio_timelines(self, params: Mapping[str, Any]) -> dict[str, Any]:
        links = _string_array(params.get("links"), "links")
        return _timeline_response(self.client.call("audio_timelines", {"links": links}))


__all__ = ["CAPCUT_NODE_ENDPOINTS", "CapCutNodeAdapter", "CapCutNodeContractError"]
