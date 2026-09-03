"""项目级画幅契约，禁止由各节点自行猜测横竖版。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


class CanvasValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CanvasSpec:
    aspect_ratio: str
    width: int
    height: int
    orientation: str
    label: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PRESETS = {
    "9:16": CanvasSpec("9:16", 1080, 1920, "portrait", "竖版 9:16（1080×1920）"),
    "16:9": CanvasSpec("16:9", 1920, 1080, "landscape", "横版 16:9（1920×1080）"),
    "1:1": CanvasSpec("1:1", 1080, 1080, "square", "方形 1:1（1080×1080）"),
    "3:2": CanvasSpec("3:2", 1620, 1080, "landscape", "横版 3:2（1620×1080）"),
    "2:3": CanvasSpec("2:3", 1080, 1620, "portrait", "竖版 2:3（1080×1620）"),
}


def canvas_presets() -> tuple[CanvasSpec, ...]:
    return tuple(_PRESETS.values())


def resolve_canvas(value: Any = None) -> CanvasSpec:
    """不传时兼容旧调用的竖版 9:16。"""
    if isinstance(value, CanvasSpec):
        return value
    if value is None or value == "":
        return _PRESETS["9:16"]
    if isinstance(value, str):
        ratio = value.strip()
    elif isinstance(value, Mapping):
        raw_ratio = value.get("aspect_ratio", value.get("ratio", ""))
        ratio = raw_ratio.strip() if isinstance(raw_ratio, str) else ""
    else:
        raise CanvasValidationError("canvas 必须是画幅字符串或对象")
    if ratio not in _PRESETS:
        raise CanvasValidationError("仅支持画幅：9:16、16:9、1:1、3:2、2:3")
    preset = _PRESETS[ratio]
    if isinstance(value, Mapping):
        for name, expected in (("width", preset.width), ("height", preset.height)):
            if value.get(name) is not None and value[name] != expected:
                raise CanvasValidationError(f"canvas.{name} 与 {ratio} 预设不一致，应为 {expected}")
    return preset


def canvas_prompt_label(value: Any = None) -> str:
    # 保持旧 9:16 首帧提示词逐字兼容；新画幅仍包含方向和尺寸，避免模型误解。
    canvas = resolve_canvas(value)
    if canvas.aspect_ratio == "9:16":
        return "9:16竖版"
    return canvas.label


__all__ = ["CanvasSpec", "CanvasValidationError", "canvas_presets", "canvas_prompt_label", "resolve_canvas"]
