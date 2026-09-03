"""8364 节点 139488“host 镜头识别”的契约适配器。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class HostShotRecognitionValidationError(ValueError):
    """输入或模型响应不符合 8364 YAML 契约。"""


class HostShotRecognitionTransportRequired(RuntimeError):
    """未注入 Host LLM transport。"""


@dataclass(frozen=True)
class HostShotRecognitionRequest:
    host_llm_input: Mapping[str, Any]


class HostShotRecognitionTransport(Protocol):
    def __call__(self, request: HostShotRecognitionRequest) -> Mapping[str, Any]: ...


def _parse(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise HostShotRecognitionValidationError("host 镜头识别输入 JSON 无效") from exc


def build_request(params: Any) -> HostShotRecognitionRequest:
    value = _parse(params)
    if not isinstance(value, Mapping):
        raise HostShotRecognitionValidationError("host 镜头识别输入必须是对象")
    for key in ("params", "_input"):
        if isinstance(value.get(key), Mapping):
            value = value[key]
    host_llm_input = _parse(value.get("host_llm_input"))
    if not isinstance(host_llm_input, Mapping):
        raise HostShotRecognitionValidationError("host_llm_input 必须是对象")
    return HostShotRecognitionRequest(dict(host_llm_input))


def normalize_response(response: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise HostShotRecognitionValidationError("host 镜头识别响应必须是对象")
    host_idxs = response.get("host_idxs")
    if not isinstance(host_idxs, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in host_idxs
    ):
        raise HostShotRecognitionValidationError("host_idxs 必须是整数数组")
    if len(host_idxs) != len(set(host_idxs)):
        raise HostShotRecognitionValidationError("host_idxs 必须去重")
    if host_idxs != sorted(host_idxs):
        raise HostShotRecognitionValidationError("host_idxs 必须按从小到大排序")
    reasoning_content = response.get("reasoning_content")
    if not isinstance(reasoning_content, str):
        raise HostShotRecognitionValidationError("reasoning_content 必须是字符串")
    return {"host_idxs": list(host_idxs), "reasoning_content": reasoning_content}


def run_host_shot_recognition(
    params: Any,
    *,
    transport: HostShotRecognitionTransport
    | Callable[[HostShotRecognitionRequest], Mapping[str, Any]]
    | None = None,
) -> dict[str, Any]:
    request = build_request(params)
    if transport is None:
        raise HostShotRecognitionTransportRequired(
            "未配置 139488 host 镜头识别 LLM transport"
        )
    result = normalize_response(transport(request))
    candidates = request.host_llm_input.get("candidates")
    if not isinstance(candidates, list):
        raise HostShotRecognitionValidationError("host_llm_input.candidates 必须是数组")
    candidate_idxs: set[int] = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            raise HostShotRecognitionValidationError(
                f"host_llm_input.candidates[{index}] 必须是对象"
            )
        candidate_idx = candidate.get("idx")
        if not isinstance(candidate_idx, int) or isinstance(candidate_idx, bool):
            raise HostShotRecognitionValidationError(
                f"host_llm_input.candidates[{index}].idx 必须是整数"
            )
        if candidate_idx in candidate_idxs:
            raise HostShotRecognitionValidationError(
                "host_llm_input.candidates 的 idx 必须唯一"
            )
        candidate_idxs.add(candidate_idx)
    unknown = [item for item in result["host_idxs"] if item not in candidate_idxs]
    if unknown:
        raise HostShotRecognitionValidationError(
            f"host_idxs 包含 candidates 中不存在的 idx: {unknown}"
        )
    return result


__all__ = [
    "HostShotRecognitionRequest",
    "HostShotRecognitionTransportRequired",
    "HostShotRecognitionValidationError",
    "build_request",
    "normalize_response",
    "run_host_shot_recognition",
]
