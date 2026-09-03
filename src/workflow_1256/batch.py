"""1256 工作流“批处理”（110697）视频生成分支的契约实现。

Coze 外层节点本身没有可提取的 Python 源码。本模块只实现已经从 YAML
和真实执行页面确认的部分：四组输入按索引一一映射到
``video_generate``，再把每项的 ``task_id`` 映射到 ``video_query``，最后
按批处理项原顺序聚合 ``public_video_url``。

真实 Coze 对单项异常是否终止整个批次，尚无完整页面证据；因此异常会
带索引抛出，并不伪装成已经通过 Coze 等价验收。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, TypeAlias

from .canvas import resolve_canvas
from .material_models import normalize_frame_continuity, normalize_video_model


BATCH_LIMIT = 100
CONCURRENT_SIZE = 8

BatchRunner: TypeAlias = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class BatchValidationError(ValueError):
    """批处理输入或子节点输出不符合已登记契约。"""


class BatchTransportRequired(RuntimeError):
    """未注入子节点执行器时，阻止任何真实外部调用。"""


class BatchItemExecutionError(RuntimeError):
    """带批处理索引的子节点执行错误。"""

    def __init__(self, index: int, message: str) -> None:
        self.index = index
        super().__init__(f"批处理第 {index + 1} 项执行失败：{message}")


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BatchValidationError("批处理 JSON 字符串输入无效") from exc


def _resolve_params(params: Any) -> Mapping[str, Any]:
    value = getattr(params, "input", params)
    value = _parse_json(value)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BatchValidationError("批处理输入必须是对象")
    if isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    return value


def _required_list(value: Any, field: str) -> list[Any]:
    value = _parse_json(value)
    if not isinstance(value, list):
        raise BatchValidationError(f"{field} 必须是数组")
    return list(value)


def _validate_string_items(values: list[Any], field: str) -> None:
    if not all(isinstance(item, str) for item in values):
        raise BatchValidationError(f"{field} 必须是字符串数组")


def _validate_duration_items(values: list[Any]) -> None:
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in values):
        raise BatchValidationError("int_duration 必须是数字数组")


def build_batch_items(params: Any) -> list[dict[str, Any]]:
    """按 Coze 批处理输入顺序建立四字段镜头项。"""

    value = _resolve_params(params)
    fields = {
        name: _required_list(value.get(name), name)
        for name in ("video_prompt", "int_duration", "ref_image_f", "ref_image_e")
    }
    counts = {len(items) for items in fields.values()}
    if len(counts) != 1:
        raise BatchValidationError(
            "批处理四组输入数组数量不一致："
            + ", ".join(f"{name}={len(items)}" for name, items in fields.items())
        )
    count = len(fields["video_prompt"])
    if count > BATCH_LIMIT:
        raise BatchValidationError(f"批处理数量超过上限 {BATCH_LIMIT}：{count}")

    _validate_string_items(fields["video_prompt"], "video_prompt")
    _validate_duration_items(fields["int_duration"])
    _validate_string_items(fields["ref_image_f"], "ref_image_f")
    _validate_string_items(fields["ref_image_e"], "ref_image_e")
    canvas = resolve_canvas(value.get("canvas", value.get("ratio", None)))
    try:
        video_model = normalize_video_model(value.get("video_model", "auto"))
    except ValueError as exc:
        raise BatchValidationError(str(exc)) from exc
    try:
        frame_continuity_enabled = normalize_frame_continuity(
            value.get("use_frame_continuity")
            if "use_frame_continuity" in value
            else None,
            default=True,
        )
    except ValueError as exc:
        raise BatchValidationError(str(exc)) from exc

    return [
        {
            "video_prompt": fields["video_prompt"][index],
            "int_duration": fields["int_duration"][index],
            "ref_image_f": fields["ref_image_f"][index],
            "ref_image_e": (
                fields["ref_image_e"][index]
                if frame_continuity_enabled
                else ""
            ),
            "ratio": canvas.aspect_ratio,
            "video_model": video_model,
        }
        for index in range(count)
    ]


def _video_generate_input(item: Mapping[str, Any]) -> dict[str, Any]:
    """构造 139653 已登记的输入映射和固定参数。"""

    return {
        "prompt": item["video_prompt"],
        "duration": item["int_duration"],
        "first_frame_url": item["ref_image_f"],
        "last_frame_url": item["ref_image_e"],
        "generate_audio": False,
        "ratio": item["ratio"],
        "resolution": "480p",
        "watermark": False,
        "video_model": item["video_model"],
    }


def _public_video_url(query_result: Mapping[str, Any]) -> str:
    value = query_result.get("public_video_url", "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BatchValidationError("video_query.public_video_url 必须是字符串")
    return value


def run_video_generation_batch(
    params: Any,
    *,
    generate_runner: BatchRunner | None = None,
    query_runner: BatchRunner | None = None,
    concurrent_size: int = CONCURRENT_SIZE,
) -> dict[str, list[str]]:
    """执行 110697 的已确认成功路径，并保持结果数组顺序。

    ``generate_runner`` 和 ``query_runner`` 必须由调用方显式注入；默认不
    访问 Ark、TOS 或任何插件。子节点可并发运行，但结果使用原始索引
    回填，避免完成先后改变 ``public_video_url_list`` 顺序。
    """

    items = build_batch_items(params)
    if generate_runner is None or query_runner is None:
        raise BatchTransportRequired("未注入 video_generate/video_query 执行器")
    if isinstance(concurrent_size, bool) or not isinstance(concurrent_size, int) or concurrent_size <= 0:
        raise BatchValidationError("并发数量必须是正整数")
    if not items:
        return {"public_video_url_list": []}

    results: list[str | None] = [None] * len(items)

    def run_one(index: int, item: Mapping[str, Any]) -> tuple[int, str]:
        try:
            generated = generate_runner(_video_generate_input(item))
            if not isinstance(generated, Mapping):
                raise BatchValidationError("video_generate 输出必须是对象")
            task_id = generated.get("task_id", "")
            queried = query_runner({"task_id": task_id})
            if not isinstance(queried, Mapping):
                raise BatchValidationError("video_query 输出必须是对象")
            return index, _public_video_url(queried)
        except BatchItemExecutionError:
            raise
        except Exception as exc:
            raise BatchItemExecutionError(index, str(exc)) from exc

    worker_count = min(concurrent_size, len(items))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(run_one, index, item) for index, item in enumerate(items)]
        for future in as_completed(futures):
            index, public_url = future.result()
            results[index] = public_url

    if any(value is None for value in results):
        raise BatchValidationError("批处理存在未完成的输出项")
    return {"public_video_url_list": [value for value in results if value is not None]}


run_batch = run_video_generation_batch


__all__ = [
    "BATCH_LIMIT",
    "CONCURRENT_SIZE",
    "BatchItemExecutionError",
    "BatchRunner",
    "BatchTransportRequired",
    "BatchValidationError",
    "build_batch_items",
    "run_batch",
    "run_video_generation_batch",
]
