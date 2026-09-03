"""“图生视频提示词导演”LLM 节点及其批处理契约适配层。

该节点没有可执行插件源码，模型调用由 Coze 平台完成。本模块只校验原始
输入/输出 Schema，并通过显式注入的固定响应传输层做离线审计；默认不调用模型。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


USER_PROMPT_TEMPLATE = """当前镜头组上下文：
{{items}}

当前组动态种子数组：
{{motion_seed.motion_seed}}

当前组分镜头时长数组：
{{clip_duration.clip_duration}}

当前组连续参考图已按时间顺序作为多图输入传入。

请严格依据连续参考图、动态种子和时长数组，输出当前镜头组内全部分镜头的时序规划 JSON。"""


class VideoPromptDirectorValidationError(ValueError):
    """输入或固定模型响应不符合原始 LLM Schema。"""


class VideoPromptDirectorTransportRequired(RuntimeError):
    """未注入模型响应时阻止外部模型调用。"""


@dataclass(frozen=True)
class VideoPromptDirectorRequest:
    items: dict[str, Any]
    ref_image: list[str]
    motion_seed: dict[str, Any]
    clip_duration: dict[str, Any]


class VideoPromptDirectorTransport(Protocol):
    def __call__(self, request: VideoPromptDirectorRequest) -> Mapping[str, Any]: ...


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _resolve_params(params: Any) -> Mapping[str, Any]:
    value = _parse_json(params)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise VideoPromptDirectorValidationError("图生视频提示词导演输入必须是对象")
    if isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    return value


def _object(value: Any, field: str) -> dict[str, Any]:
    value = _parse_json(value)
    if not isinstance(value, Mapping):
        raise VideoPromptDirectorValidationError(f"$.{field} 必须是对象")
    return dict(value)


def _string_list(value: Any, field: str) -> list[str]:
    value = _parse_json(value)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise VideoPromptDirectorValidationError(f"$.{field} 必须是字符串数组")
    return list(value)


def _number_list(value: Any, field: str) -> list[int | float]:
    value = _parse_json(value)
    if not isinstance(value, list) or not all(
        isinstance(item, (int, float)) and not isinstance(item, bool)
        for item in value
    ):
        raise VideoPromptDirectorValidationError(f"$.{field} 必须是数字数组")
    return list(value)


def build_request(params: Any) -> VideoPromptDirectorRequest:
    value = _resolve_params(params)
    items = _object(value.get("items"), "items")
    ref_image = _string_list(value.get("ref_image"), "ref_image")
    motion_seed = _object(value.get("motion_seed"), "motion_seed")
    clip_duration = _object(value.get("clip_duration"), "clip_duration")

    _string_list(motion_seed.get("motion_seed"), "motion_seed.motion_seed")
    _number_list(clip_duration.get("clip_duration"), "clip_duration.clip_duration")
    return VideoPromptDirectorRequest(
        items=items,
        ref_image=ref_image,
        motion_seed=motion_seed,
        clip_duration=clip_duration,
    )


def render_user_prompt(request: VideoPromptDirectorRequest) -> str:
    """生成可审计的模板展开文本，不触发模型调用。"""

    return USER_PROMPT_TEMPLATE.replace(
        "{{items}}", json.dumps(request.items, ensure_ascii=False)
    ).replace(
        "{{motion_seed.motion_seed}}",
        json.dumps(request.motion_seed.get("motion_seed"), ensure_ascii=False),
    ).replace(
        "{{clip_duration.clip_duration}}",
        json.dumps(request.clip_duration.get("clip_duration"), ensure_ascii=False),
    )


def _required_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise VideoPromptDirectorValidationError(f"{path} 必须是字符串")
    return value


def _required_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VideoPromptDirectorValidationError(f"{path} 必须是整数")
    return value


def normalize_response(response: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise VideoPromptDirectorValidationError("图生视频提示词导演响应必须是对象")

    plans_value = response.get("plans")
    if not isinstance(plans_value, list):
        raise VideoPromptDirectorValidationError("$.plans 必须是数组")
    plans: list[dict[str, Any]] = []
    for index, raw_plan in enumerate(plans_value):
        path = f"$.plans[{index}]"
        if not isinstance(raw_plan, Mapping):
            raise VideoPromptDirectorValidationError(f"{path} 必须是对象")
        stages_value = raw_plan.get("stages")
        if not isinstance(stages_value, list):
            raise VideoPromptDirectorValidationError(f"{path}.stages 必须是数组")
        stages: list[dict[str, str]] = []
        for stage_index, raw_stage in enumerate(stages_value):
            stage_path = f"{path}.stages[{stage_index}]"
            if not isinstance(raw_stage, Mapping):
                raise VideoPromptDirectorValidationError(f"{stage_path} 必须是对象")
            stages.append({
                "action": _required_string(raw_stage.get("action"), f"{stage_path}.action"),
                "camera_motion": _required_string(
                    raw_stage.get("camera_motion"), f"{stage_path}.camera_motion"
                ),
                "time_range": _required_string(
                    raw_stage.get("time_range"), f"{stage_path}.time_range"
                ),
            })
        plans.append({
            "shot_index": _required_int(raw_plan.get("shot_index"), f"{path}.shot_index"),
            "duration": _required_int(raw_plan.get("duration"), f"{path}.duration"),
            "stages": stages,
        })

    # 顺序按 YAML node_outputs：plans、reasoning_content、visual_lock。
    return {
        "plans": plans,
        "reasoning_content": _required_string(
            response.get("reasoning_content"), "$.reasoning_content"
        ),
        "visual_lock": _required_string(response.get("visual_lock"), "$.visual_lock"),
    }


def run_video_prompt_director(
    params: Any,
    *,
    transport: VideoPromptDirectorTransport
    | Callable[[VideoPromptDirectorRequest], Mapping[str, Any]]
    | None = None,
) -> dict[str, Any]:
    request = build_request(params)
    if transport is None:
        raise VideoPromptDirectorTransportRequired(
            "未配置图生视频提示词导演模型响应；当前只允许离线契约审计"
        )
    normalized = normalize_response(transport(request))
    expected_plan_count = len(request.clip_duration["clip_duration"])
    if len(normalized["plans"]) != expected_plan_count:
        raise VideoPromptDirectorValidationError(
            "$.plans 数量必须与 clip_duration 数量一致："
            f"{len(normalized['plans'])} / {expected_plan_count}"
        )
    return normalized


def _batch_group(value: Any, index: int, field: str) -> Any:
    if not isinstance(value, list):
        raise VideoPromptDirectorValidationError(f"批处理输入 {field} 必须是数组")
    if index >= len(value):
        raise VideoPromptDirectorValidationError(f"批处理输入 {field} 缺少第 {index + 1} 组")
    return value[index]


def run_video_prompt_batch(
    params: Any,
    *,
    transport: VideoPromptDirectorTransport
    | Callable[[VideoPromptDirectorRequest], Mapping[str, Any]]
    | None = None,
) -> dict[str, Any]:
    """按批处理项顺序调用固定传输层，保持 195692 的输出数组顺序。"""

    value = _resolve_params(params)
    items = value.get("items")
    motion_seed = value.get("motion_seed")
    ref_image = value.get("ref_image")
    clip_duration = value.get("clip_duration")
    if not all(isinstance(item, list) for item in (items, motion_seed, ref_image, clip_duration)):
        raise VideoPromptDirectorValidationError("生成视频提示词撰写批处理输入必须全部是数组")
    counts = [len(items), len(motion_seed), len(ref_image), len(clip_duration)]
    if len(set(counts)) != 1:
        raise VideoPromptDirectorValidationError(f"批处理输入组数不一致：{counts}")

    if transport is None:
        raise VideoPromptDirectorTransportRequired(
            "未配置图生视频提示词导演模型响应；当前只允许离线契约审计"
        )

    outputs: list[dict[str, Any]] = []
    for index in range(len(items)):
        raw_ref_image = _batch_group(ref_image, index, "ref_image")
        if isinstance(raw_ref_image, Mapping) and "ref_image" in raw_ref_image:
            raw_ref_image = raw_ref_image["ref_image"]
        group = {
            "items": _batch_group(items, index, "items"),
            "motion_seed": _batch_group(motion_seed, index, "motion_seed"),
            "ref_image": raw_ref_image,
            "clip_duration": _batch_group(clip_duration, index, "clip_duration"),
        }
        outputs.append(run_video_prompt_director(group, transport=transport))
    return {"plan_out_list": outputs}
