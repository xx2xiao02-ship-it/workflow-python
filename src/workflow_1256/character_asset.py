"""主角角色锚定图：一次生成，供所有人物镜头作为身份参考。"""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from typing import Any

from .canvas import canvas_prompt_label, resolve_canvas
from .reference_image_publish import split_reference_inputs

class CharacterAssetValidationError(ValueError):
    pass

def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CharacterAssetValidationError(f"{field} 必须是非空字符串")
    return value.strip()

def build_character_asset_plan(cinematic_story: Mapping[str, Any], *, style_ref_images: Sequence[str], canvas: Any = None) -> dict[str, Any]:
    outline = cinematic_story.get("movie_outline") if isinstance(cinematic_story, Mapping) else None
    if not isinstance(outline, Mapping):
        raise CharacterAssetValidationError("cinematic_story.movie_outline 必须是对象")
    protagonist = _text(outline.get("protagonist"), "movie_outline.protagonist")
    raw_refs = [_text(item, f"style_ref_images[{index}]") for index, item in enumerate(style_ref_images)]
    remote_refs, local_refs = split_reference_inputs(raw_refs)
    canvas_spec = resolve_canvas(canvas)
    if not raw_refs:
        return {"status": "blocked", "reason": "缺少参考图画风", "character_anchor": protagonist, "image_urls": []}
    return {
        "status": "planned" if not local_refs else "planned_local_upload",
        "requirement_id": "project.image.character_anchor", "character_anchor": protagonist,
        "canvas": canvas_spec.to_dict(),
        "prompt": f"基于全部参考图画风生成固定角色资产图。角色设定：{protagonist}。{canvas_prompt_label(canvas_spec.to_dict())}、全身、脸部发型服装稳定、中性背景；不要文字品牌复杂界面或其他人物。此图供后续人物镜头保持身份一致。",
        "image_urls": remote_refs, "local_file_paths": local_refs,
        "n": 1, "size": "auto", "resolution": "1k", "official_fallback": False,
    }

def inject_character_asset_refs(style_refs: Sequence[str], *, character_asset_url: str, has_character: bool) -> list[str]:
    refs = [_text(item, f"style_refs[{index}]") for index, item in enumerate(style_refs)]
    if not has_character:
        return refs
    return list(dict.fromkeys([_text(character_asset_url, "character_asset_url"), *refs]))

__all__ = ["CharacterAssetValidationError", "build_character_asset_plan", "inject_character_asset_refs"]
