"""1256 节点 187358「添加特效」的 Python 契约实现。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse


class AddEffectsTransportRequired(RuntimeError):
    pass


class AddEffectsValidationError(ValueError):
    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__("无效的特效信息，请检查effect_infos字段值是否正确")


def parse_effects_data(json_str: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise AddEffectsValidationError(f"JSON parse error: {exc.msg}") from exc
    if not isinstance(data, list):
        raise AddEffectsValidationError("effect_infos should be a list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise AddEffectsValidationError(f"the {index}th item should be a dict")
        required = ["effect_title", "start", "end"]
        missing = [field for field in required if field not in item]
        if missing:
            raise AddEffectsValidationError(f"the {index}th item is missing required fields: {', '.join(missing)}")
        processed = {"effect_title": str(item["effect_title"]), "start": item["start"], "end": item["end"]}
        if not isinstance(processed["start"], (int, float)) or processed["start"] < 0:
            raise AddEffectsValidationError(f"the {index}th item has invalid start time")
        if not isinstance(processed["end"], (int, float)) or processed["end"] <= processed["start"]:
            raise AddEffectsValidationError(f"the {index}th item has invalid end time")
        if len(processed["effect_title"].strip()) == 0:
            raise AddEffectsValidationError(f"the {index}th item has invalid effect_title")
        processed["start"] = int(processed["start"])
        processed["end"] = int(processed["end"])
        result.append(processed)
    return result


def _resolve(params: Any) -> Mapping[str, Any]:
    value = getattr(params, "input", params)
    if isinstance(value, str): value = json.loads(value)
    if isinstance(value, Mapping) and isinstance(value.get("input"), Mapping): value = value["input"]
    if isinstance(value, Mapping) and isinstance(value.get("params"), Mapping): value = value["params"]
    if not isinstance(value, Mapping): raise TypeError("添加特效输入必须是对象")
    return value


def _draft_id(url: str) -> str:
    return parse_qs(urlparse(url).query).get("draft_id", [""])[0]


def run_add_effects(params: Any, *, executor: Callable[[str, str], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    value = _resolve(params)
    draft_url = value["draft_url"]
    effect_infos = value["effect_infos"]
    if not isinstance(draft_url, str) or not draft_url or not _draft_id(draft_url):
        raise ValueError("无效的草稿URL")
    if not isinstance(effect_infos, str): effect_infos = json.dumps(effect_infos, ensure_ascii=False)
    parsed = parse_effects_data(effect_infos)
    if not parsed: raise AddEffectsValidationError()
    if executor is None: raise AddEffectsTransportRequired("未注入add_effects草稿写入executor")
    response = executor(draft_url, effect_infos)
    if not isinstance(response, Mapping) or not isinstance(response.get("draft_url"), str) or not response["draft_url"]:
        raise ValueError("add_effects执行器必须返回有效draft_url")
    return {
        "draft_url": response["draft_url"],
        "effect_ids": list(response.get("effect_ids", [])),
        "segment_ids": list(response.get("segment_ids", [])),
        "track_id": response.get("track_id", ""),
    }


__all__ = ["AddEffectsTransportRequired", "AddEffectsValidationError", "parse_effects_data", "run_add_effects"]
