"""1256 工作流“解说*”节点的离线输入/输出契约实现。

该节点对应 capcut-mate 的 ``add_audios`` 接口。真正的接口会读取草稿、
下载音频并写入草稿；本地默认禁止这些外部副作用，只保留已读到的请求
校验、音频项归一化和输出契约。需要执行器时必须由测试或上层显式注入。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any


class AudioInfoValidationError(ValueError):
    """与 add_audios Schema/服务校验对应的输入错误。"""


class AddAudiosTransportRequired(RuntimeError):
    """未注入草稿写入执行器时，阻止真实外部副作用。"""


def _parse_audio_infos(audio_infos: str) -> list[Any]:
    try:
        data = json.loads(audio_infos)
    except json.JSONDecodeError as exc:
        raise AudioInfoValidationError(f"audio_infos JSON parse error: {exc.msg}") from exc

    if not isinstance(data, list):
        raise AudioInfoValidationError("audio_infos should be a list")
    return data


def _validate_schema_audio_infos(data: list[Any]) -> None:
    """复现 capcut-mate Schema 中的 http/https URL 校验。"""

    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise AudioInfoValidationError(f"audio_infos[{index}] should be an object")
        audio_url = item.get("audio_url")
        if not isinstance(audio_url, str) or not audio_url.startswith(("http://", "https://")):
            raise AudioInfoValidationError(
                f"audio_infos[{index}].audio_url must start with http:// or https://"
            )


def _validate_item_type(item: Any, index: int) -> None:
    if not isinstance(item, dict):
        raise AudioInfoValidationError(f"the {index}th item should be a dict")


def _validate_required_fields(item: dict[str, Any], index: int) -> None:
    required_fields = ["audio_url", "start", "end"]
    missing_fields = [field for field in required_fields if field not in item]
    if missing_fields:
        raise AudioInfoValidationError(
            f"the {index}th item is missing required fields: {', '.join(missing_fields)}"
        )


def _create_processed_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "audio_url": item["audio_url"],
        "duration": item.get("duration"),
        "start": item["start"],
        "end": item["end"],
        "volume": item.get("volume", 1.0),
        "audio_effect": item.get("audio_effect", None),
    }


def _validate_numeric_ranges(item: dict[str, Any], index: int) -> None:
    if item["volume"] < 0.0 or item["volume"] > 2.0:
        item["volume"] = 1.0

    if not isinstance(item["start"], (int, float)) or item["start"] < 0:
        raise AudioInfoValidationError(f"the {index}th item has invalid start time")

    if not isinstance(item["end"], (int, float)) or item["end"] <= item["start"]:
        raise AudioInfoValidationError(f"the {index}th item has invalid end time")

    item["start"] = int(item["start"])
    item["end"] = int(item["end"])
    if item["end"] <= item["start"]:
        raise AudioInfoValidationError(f"the {index}th item has invalid end time")

    if item["duration"] is not None and item["duration"] <= 0:
        raise AudioInfoValidationError(f"the {index}th item has invalid duration")


def normalize_audio_infos(audio_infos: str) -> list[dict[str, Any]]:
    """解析并归一化音频项，保持已读到的服务层字段顺序和规则。"""

    data = _parse_audio_infos(audio_infos)
    _validate_schema_audio_infos(data)
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        _validate_item_type(item, index)
        _validate_required_fields(item, index)
        processed = _create_processed_item(item)
        _validate_numeric_ranges(processed, index)
        normalized.append(processed)
    return normalized


def _resolve_params(params: Any) -> Mapping[str, Any]:
    value = getattr(params, "input", params)
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, Mapping) and isinstance(value.get("input"), Mapping):
        value = value["input"]
    if isinstance(value, Mapping) and isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value, Mapping) and isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    if not isinstance(value, Mapping):
        raise TypeError("解说* 输入必须是对象")
    return value


def run_add_audios(
    params: Any,
    *,
    executor: Callable[[str, str, list[dict[str, Any]]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """执行本节点的本地契约边界；真实草稿写入必须显式注入 executor。"""

    value = _resolve_params(params)
    draft_url = value["draft_url"]
    audio_infos = value["audio_infos"]
    normalized = normalize_audio_infos(audio_infos)
    if executor is None:
        raise AddAudiosTransportRequired("未注入 add_audios 草稿写入执行器")

    result = executor(draft_url, audio_infos, normalized)
    if not isinstance(result, Mapping):
        raise AudioInfoValidationError("add_audios 执行器输出必须是对象")

    audio_ids = result.get("audio_ids", [])
    output_draft_url = result.get("draft_url", draft_url)
    track_id = result.get("track_id", "")
    if not isinstance(audio_ids, list) or not all(isinstance(item, str) for item in audio_ids):
        raise AudioInfoValidationError("audio_ids 必须是字符串数组")
    if not isinstance(output_draft_url, str) or not isinstance(track_id, str):
        raise AudioInfoValidationError("draft_url 和 track_id 必须是字符串")

    # 按原始 YAML node_outputs 顺序返回。
    return {"audio_ids": list(audio_ids), "draft_url": output_draft_url, "track_id": track_id}


__all__ = [
    "AddAudiosTransportRequired",
    "AudioInfoValidationError",
    "normalize_audio_infos",
    "run_add_audios",
]
