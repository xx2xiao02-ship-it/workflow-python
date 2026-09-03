"""1256 工作流“添加关键帧”（115137）的离线契约实现。

该节点会写入剪映草稿，因此默认只做输入解析和校验；真实写入必须显式注入
CapCut Mate executor。这样不会伪造 draft_url，也不会在离线测试中修改草稿。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse


SUPPORTED_PROPERTIES = {
    "KFTypePositionX", "KFTypePositionY", "KFTypeScaleX", "KFTypeScaleY",
    "KFTypeRotation", "KFTypeAlpha", "UNIFORM_SCALE", "KFTypeSaturation",
    "KFTypeContrast", "KFTypeBrightness", "KFTypeVolume",
}


class AddKeyframesValidationError(ValueError):
    """对齐原始 CustomError.INVALID_KEYFRAME_INFO 的输入错误。"""

    code = 2013
    base_message = "无效的关键帧信息，请检查keyframes字段值是否正确"

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(self.base_message)


class AddKeyframesTransportRequired(RuntimeError):
    """未注入草稿写入 executor 时的明确阻断。"""


def _invalid(detail: str) -> AddKeyframesValidationError:
    return AddKeyframesValidationError(detail)


def parse_keyframes_data(json_str: str) -> list[dict[str, Any]]:
    """复现 CapCut Mate 原始 parse_keyframes_data 的校验和字段顺序。"""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise _invalid(f"JSON parse error: {exc.msg}") from exc

    if not isinstance(data, list):
        raise _invalid("keyframes should be a list")

    result: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise _invalid(f"the {index}th item should be a dict")

        required_fields = ["segment_id", "property", "offset", "value"]
        missing_fields = [field for field in required_fields if field not in item]
        if missing_fields:
            raise _invalid(
                f"the {index}th item is missing required fields: {', '.join(missing_fields)}"
            )

        if item["property"] not in SUPPORTED_PROPERTIES:
            raise _invalid(
                f"the {index}th item has unsupported property type: {item['property']}"
            )

        if not isinstance(item["offset"], (int, float)) or item["offset"] < 0:
            raise _invalid(
                f"the {index}th item has invalid offset type or value: {item['offset']}"
            )

        if not isinstance(item["value"], (int, float)):
            raise _invalid(
                f"the {index}th item has invalid value type: {type(item['value'])}"
            )

        result.append({
            "segment_id": str(item["segment_id"]),
            "property": item["property"],
            "offset": float(item["offset"]),
            "value": float(item["value"]),
        })
    return result


def _parse_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return json.loads(value) if value.strip() else value


def _resolve_params(params: Any) -> Mapping[str, Any]:
    value = getattr(params, "input", params)
    value = _parse_json_string(value)
    if isinstance(value, Mapping) and isinstance(value.get("input"), Mapping):
        value = value["input"]
    if isinstance(value, Mapping) and isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value, Mapping) and isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    if not isinstance(value, Mapping):
        raise TypeError("添加关键帧输入必须是对象")
    return value


def _draft_id(draft_url: str) -> str:
    parsed = urlparse(draft_url)
    if parsed.query:
        return parse_qs(parsed.query).get("draft_id", [""])[0]
    return ""


def run_add_keyframes(
    params: Any,
    *,
    executor: Callable[[str, str], Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    """运行节点契约；真实草稿写入必须显式提供 executor。"""
    value = _resolve_params(params)
    draft_url = value["draft_url"]
    keyframes = value["keyframes"]
    if not isinstance(draft_url, str) or not draft_url:
        raise ValueError("无效的草稿URL")
    if not _draft_id(draft_url):
        raise ValueError("无效的草稿URL")
    if not isinstance(keyframes, str):
        keyframes = json.dumps(keyframes, ensure_ascii=False)

    parsed = parse_keyframes_data(keyframes)
    if not parsed:
        raise AddKeyframesValidationError("无效的关键帧信息，请检查keyframes字段值是否正确")
    if executor is None:
        raise AddKeyframesTransportRequired(
            "未注入 add_keyframes 草稿写入 executor；不会伪造 draft_url"
        )

    response = executor(draft_url, keyframes)
    if not isinstance(response, Mapping):
        raise ValueError("add_keyframes 执行器输出必须是对象")
    output_draft_url = response.get("draft_url")
    if not isinstance(output_draft_url, str) or not output_draft_url:
        raise ValueError("add_keyframes 执行器必须返回有效 draft_url")
    return {"draft_url": output_draft_url}


__all__ = [
    "AddKeyframesTransportRequired",
    "AddKeyframesValidationError",
    "parse_keyframes_data",
    "run_add_keyframes",
]
