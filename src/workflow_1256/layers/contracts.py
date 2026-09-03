"""1256 三层架构的稳定数据契约。

三层之间只通过这里定义的 JSON 形状交接，不把模型响应、素材插件响应和
剪映写入响应混在一起。时间统一使用整数微秒；所有列表都保留输入顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class LayerContractError(ValueError):
    """三层架构的输入或输出不符合契约。"""


TIMELINE_UNIT = "microseconds"
MEDIA_KINDS = {"video", "audio", "caption"}


def _int_us(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LayerContractError(f"{field_name} 必须是整数微秒")
    if value < 0:
        raise LayerContractError(f"{field_name} 不能为负数")
    return value


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LayerContractError(f"{field_name} 必须是非空字符串")
    return value


@dataclass(frozen=True)
class DirectorPlan:
    """编导层输出：只描述内容和镜头意图，不包含素材 URL 或草稿 ID。"""

    text: str
    director_plan: dict[str, Any]
    segment_beats: list[dict[str, Any]]
    segments: list[str]
    source_node: str = "具体画面导演"

    def __post_init__(self) -> None:
        _non_empty_string(self.text, "DirectorPlan.text")
        if not isinstance(self.director_plan, dict):
            raise LayerContractError("DirectorPlan.director_plan 必须是对象")
        if not isinstance(self.segments, list) or not all(
            isinstance(item, str) and item.strip() for item in self.segments
        ):
            raise LayerContractError("DirectorPlan.segments 必须是非空字符串数组")
        if not isinstance(self.segment_beats, list):
            raise LayerContractError("DirectorPlan.segment_beats 必须是数组")
        if len(self.segments) != len(self.segment_beats):
            raise LayerContractError("segments 与 segment_beats 数量必须一致")
        if not all(isinstance(item, dict) for item in self.segment_beats):
            raise LayerContractError("DirectorPlan.segment_beats 每项必须是对象")

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "director_plan": dict(self.director_plan),
            "segment_beats": [dict(item) for item in self.segment_beats],
            "segments": list(self.segments),
            "source_node": self.source_node,
        }


@dataclass(frozen=True)
class AssetRecord:
    """素材层的一项可执行素材或字幕片段。"""

    asset_id: str
    kind: str
    uri: str
    start_us: int
    end_us: int
    duration_us: int
    source_node: str
    index: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _non_empty_string(self.asset_id, "AssetRecord.asset_id")
        if self.kind not in MEDIA_KINDS:
            raise LayerContractError(f"AssetRecord.kind 不支持：{self.kind}")
        if self.kind != "caption":
            _non_empty_string(self.uri, f"{self.asset_id}.uri")
        start = _int_us(self.start_us, f"{self.asset_id}.start_us")
        end = _int_us(self.end_us, f"{self.asset_id}.end_us")
        duration = _int_us(self.duration_us, f"{self.asset_id}.duration_us")
        if end <= start:
            raise LayerContractError(f"{self.asset_id}.end_us 必须大于 start_us")
        if duration != end - start:
            raise LayerContractError(f"{self.asset_id}.duration_us 必须等于 end_us-start_us")
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise LayerContractError(f"{self.asset_id}.index 必须是非负整数")
        if not isinstance(self.metadata, dict):
            raise LayerContractError(f"{self.asset_id}.metadata 必须是对象")

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "uri": self.uri,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "duration_us": self.duration_us,
            "source_node": self.source_node,
            "index": self.index,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AssetManifest:
    """素材层输出：素材、字幕和统一总时长。"""

    assets: list[AssetRecord]
    duration_us: int
    timeline_unit: str = TIMELINE_UNIT
    director_segments: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.timeline_unit != TIMELINE_UNIT:
            raise LayerContractError("AssetManifest.timeline_unit 必须是 microseconds")
        duration = _int_us(self.duration_us, "AssetManifest.duration_us")
        if not isinstance(self.assets, list):
            raise LayerContractError("AssetManifest.assets 必须是数组")
        for index, asset in enumerate(self.assets):
            if not isinstance(asset, AssetRecord):
                raise LayerContractError(f"AssetManifest.assets[{index}] 类型错误")
            if asset.end_us > duration:
                raise LayerContractError(f"{asset.asset_id}.end_us 超出总时长")
        if not isinstance(self.director_segments, list) or not all(
            isinstance(item, str) for item in self.director_segments
        ):
            raise LayerContractError("AssetManifest.director_segments 必须是字符串数组")
        if not isinstance(self.warnings, list) or not all(
            isinstance(item, str) for item in self.warnings
        ):
            raise LayerContractError("AssetManifest.warnings 必须是字符串数组")

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeline_unit": self.timeline_unit,
            "duration_us": self.duration_us,
            "assets": [asset.to_dict() for asset in self.assets],
            "director_segments": list(self.director_segments),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class TrackPlan:
    """执行层的一条剪映轨道计划。"""

    track_id: str
    kind: str
    source_node: str
    items: list[str]

    def __post_init__(self) -> None:
        _non_empty_string(self.track_id, "TrackPlan.track_id")
        if self.kind not in MEDIA_KINDS:
            raise LayerContractError(f"TrackPlan.kind 不支持：{self.kind}")
        if not isinstance(self.items, list) or not all(isinstance(item, str) for item in self.items):
            raise LayerContractError("TrackPlan.items 必须是字符串数组")

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "kind": self.kind,
            "source_node": self.source_node,
            "items": list(self.items),
        }


@dataclass(frozen=True)
class DraftPlan:
    """执行层输出：不写入剪映，只描述应如何创建剪映草稿。"""

    width: int
    height: int
    duration_us: int
    tracks: list[TrackPlan]
    assets: list[AssetRecord]
    effects_enabled: bool = False
    draft_url: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.width, bool) or not isinstance(self.width, int) or self.width <= 0:
            raise LayerContractError("DraftPlan.width 必须是正整数")
        if isinstance(self.height, bool) or not isinstance(self.height, int) or self.height <= 0:
            raise LayerContractError("DraftPlan.height 必须是正整数")
        _int_us(self.duration_us, "DraftPlan.duration_us")
        if not isinstance(self.tracks, list) or not all(
            isinstance(item, TrackPlan) for item in self.tracks
        ):
            raise LayerContractError("DraftPlan.tracks 类型错误")
        if not isinstance(self.assets, list) or not all(
            isinstance(item, AssetRecord) for item in self.assets
        ):
            raise LayerContractError("DraftPlan.assets 类型错误")
        if not isinstance(self.effects_enabled, bool):
            raise LayerContractError("DraftPlan.effects_enabled 必须是布尔值")

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "timeline_unit": TIMELINE_UNIT,
            "duration_us": self.duration_us,
            "effects_enabled": self.effects_enabled,
            "draft_url": self.draft_url,
            "tracks": [track.to_dict() for track in self.tracks],
            "assets": [asset.to_dict() for asset in self.assets],
        }


__all__ = [
    "AssetManifest",
    "AssetRecord",
    "DirectorPlan",
    "DraftPlan",
    "LayerContractError",
    "TIMELINE_UNIT",
    "TrackPlan",
]
