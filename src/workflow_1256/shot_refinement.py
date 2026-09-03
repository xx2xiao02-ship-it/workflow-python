"""“镜头精细化”批处理节点的无外部调用契约适配器。

原始节点 ID 为 103964，批体内部包含一个大模型节点和一个代码节点。
本模块只负责批次拆分、顺序保留和响应契约校验；两个执行器必须显式注入，
因此不会调用外部模型、插件或鉴权接口。
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class ShotRefinementValidationError(ValueError):
    """批处理输入或输出不符合原始契约。"""


class ShotRefinementTransportRequired(RuntimeError):
    """未注入批体执行器。"""


SHOT_REFINEMENT_PARALLEL_WORKERS = 4


@dataclass(frozen=True)
class ShotRefinementItem:
    duration: float
    item: Mapping[str, Any]
    segment: str
    timeline: Mapping[str, int]


class LLMTransport(Protocol):
    def __call__(self, item: ShotRefinementItem) -> Mapping[str, Any]: ...


class CodeTransport(Protocol):
    def __call__(self, item: ShotRefinementItem, llm_result: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _parse(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ShotRefinementValidationError("镜头精细化输入 JSON 字符串无效") from exc


def _resolve(params: Any) -> Mapping[str, Any]:
    value = _parse(params)
    if not isinstance(value, Mapping):
        raise ShotRefinementValidationError("镜头精细化输入必须是对象")
    if isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool):
        raise ShotRefinementValidationError(f"{path} 必须是数字")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ShotRefinementValidationError(f"{path} 必须是数字") from exc


def _number_preserve_json_type(value: Any, path: str) -> int | float:
    """保留 Coze JSON 数字的 int/float 类型；字符串数字只作兼容转换。"""

    if isinstance(value, bool):
        raise ShotRefinementValidationError(f"{path} 必须是数字")
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ShotRefinementValidationError(f"{path} 必须是数字") from exc


def _timeline(value: Any, path: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ShotRefinementValidationError(f"{path} 必须是对象")
    result: dict[str, int] = {}
    for field in ("start", "end"):
        field_value = value.get(field)
        if isinstance(field_value, bool) or not isinstance(field_value, int):
            raise ShotRefinementValidationError(f"{path}.{field} 必须是整数")
        result[field] = field_value
    return result


def build_items(params: Any = None) -> list[ShotRefinementItem]:
    value = _resolve(params)
    durations = value.get("duration")
    items = value.get("items")
    segments = value.get("segments")
    timelines = value.get("timelines")
    if not all(isinstance(part, list) for part in (durations, items, segments, timelines)):
        raise ShotRefinementValidationError("duration、items、segments、timelines 必须都是数组")
    counts = {len(durations), len(items), len(segments), len(timelines)}
    if len(counts) != 1:
        raise ShotRefinementValidationError("批处理四组输入数组数量不一致")

    result: list[ShotRefinementItem] = []
    for index in range(len(durations)):
        if not isinstance(items[index], Mapping):
            raise ShotRefinementValidationError(f"$.items[{index}] 必须是对象")
        if not isinstance(segments[index], str):
            raise ShotRefinementValidationError(f"$.segments[{index}] 必须是字符串")
        result.append(ShotRefinementItem(
            duration=_number(durations[index], f"$.duration[{index}]"),
            item=dict(items[index]),
            segment=segments[index],
            timeline=_timeline(timelines[index], f"$.timelines[{index}]"),
        ))
    return result


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ShotRefinementValidationError(f"{path} 必须是字符串")
    return value


def _llm_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ShotRefinementValidationError("镜头精细化导演响应必须是对象")
    shots = value.get("shots")
    if not isinstance(shots, list):
        raise ShotRefinementValidationError("LLM 输出 shots 必须是数组")
    normalized_shots: list[dict[str, str]] = []
    for i, raw_shot in enumerate(shots):
        if not isinstance(raw_shot, Mapping):
            raise ShotRefinementValidationError(f"$.shots[{i}] 必须是对象")
        shot = {
            "source_text": _string(raw_shot.get("source_text"), f"$.shots[{i}].source_text"),
            "clip_role": _string(raw_shot.get("clip_role"), f"$.shots[{i}].clip_role"),
            "story_beat": _string(raw_shot.get("story_beat"), f"$.shots[{i}].story_beat"),
        }
        # 新版逐镜编导使用 narration_text 锁定真实旁白切分；旧 Coze fixture
        # 没有该字段时保持原三字段契约，以便离线等价测试仍可通过。
        if "narration_text" in raw_shot:
            shot["narration_text"] = _string(raw_shot.get("narration_text"), f"$.shots[{i}].narration_text")
        normalized_shots.append(shot)
    return {
        "shots": normalized_shots,
        "reasoning_content": _string(value.get("reasoning_content", ""), "$.reasoning_content"),
    }


def _code_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ShotRefinementValidationError("时长计算时间线规划响应必须是对象")
    for field in ("clip_duration", "int_duration", "shots", "timelines"):
        if not isinstance(value.get(field), list):
            raise ShotRefinementValidationError(f"Code 输出 {field} 必须是数组")
    normalized_shots: list[dict[str, Any]] = []
    for i, shot in enumerate(value["shots"]):
        if not isinstance(shot, Mapping):
            raise ShotRefinementValidationError(f"$.shots[{i}] 必须是对象")
        normalized = {
            "source_text": _string(shot.get("source_text"), f"$.shots[{i}].source_text"),
            "clip_role": _string(shot.get("clip_role"), f"$.shots[{i}].clip_role"),
            "story_beat": _string(shot.get("story_beat"), f"$.shots[{i}].story_beat"),
            "clip_duration": _number_preserve_json_type(
                shot.get("clip_duration"), f"$.shots[{i}].clip_duration"
            ),
        }
        if "narration_text" in shot:
            normalized["narration_text"] = _string(shot.get("narration_text"), f"$.shots[{i}].narration_text")
        normalized_shots.append(normalized)
    return {
        "shots": normalized_shots,
        "clip_duration": [
            _number_preserve_json_type(v, "$.clip_duration[]")
            for v in value["clip_duration"]
        ],
        "int_duration": [
            int(v) if isinstance(v, int) and not isinstance(v, bool) else int(_number(v, "$.int_duration[]"))
            for v in value["int_duration"]
        ],
        "timelines": [_timeline(v, f"$.timelines[{i}]") for i, v in enumerate(value["timelines"])],
    }


def _resolve_parallel_workers(value: int | None) -> int:
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ShotRefinementValidationError("并行 worker 数必须是正整数")
        return value
    raw = os.environ.get("DIRECTOR_SHOT_REFINEMENT_PARALLEL_WORKERS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = SHOT_REFINEMENT_PARALLEL_WORKERS
        if parsed > 0:
            return parsed
    return SHOT_REFINEMENT_PARALLEL_WORKERS


def run_shot_refinement(
    params: Any = None,
    *,
    llm_transport: LLMTransport | Callable[[ShotRefinementItem], Mapping[str, Any]] | None = None,
    code_transport: CodeTransport | Callable[[ShotRefinementItem, Mapping[str, Any]], Mapping[str, Any]] | None = None,
    parallel_workers: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    items = build_items(params)
    if llm_transport is None or code_transport is None:
        raise ShotRefinementTransportRequired(
            "未配置镜头精细化批体执行器；当前只能完成契约测试"
        )

    if not items:
        return {"LLM_list": [], "Code_list": []}

    worker_count = min(_resolve_parallel_workers(parallel_workers), len(items))
    indexed_results: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    errors: list[tuple[int, Exception]] = []

    def run_one(index: int, item: ShotRefinementItem) -> tuple[int, dict[str, Any], dict[str, Any]]:
        llm_result = _llm_result(llm_transport(item))
        code_result = _code_result(code_transport(item, llm_result))
        return index, llm_result, code_result

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="shot-refinement") as executor:
        futures = {
            executor.submit(run_one, index, item): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                result_index, llm_result, code_result = future.result()
                indexed_results[result_index] = (llm_result, code_result)
            except Exception as error:
                errors.append((index, error))
    if errors:
        _, error = min(errors, key=lambda item: item[0])
        raise error

    llm_list = [indexed_results[index][0] for index in range(len(items))]
    code_list = [indexed_results[index][1] for index in range(len(items))]
    return {"LLM_list": llm_list, "Code_list": code_list}
