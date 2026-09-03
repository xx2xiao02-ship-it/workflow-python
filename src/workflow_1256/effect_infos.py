"""1256 节点 197721「特效信息」的 Python 实现。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def effect_infos(effects: list[str], timelines: list[Mapping[str, Any]]) -> str:
    if len(effects) != len(timelines):
        min_len = min(len(effects), len(timelines))
        effects = effects[:min_len]
        timelines = timelines[:min_len]
    infos = []
    for effect, timeline in zip(effects, timelines):
        infos.append({"effect_title": effect, "start": timeline["start"], "end": timeline["end"]})
    return json.dumps(infos, ensure_ascii=False)


def _resolve(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, Mapping) and isinstance(value.get("input"), Mapping):
        value = value["input"]
    if not isinstance(value, Mapping):
        raise TypeError("特效信息输入必须是对象")
    return value


def run_effect_infos(params: Any) -> dict[str, str]:
    value = _resolve(getattr(params, "input", params))
    return {"infos": effect_infos(value["effects"], value["timelines"])}


__all__ = ["effect_infos", "run_effect_infos"]
