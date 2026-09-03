"""视频风格包：定义脚本镜头结构，并自动补齐片头片尾。"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "video-style-package-v1"
FIXED_CATEGORIES = ("opening", "ending")
CATEGORY_LABELS = {
    "opening": "片头",
    "image": "图片",
    "digital_human": "数字人",
    "aigc": "AIGC",
    "explanation": "说明镜头",
    "mixed_explanation": "混合说明镜头",
    "ending": "片尾",
}


class VideoStylePackageError(ValueError):
    """视频风格包结构或映射不合法。"""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe(value: Any, fallback: str) -> str:
    result = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "_", _text(value) or fallback).strip("._")
    if not result:
        raise VideoStylePackageError("视频风格包名称或编号不包含有效字符")
    return result[:80]


def normalize_script_categories(categories: Sequence[Any]) -> list[str]:
    """保留脚本分类顺序、去重，并排除由系统治理的片头片尾。"""

    result: list[str] = []
    for value in categories:
        if isinstance(value, Mapping):
            value = value.get("category") or value.get("shot_category") or value.get("shot_type")
        category = _text(value)
        if not category or category in FIXED_CATEGORIES or category in result:
            continue
        result.append(category)
    if not result:
        raise VideoStylePackageError("至少需要一个脚本镜头分类")
    return result


def categories_from_script_shots(shots: Sequence[Mapping[str, Any]]) -> list[str]:
    values: list[Any] = []
    for shot in shots:
        if isinstance(shot, Mapping):
            values.append(
                shot.get("category")
                or shot.get("shot_category")
                or shot.get("shot_type")
                or shot.get("media_type")
            )
    return normalize_script_categories(values)


def build_video_style_package(
    name: str,
    version: str,
    script_categories: Sequence[Any],
    *,
    style_profile_id: str = "",
    description: str = "",
) -> dict[str, Any]:
    """构建 x+2 视频风格包，不读取或修改任何剪映草稿。"""

    package_name = _text(name)
    if not package_name:
        raise VideoStylePackageError("视频风格包名称不能为空")
    package_version = _text(version) or "1.0"
    script = normalize_script_categories(script_categories)
    ordered = [*script, *FIXED_CATEGORIES]
    return {
        "schema_version": SCHEMA_VERSION,
        "package_type": "video_style_package",
        "video_style_package_id": f"vstyle_{_safe(package_name, 'video_style')}_v{_safe(package_version, '1.0')}",
        "name": package_name,
        "version": package_version,
        "upstream_style_profile_id": _text(style_profile_id),
        "description": _text(description),
        "script_categories": script,
        "fixed_categories": list(FIXED_CATEGORIES),
        "category_order": ordered,
        "required_categories": ordered,
        "category_count": len(ordered),
        "mapping": [
            {
                "category": category,
                "label": CATEGORY_LABELS.get(category, category),
                "role": "fixed" if category in FIXED_CATEGORIES else "script",
                "source": "governed_by_video_style_package",
            }
            for category in ordered
        ],
    }


def validate_video_style_package(package: Mapping[str, Any]) -> dict[str, Any]:
    if package.get("package_type") != "video_style_package":
        raise VideoStylePackageError("视频风格包类型不正确")
    script = normalize_script_categories(package.get("script_categories") or [])
    expected = [*script, *FIXED_CATEGORIES]
    actual = list(package.get("category_order") or [])
    if actual != expected:
        raise VideoStylePackageError("视频风格包必须按脚本分类 + 片头 + 片尾形成 x+2 结构")
    if list(package.get("required_categories") or []) != expected:
        raise VideoStylePackageError("视频风格包 required_categories 与 category_order 不一致")
    if int(package.get("category_count") or 0) != len(expected):
        raise VideoStylePackageError("视频风格包 category_count 与实际分类数不一致")
    return {
        "status": "valid",
        "category_count": len(expected),
        "script_category_count": len(script),
        "category_order": expected,
        "structure": f"{len(script)}+2",
    }


def bind_packaging_manifest(
    manifest: Mapping[str, Any], video_style_package: Mapping[str, Any]
) -> dict[str, Any]:
    """把包装包绑定到风格包，并保存构建时的分类快照。"""

    validate_video_style_package(video_style_package)
    categories = [str(item.get("category")) for item in manifest.get("categories", []) if isinstance(item, Mapping)]
    required = list(video_style_package["required_categories"])
    missing = [item for item in required if item not in categories]
    extra = [item for item in categories if item not in required]
    if extra:
        raise VideoStylePackageError(
            "包装包分类必须与上游视频风格包一致；缺少："
            + "、".join(missing or ["无"])
            + "；多出："
            + "、".join(extra or ["无"])
        )
    result = copy.deepcopy(dict(manifest))
    result["upstream_video_style_package"] = {
        "video_style_package_id": video_style_package["video_style_package_id"],
        "name": video_style_package["name"],
        "version": video_style_package["version"],
        "category_order": required,
        "structure": f"{len(video_style_package['script_categories'])}+2",
    }
    result["validation"] = {
        **dict(result.get("validation") or {}),
        "upstream_style_bound": True,
        "structure": f"{len(video_style_package['script_categories'])}+2",
        "missing_categories": missing,
    }
    return result


def write_video_style_package(package: Mapping[str, Any], output_path: str | Path) -> dict[str, Any]:
    validate_video_style_package(package)
    path = Path(output_path).expanduser().resolve()
    if path.exists():
        raise VideoStylePackageError(f"视频风格包已存在，为避免覆盖已停止：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(package), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dict(package)


def load_video_style_package(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    try:
        package = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VideoStylePackageError(f"无法读取视频风格包：{target}") from exc
    if not isinstance(package, dict):
        raise VideoStylePackageError("视频风格包格式错误")
    validate_video_style_package(package)
    return package


__all__ = [
    "CATEGORY_LABELS",
    "FIXED_CATEGORIES",
    "SCHEMA_VERSION",
    "VideoStylePackageError",
    "bind_packaging_manifest",
    "build_video_style_package",
    "categories_from_script_shots",
    "load_video_style_package",
    "normalize_script_categories",
    "validate_video_style_package",
    "write_video_style_package",
]
