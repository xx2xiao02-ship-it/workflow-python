"""执行层适配器：把 AssetManifest 转成无副作用的剪映 DraftPlan。"""

from __future__ import annotations

from collections import OrderedDict

from .contracts import AssetManifest, DraftPlan, LayerContractError, TrackPlan


TRACK_ORDER = ("video", "audio", "caption")


def build_draft_plan(
    manifest: AssetManifest,
    *,
    width: int = 1080,
    height: int = 1920,
    draft_url: str = "",
    effects_enabled: bool = False,
) -> DraftPlan:
    """只生成执行计划，不写入剪映，也不调用特效插件。"""

    if not isinstance(manifest, AssetManifest):
        raise LayerContractError("build_draft_plan 需要 AssetManifest")
    grouped: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
    for asset in manifest.assets:
        key = (asset.kind, asset.source_node)
        grouped.setdefault(key, []).append(asset.asset_id)

    tracks: list[TrackPlan] = []
    for kind in TRACK_ORDER:
        for (group_kind, source_node), asset_ids in grouped.items():
            if group_kind == kind:
                safe_name = source_node.replace("*", "")
                tracks.append(
                    TrackPlan(
                        track_id=f"{kind}:{safe_name}",
                        kind=kind,
                        source_node=source_node,
                        items=asset_ids,
                    )
                )
    return DraftPlan(
        width=width,
        height=height,
        duration_us=manifest.duration_us,
        tracks=tracks,
        assets=list(manifest.assets),
        effects_enabled=effects_enabled,
        draft_url=draft_url,
    )


__all__ = ["TRACK_ORDER", "build_draft_plan"]
