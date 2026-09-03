"""BGM 生成批处理的严格编排适配。

对应原工作流的 ``141288 -> 186546``：保持 BGM 任务、生成结果和 URL
数组按输入顺序一一对应。该模块不负责 ``165818 merge_bgm_timeline``；融合器
仍是独立插件边界，未拿 CapCut 的 ``audio_timelines`` 冒充融合结果。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .bgm_task_assembly import run_bgm_task_assembly


class BgmGenerationTransportRequired(RuntimeError):
    """未注入 gen_bgm 真实 transport。"""


class BgmGenerationValidationError(ValueError):
    """gen_bgm 响应不符合原节点输出契约。"""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def extract_bgm_audio_url(response: Mapping[str, Any], index: int = 0) -> str:
    if not isinstance(response, Mapping):
        raise BgmGenerationValidationError(f"186546[{index}] 输出必须是对象")
    data = response.get("data")
    if not isinstance(data, Mapping):
        raise BgmGenerationValidationError(f"186546[{index}].data 必须是对象")
    song = data.get("SongDetail")
    if not isinstance(song, Mapping):
        raise BgmGenerationValidationError(
            f"186546[{index}].data.SongDetail 必须是对象"
        )
    url = _text(song.get("AudioUrl"))
    if not url.startswith(("http://", "https://")):
        raise BgmGenerationValidationError(
            f"186546[{index}].data.SongDetail.AudioUrl 必须是 http(s) URL"
        )
    return url


def run_bgm_generation(
    params: Any,
    *,
    transport: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    query_transport: Callable[[Mapping[str, Any], str], Mapping[str, Any]] | None = None,
    existing_task_ids: Sequence[str] | None = None,
    task_created_reporter: Callable[[Mapping[str, Any]], None] | None = None,
    task_query_reporter: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """组装并逐项执行 BGM 生成，保留原始批处理顺序。"""

    if transport is None:
        raise BgmGenerationTransportRequired(
            "未配置 186546 gen_bgm 真实 transport"
        )
    assembly = run_bgm_task_assembly(params)
    tasks = assembly["bgm_tasks"]
    outputs: list[dict[str, Any]] = []
    audio_urls: list[str] = []
    existing = list(existing_task_ids or [])
    for index, task in enumerate(tasks):
        existing_task_id = str(existing[index]).strip() if index < len(existing) else ""
        if existing_task_id:
            if query_transport is None:
                raise BgmGenerationValidationError(
                    f"186546[{index}] 已有 TaskID，但未提供续查 transport，拒绝重复创建"
                )
            raw = query_transport(task, existing_task_id)
        else:
            raw = transport(task)
        if not isinstance(raw, Mapping):
            raise BgmGenerationValidationError(
                f"186546[{index}] transport 输出必须是对象"
            )
        audio_urls.append(extract_bgm_audio_url(raw, index))
        outputs.append(dict(raw))
    return {
        "bgm_tasks": list(tasks),
        "bgm_timelines": list(assembly["bgm_timelines"]),
        "transition_schemes": list(assembly["transition_schemes"]),
        "AudioUrl_list": audio_urls,
        "gen_bgm_outputs": outputs,
    }


__all__ = [
    "BgmGenerationTransportRequired",
    "BgmGenerationValidationError",
    "extract_bgm_audio_url",
    "run_bgm_generation",
]
