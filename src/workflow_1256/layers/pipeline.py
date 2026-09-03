"""三层架构的离线编排入口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .assets import build_asset_manifest
from .director import build_director_plan
from .execution import build_draft_plan
from ..canvas import resolve_canvas


def run_three_layer_pipeline(params: Mapping[str, Any]) -> dict[str, Any]:
    """按编导层、素材层、执行层顺序运行一次无副作用样例管线。

    该入口不调用模型、素材服务或剪映服务；真实服务由上层节点 runner
    先得到响应，再把响应交给本入口进行契约化组装。
    """

    if not isinstance(params, Mapping):
        raise ValueError("三层管线输入必须是对象")
    director = build_director_plan(
        params.get("director_output"),
        text=params.get("text"),
    )
    manifest = build_asset_manifest(
        params.get("asset_inputs", {}),
        director_segments=director.segments,
    )
    canvas = resolve_canvas(params.get("canvas"))
    draft = build_draft_plan(
        manifest,
        width=canvas.width,
        height=canvas.height,
        draft_url=params.get("draft_url", ""),
        effects_enabled=params.get("effects_enabled", False),
    )
    return {
        "director_plan": director.to_dict(),
        "asset_manifest": manifest.to_dict(),
        "canvas": canvas.to_dict(),
        "draft_plan": draft.to_dict(),
    }


__all__ = ["run_three_layer_pipeline"]
