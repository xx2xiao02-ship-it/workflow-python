"""“具体画面导演”节点的 Python 契约适配层。

原始 Coze 导出包只保存了 ``directors_v2`` 插件的节点配置，没有随包导出
插件实现源码。该节点的真实导演逻辑因此不能在本地臆造。本模块只负责：

* 按原始配置接收 ``text``；
* 校验并保留 Coze 输出字段、对象字段和数组顺序；
* 通过显式注入的传输层转发请求。

未注入传输层时不会调用网络、模型或插件，也不会伪造导演结果。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class DirectorsV2ValidationError(ValueError):
    """输入或响应不符合 ``directors_v2`` 契约时抛出。"""


class DirectorsV2TransportRequired(RuntimeError):
    """未注入外部插件传输层时抛出。"""


@dataclass(frozen=True)
class DirectorsV2Request:
    """原始节点显式声明的请求。"""

    text: str


class DirectorsV2Transport(Protocol):
    def __call__(self, request: DirectorsV2Request) -> Mapping[str, Any]: ...


_MISSING = object()


def _parse_json_object(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DirectorsV2ValidationError("具体画面导演输入 JSON 字符串无效") from exc


def _resolve_params(params: Any) -> Mapping[str, Any]:
    value = _parse_json_object(params)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DirectorsV2ValidationError("具体画面导演输入必须是对象")

    if isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    return value


def build_request(params: Any = None) -> DirectorsV2Request:
    """把 Coze 节点输入转换为 ``directors_v2`` 请求。"""

    resolved = _resolve_params(params)
    text = resolved.get("text", _MISSING)
    if not isinstance(text, str):
        raise DirectorsV2ValidationError("具体画面导演的 text 必须是字符串")
    return DirectorsV2Request(text=text)


def _required_string(value: Mapping[str, Any], field: str, path: str) -> str:
    result = value.get(field, _MISSING)
    if not isinstance(result, str):
        raise DirectorsV2ValidationError(f"{path}.{field} 必须是字符串")
    return result


def _required_string_list(value: Mapping[str, Any], field: str, path: str) -> list[str]:
    result = value.get(field, _MISSING)
    if not isinstance(result, list) or not all(isinstance(item, str) for item in result):
        raise DirectorsV2ValidationError(f"{path}.{field} 必须是字符串数组")
    return list(result)


def _normalize_director_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DirectorsV2ValidationError("$.director_plan 必须是对象")

    # 顺序来自原始 YAML 的 node_outputs.properties，不按“合理逻辑”重排。
    return {
        "arc": _required_string_list(value, "arc", "$.director_plan"),
        "core": _required_string(value, "core", "$.director_plan"),
        "director_type": _required_string(value, "director_type", "$.director_plan"),
        "emo": _required_string(value, "emo", "$.director_plan"),
        "expression_domains": _required_string_list(
            value, "expression_domains", "$.director_plan"
        ),
        "goal": _required_string(value, "goal", "$.director_plan"),
        "open": _required_string(value, "open", "$.director_plan"),
        "rule": _required_string(value, "rule", "$.director_plan"),
        "spine": _required_string(value, "spine", "$.director_plan"),
        "tone": _required_string(value, "tone", "$.director_plan"),
        "variation_focus": _required_string_list(
            value, "variation_focus", "$.director_plan"
        ),
    }


def _normalize_segment_beats(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DirectorsV2ValidationError("$.segment_beats 必须是数组")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        path = f"$.segment_beats[{index}]"
        if not isinstance(item, Mapping):
            raise DirectorsV2ValidationError(f"{path} 必须是对象")

        beats = item.get("beats", _MISSING)
        if not isinstance(beats, list):
            raise DirectorsV2ValidationError(f"{path}.beats 必须是数组")

        normalized_beats: list[dict[str, Any]] = []
        for beat_index, beat in enumerate(beats):
            beat_path = f"{path}.beats[{beat_index}]"
            if not isinstance(beat, Mapping):
                raise DirectorsV2ValidationError(f"{beat_path} 必须是对象")
            normalized_beats.append({
                "expression_need": _required_string(beat, "expression_need", beat_path),
                "relation": _required_string(beat, "relation", beat_path),
                "route_candidates": _required_string_list(
                    beat, "route_candidates", beat_path
                ),
            })

        segment_index = item.get("segment_index", _MISSING)
        if isinstance(segment_index, bool) or not isinstance(segment_index, int):
            raise DirectorsV2ValidationError(f"{path}.segment_index 必须是整数")

        normalized.append({
            "beats": normalized_beats,
            "rhythm": _required_string(item, "rhythm", path),
            "segment_goal": _required_string(item, "segment_goal", path),
            "segment_index": segment_index,
            "segment_text": _required_string(item, "segment_text", path),
        })

    return normalized


def normalize_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """校验插件响应并按原始输出契约返回，不改变数组内容或顺序。"""

    if not isinstance(response, Mapping):
        raise DirectorsV2ValidationError("具体画面导演响应必须是对象")

    ok = response.get("ok", _MISSING)
    if not isinstance(ok, bool):
        raise DirectorsV2ValidationError("$.ok 必须是布尔值")

    segments = response.get("segments", _MISSING)
    if not isinstance(segments, list) or not all(isinstance(item, str) for item in segments):
        raise DirectorsV2ValidationError("$.segments 必须是字符串数组")

    # 顺序来自原始 YAML 的 node_outputs：director_plan、ok、segment_beats、segments。
    return {
        "director_plan": _normalize_director_plan(response.get("director_plan")),
        "ok": ok,
        "segment_beats": _normalize_segment_beats(response.get("segment_beats")),
        "segments": list(segments),
    }


def run_directors_v2(
    params: Any = None,
    *,
    transport: DirectorsV2Transport
    | Callable[[DirectorsV2Request], Mapping[str, Any]]
    | None = None,
) -> dict[str, Any]:
    """执行契约适配；未提供传输层时不产生外部副作用。"""

    request = build_request(params)
    if transport is None:
        raise DirectorsV2TransportRequired(
            "未配置 directors_v2 传输层；当前只能完成契约测试，不能生成真实导演结果"
        )
    return normalize_response(transport(request))


async def main(
    args: Any,
    *,
    transport: DirectorsV2Transport
    | Callable[[DirectorsV2Request], Mapping[str, Any]]
    | None = None,
) -> dict[str, Any]:
    if isinstance(args, Mapping):
        params = args.get("params", args)
    else:
        params = getattr(args, "params", None)
    return run_directors_v2(params, transport=transport)
