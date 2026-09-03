"""素材层适配器：统一视频、音频和字幕的时间线与来源。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .contracts import AssetManifest, AssetRecord, LayerContractError


def _parse_list(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise LayerContractError(f"{field} JSON 无效") from exc
    if not isinstance(value, list):
        raise LayerContractError(f"{field} 必须是数组或 JSON 数组字符串")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise LayerContractError(f"{field}[{index}] 必须是对象")
        result.append(dict(item))
    return result


def _timeline(item: Mapping[str, Any], field: str) -> tuple[int, int]:
    start = item.get("start", item.get("start_us"))
    end = item.get("end", item.get("end_us"))
    if isinstance(start, bool) or not isinstance(start, int):
        raise LayerContractError(f"{field}.start 必须是整数微秒")
    if isinstance(end, bool) or not isinstance(end, int):
        raise LayerContractError(f"{field}.end 必须是整数微秒")
    if start < 0 or end <= start:
        raise LayerContractError(f"{field} 时间线无效：end 必须大于 start")
    return start, end


def _add_media(
    assets: list[AssetRecord],
    value: Any,
    *,
    field: str,
    kind: str,
    source_node: str,
    uri_fields: tuple[str, ...],
) -> None:
    for index, item in enumerate(_parse_list(value, field)):
        uri = next((item.get(name) for name in uri_fields if item.get(name)), "")
        if not isinstance(uri, str) or not uri.strip():
            raise LayerContractError(f"{field}[{index}] 缺少素材地址")
        start, end = _timeline(item, f"{field}[{index}]")
        assets.append(
            AssetRecord(
                asset_id=f"{kind}:{source_node}:{index}",
                kind=kind,
                uri=uri,
                start_us=start,
                end_us=end,
                duration_us=end - start,
                source_node=source_node,
                index=index,
                metadata={
                    key: value
                    for key, value in item.items()
                    if key
                    not in (set(uri_fields) | {"start", "end", "start_us", "end_us"})
                },
            )
        )


def build_asset_manifest(
    inputs: Mapping[str, Any],
    *,
    director_segments: list[str] | None = None,
) -> AssetManifest:
    """把执行插件输出转换为统一 AssetManifest。

    支持的输入键：``aigc_video_infos``、``digital_human_video_infos``、
    ``narration_audio_infos``、``bgm_audio_infos``、``captions``。每项可以是
    数组，也可以是 Coze 常见的 JSON 字符串数组。
    """

    if not isinstance(inputs, Mapping):
        raise LayerContractError("asset_inputs 必须是对象")
    assets: list[AssetRecord] = []
    _add_media(
        assets,
        inputs.get("aigc_video_infos", inputs.get("video_infos")),
        field="aigc_video_infos",
        kind="video",
        source_node="AIGC动画*",
        uri_fields=("video_url", "url"),
    )
    _add_media(
        assets,
        inputs.get("digital_human_video_infos"),
        field="digital_human_video_infos",
        kind="video",
        source_node="数字人*",
        uri_fields=("video_url", "url"),
    )
    _add_media(
        assets,
        inputs.get("narration_audio_infos", inputs.get("audio_infos")),
        field="narration_audio_infos",
        kind="audio",
        source_node="解说*",
        uri_fields=("audio_url", "url", "link"),
    )
    _add_media(
        assets,
        inputs.get("bgm_audio_infos"),
        field="bgm_audio_infos",
        kind="audio",
        source_node="背景音乐*",
        uri_fields=("audio_url", "url", "link"),
    )

    captions = inputs.get("captions")
    if captions is not None:
        for index, item in enumerate(_parse_list(captions, "captions")):
            start, end = _timeline(item, f"captions[{index}]")
            text = item.get("text", item.get("content", ""))
            if not isinstance(text, str):
                raise LayerContractError(f"captions[{index}].text 必须是字符串")
            assets.append(
                AssetRecord(
                    asset_id=f"caption:字幕:{index}",
                    kind="caption",
                    uri="",
                    start_us=start,
                    end_us=end,
                    duration_us=end - start,
                    source_node="字幕*",
                    index=index,
                    metadata={"text": text},
                )
            )

    duration_us = max((asset.end_us for asset in assets), default=0)
    return AssetManifest(
        assets=assets,
        duration_us=duration_us,
        director_segments=list(director_segments or []),
    )


__all__ = ["build_asset_manifest"]
