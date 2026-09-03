"""V3 说明镜头的确定性风格锁。

该模块只读取已经落盘的本地首帧，不调用外部模型，不改变编导字段。
首帧必须按 shot_id 绑定；无法证明绑定关系时直接阻断。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


class ExplanationStyleLockError(ValueError):
    """说明镜头风格锁无法建立。"""


STYLE_OVERRIDE_DEFAULTS: dict[str, Any] = {
    "background_color": "#F7F4EE",
    "accent_color": "#D97745",
    "accent_secondary": "#3B82A0",
    "text_color": "#172033",
    "surface_color": "#FFFFFF",
    "title_font_size": 58,
    "body_font_size": 34,
    "animation_pace": "steady",
    "pip_radius": 28,
    "pip_object_fit": "cover",
    "pip_top": "53%",
    "pip_right": "6%",
    "pip_width": "24%",
    "pip_height": "34%",
}


_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _bounded_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ExplanationStyleLockError(f"{label}必须是{minimum}到{maximum}之间的整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ExplanationStyleLockError(f"{label}必须是{minimum}到{maximum}之间的整数") from exc
    if number < minimum or number > maximum:
        raise ExplanationStyleLockError(f"{label}必须是{minimum}到{maximum}之间的整数")
    return number


def _percent(value: object, *, label: str, minimum: int, maximum: int) -> str:
    raw = str(value or "").strip().replace("%", "")
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ExplanationStyleLockError(f"{label}必须是{minimum}%到{maximum}%") from exc
    if number < minimum or number > maximum:
        raise ExplanationStyleLockError(f"{label}必须是{minimum}%到{maximum}%")
    return f"{number:g}%"


def normalize_style_override(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """校验素材页可编辑的 Remotion 样式参数，拒绝未知参数和越界值。"""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ExplanationStyleLockError("Remotion 微调参数必须是对象")
    unknown = sorted(str(key) for key in value if str(key) not in STYLE_OVERRIDE_DEFAULTS)
    if unknown:
        raise ExplanationStyleLockError(f"Remotion 微调参数不支持：{'、'.join(unknown)}")
    result: dict[str, Any] = {}
    for key in ("background_color", "accent_color", "accent_secondary", "text_color", "surface_color"):
        if key in value and value[key] is not None:
            color = str(value[key]).strip()
            if not _HEX_COLOR.fullmatch(color):
                raise ExplanationStyleLockError(f"{key}必须是六位十六进制颜色，例如 #D97745")
            result[key] = color.upper()
    if "title_font_size" in value:
        result["title_font_size"] = _bounded_int(value["title_font_size"], label="标题字号", minimum=36, maximum=96)
    if "body_font_size" in value:
        result["body_font_size"] = _bounded_int(value["body_font_size"], label="正文字号", minimum=20, maximum=64)
    if "animation_pace" in value:
        pace = str(value["animation_pace"] or "").strip().lower()
        if pace not in {"slow", "steady", "fast"}:
            raise ExplanationStyleLockError("动画节奏只支持慢、标准、快")
        result["animation_pace"] = pace
    if "pip_radius" in value:
        result["pip_radius"] = _bounded_int(value["pip_radius"], label="数字人圆角", minimum=0, maximum=80)
    if "pip_object_fit" in value:
        fit = str(value["pip_object_fit"] or "").strip().lower()
        if fit not in {"cover", "contain"}:
            raise ExplanationStyleLockError("数字人裁切只支持铺满裁切或完整显示")
        result["pip_object_fit"] = fit
    if "pip_top" in value:
        result["pip_top"] = _percent(value["pip_top"], label="数字人顶部位置", minimum=35, maximum=75)
    if "pip_right" in value:
        result["pip_right"] = _percent(value["pip_right"], label="数字人右边距", minimum=0, maximum=20)
    if "pip_width" in value:
        result["pip_width"] = _percent(value["pip_width"], label="数字人宽度", minimum=10, maximum=45)
    if "pip_height" in value:
        result["pip_height"] = _percent(value["pip_height"], label="数字人高度", minimum=20, maximum=55)
    return result


def duration_to_frames(duration_us: int, fps: int) -> int:
    """把微秒时长确定性转换为帧数，采用四舍五入且最少一帧。"""

    duration_us = int(duration_us)
    fps = int(fps)
    if duration_us <= 0 or fps <= 0:
        raise ExplanationStyleLockError("时长和帧率必须为正数")
    return max(1, (duration_us * fps + 500_000) // 1_000_000)


def _local_path(value: object) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path.resolve() if path.is_file() else None


def _aigc_shot_ids(shots: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(shot.get("shot_id") or "")
        for shot in shots
        if str(shot.get("shot_id") or "")
        and str(shot.get("media_type") or "") == "aigc_video"
    ]


def resolve_first_aigc_frame(
    shots: Sequence[Mapping[str, Any]],
    first_frame_assets: Mapping[str, Any],
) -> tuple[str, Path]:
    """按冻结镜头顺序把首个 AIGC 镜头绑定到本地首帧。"""

    raw_image_paths = first_frame_assets.get("image_paths") or []
    if not isinstance(raw_image_paths, list) or not raw_image_paths:
        raise ExplanationStyleLockError("首帧结果没有本地图片路径，无法建立风格锁")
    image_paths = [_local_path(value) for value in raw_image_paths]
    if any(path is None for path in image_paths):
        raise ExplanationStyleLockError("首帧结果包含不存在或非本地文件，无法建立风格锁")
    image_paths = [path for path in image_paths if path is not None]
    candidate_ids = _aigc_shot_ids(shots)
    if not candidate_ids:
        raise ExplanationStyleLockError("当前 V3 任务没有可用的 AIGC 镜头，无法建立首帧风格锁")

    # 新结果应在 grid cell 中保留 shot_id；旧结果没有该字段时，只允许
    # 按冻结 AIGC 镜头顺序一一对应，不允许使用未核对的 image_paths[0]。
    all_cell_ids: list[str] = []
    for batch in first_frame_assets.get("grid_batches") or []:
        if not isinstance(batch, Mapping):
            continue
        cells = batch.get("cells") if isinstance(batch.get("cells"), list) else []
        for cell in sorted(
            (item for item in cells if isinstance(item, Mapping)),
            key=lambda item: int(item.get("cell_index") or 0),
        ):
            shot_id = str(cell.get("shot_id") or "")
            if shot_id:
                all_cell_ids.append(shot_id)
    if len(all_cell_ids) == len(image_paths) and len(set(all_cell_ids)) == len(all_cell_ids):
        ordered_ids = all_cell_ids
    elif len(candidate_ids) == len(image_paths):
        # 兼容没有 grid cell shot_id 的旧结果；此时只能接受“全部首帧
        # 都属于 AIGC 镜头”的严格一一对应，不能把图片镜头错配给 AIGC。
        ordered_ids = candidate_ids
    else:
        ordered_ids = []
    if not image_paths or len(ordered_ids) != len(image_paths):
        raise ExplanationStyleLockError(
            f"首帧本地文件与 AIGC 镜头无法一一对应：镜头 {len(candidate_ids)}，首帧 {len(image_paths)}"
        )
    by_id = dict(zip(ordered_ids, image_paths, strict=True))
    first_id = candidate_ids[0]
    first_path = by_id.get(first_id)
    if first_path is None:
        raise ExplanationStyleLockError(f"找不到首个 AIGC 镜头 {first_id} 的本地首帧")
    return first_id, first_path


def _derive_palette(image_path: Path) -> dict[str, str]:
    try:
        from PIL import Image, ImageStat

        with Image.open(image_path) as image:
            stat = ImageStat.Stat(image.convert("RGB").resize((1, 1)))
            r, g, b = (int(max(0, min(255, value))) for value in stat.mean[:3])
    except Exception as exc:  # pragma: no cover - only malformed image is exceptional
        raise ExplanationStyleLockError(f"无法读取首帧图片：{image_path}：{exc}") from exc
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    background = "#F7F4EE" if brightness >= 150 else "#172033"
    text = "#172033" if brightness >= 150 else "#F8FAFC"
    accent = "#{:02X}{:02X}{:02X}".format(r, g, b)
    # 防止近灰色首帧产生不可读的强调色。
    if max(r, g, b) - min(r, g, b) < 18:
        accent = "#D97745"
    return {
        "background_color": background,
        "surface_color": "#FFFFFF" if brightness >= 150 else "#25324A",
        "text_color": text,
        "muted_color": "#667085" if brightness >= 150 else "#B7C2D4",
        "accent_color": accent,
        "accent_secondary": "#3B82A0",
    }


def build_style_lock(
    shots: Sequence[Mapping[str, Any]],
    first_frame_assets: Mapping[str, Any],
    output_path: str | Path,
    *,
    packaging_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    first_shot_id, first_path = resolve_first_aigc_frame(shots, first_frame_assets)
    style = {
        **_derive_palette(first_path),
        "font_family": "Microsoft YaHei, Arial, sans-serif",
        "font_source": "系统中文字体；生产机必须存在 Microsoft YaHei",
        "title_font_size": 58,
        "body_font_size": 34,
        "animation_pace": "steady",
        "pip_radius": 28,
        "pip_object_fit": "cover",
        "pip_top": "53%",
        "pip_right": "6%",
        "pip_width": "24%",
        "pip_height": "34%",
    }
    style_source = "first_aigc_frame_local_analysis+default"
    if isinstance(packaging_override, Mapping) and packaging_override:
        style.update({str(key): value for key, value in packaging_override.items() if value is not None})
        style_source += "+packaging_override"
    payload: dict[str, Any] = {
        "schema_version": "explanation-style-lock-v1",
        "source_shot_id": first_shot_id,
        "source_image_path": str(first_path),
        "style_source": style_source,
        "style": style,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    payload["style_lock_id"] = f"style-{digest}"
    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


__all__ = [
    "ExplanationStyleLockError",
    "STYLE_OVERRIDE_DEFAULTS",
    "build_style_lock",
    "duration_to_frames",
    "normalize_style_override",
    "resolve_first_aigc_frame",
]
