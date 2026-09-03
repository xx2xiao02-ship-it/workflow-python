"""素材生成层的模型选择契约。

模型选择是素材层可选配置，不进入编导层锁定数据，也不携带鉴权信息。
"""

from __future__ import annotations

from typing import Any


class MaterialModelSelectionError(ValueError):
    """素材模型选择不在当前支持清单内。"""


IMAGE_MODEL_CHOICES = ("auto", "image2")
VIDEO_MODEL_CHOICES = ("auto", "seedance_1_5_pro", "seedance_1_0_pro")
FRAME_CONTINUITY_CHOICES = ("disabled", "enabled")

# 对外使用稳定别名；实际图像 API 仍使用当前已验证的模型 ID。
IMAGE_MODEL_API_IDS = {"image2": "gpt-image-2"}
VIDEO_MODEL_API_IDS = {
    "seedance_1_5_pro": "doubao-seedance-1-5-pro-251215",
    "seedance_1_0_pro": "doubao-seedance-1-0-pro-250528",
}


def normalize_image_model(value: Any) -> str:
    selected = str(value or "auto").strip().lower()
    aliases = {"gpt-image-2": "image2", "image_2": "image2", "image2": "image2"}
    selected = aliases.get(selected, selected)
    if selected == "auto":
        return "image2"
    if selected not in IMAGE_MODEL_API_IDS:
        raise MaterialModelSelectionError(
            f"image_model 不受支持：{value!r}；当前可选 Image2 或 auto"
        )
    return selected


def normalize_video_model(value: Any) -> str:
    selected = str(value or "auto").strip().lower().replace(" ", "_")
    aliases = {
        "seedance1.5pro": "seedance_1_5_pro",
        "seedance_1_5_pro": "seedance_1_5_pro",
        "seedance_1_5pro": "seedance_1_5_pro",
        "doubao-seedance-1-5-pro-251215": "seedance_1_5_pro",
        "seedance1.0pro": "seedance_1_0_pro",
        "seedance_1_0_pro": "seedance_1_0_pro",
        "seedance_1_0pro": "seedance_1_0_pro",
        "doubao-seedance-1-0-pro-250528": "seedance_1_0_pro",
        "doubao-seedance-1-0-pro-250428": "seedance_1_0_pro",
    }
    selected = aliases.get(selected, selected)
    if selected == "auto":
        # 自动模式必须先尝试 1.5 Pro；额度/权限级失败由 transport
        # 再降级到 1.0 Pro，而不是在这里提前丢失优先级信息。
        return "seedance_1_5_pro"
    if selected not in VIDEO_MODEL_API_IDS:
        raise MaterialModelSelectionError(
            f"video_model 不受支持：{value!r}；当前可选 Seedance 1.5 Pro、Seedance 1.0 Pro 或 auto"
        )
    return selected


def normalize_frame_continuity(value: Any, *, default: bool = True) -> bool:
    """解析素材层首尾帧开关；用户界面应显式传入 false/true。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if text in {"1", "true", "yes", "y", "on", "enabled", "enable", "use", "开启", "启用", "首尾帧"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "disable", "skip", "关闭", "停用", "不使用"}:
        return False
    raise MaterialModelSelectionError(
        f"use_frame_continuity 不受支持：{value!r}；请传 true/false"
    )


def normalize_material_model_selection(value: Any) -> dict[str, str]:
    data = value if isinstance(value, dict) else {}
    return {
        "image_model": normalize_image_model(data.get("image_model", "auto")),
        "video_model": normalize_video_model(data.get("video_model", "auto")),
    }


__all__ = [
    "IMAGE_MODEL_API_IDS",
    "IMAGE_MODEL_CHOICES",
    "VIDEO_MODEL_API_IDS",
    "VIDEO_MODEL_CHOICES",
    "FRAME_CONTINUITY_CHOICES",
    "MaterialModelSelectionError",
    "normalize_image_model",
    "normalize_material_model_selection",
    "normalize_video_model",
    "normalize_frame_continuity",
]
