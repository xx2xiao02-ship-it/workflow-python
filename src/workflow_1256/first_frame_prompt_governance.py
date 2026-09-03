"""Deterministic projection from video language to a static first-frame prompt.

This module deliberately does not mutate Director Lock, material planning or
video-prompt fields.  It only compiles a model-visible still-image projection
and returns an audit trail for the asset manifest.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any


_OUTDOOR_SCENE_MARKERS = (
    "路边", "街口", "市场外", "公园", "广场", "街道", "摊位前", "室外", "楼下",
)
_INDOOR_EXTENSION_MARKERS = (
    "室内", "桌旁", "桌面", "房间", "门框", "床边", "厨房", "客厅", "屋内",
)
_TEMPORAL_MOTION_MARKERS = (
    "缓慢横摇", "横摇", "摇镜", "跟拍", "横移", "推近", "拉远", "环绕", "扫过",
    "移动轨迹", "继续", "逐渐", "随后", "最终", "慢慢", "缓慢", "跑几步", "停下",
    "滑开", "收回", "靠近", "下滑", "恢复静止",
    "让出", "跟随", "离开",
)
_SCENE_REFERENCE_RE = re.compile(r"\bscene[_\s-]*\d+\b", re.IGNORECASE)
_MULTI_ACTION_SPLIT_RE = re.compile(r"[，、；;]\s*(?:又|再|随后|然后|并|接着|回头|望向|依次)\s*")


def _text(value: object) -> str:
    return str(value or "").strip()


def _dedupe_warnings(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _staticize_temporal_phrase(value: str) -> tuple[str, bool]:
    """Turn an end-state phrase into a still-image state without inventing it."""

    original = value
    value = value.replace("微微下陷后停住", "呈现下陷状态")
    value = value.replace("轻颤一下后恢复静止", "呈现轻微起伏状态")
    value = re.sub(r"(?:随后|然后|最终)\s*停住", "", value)
    value = value.replace("后停住", "保持当前状态")
    value = re.sub(r"^(?:随后|然后|最终)\s*", "", value).strip(" ，、；;")
    value = re.sub(r"(?:随后|然后|最终)[^，、；;。]*$", "", value).strip(" ，、；;")
    if value in {"停住", "停止", "定住", "停在"}:
        value = ""
    return value, value != original


def has_static_scene_conflict(scene: object, extension: object) -> bool:
    """Identify a high-confidence outdoor-scene / indoor-extension conflict."""

    scene_text = _text(scene)
    extension_text = _text(extension)
    return (
        any(marker in scene_text for marker in _OUTDOOR_SCENE_MARKERS)
        and any(marker in extension_text for marker in _INDOOR_EXTENSION_MARKERS)
    )


def render_scene_continuity_for_image(
    continuity: object,
    *,
    scene: object = "",
    action: object = "",
) -> tuple[str, list[str]]:
    """Render only image-executable continuity; never expose internal scene IDs.

    The current fallback can carry a symbolic ``scene_02`` reference without
    concrete people, props or spatial state.  Such text is intentionally
    omitted rather than invented.
    """

    value = _text(continuity)
    if not value:
        return "", []
    if _SCENE_REFERENCE_RE.search(value) or "当前段落相关" in value or "既有物件状态与动作余波" in value:
        return "", ["SCENE_CONTINUITY_OMITTED"]
    if _SCENE_REFERENCE_RE.search(_text(scene)) or _SCENE_REFERENCE_RE.search(_text(action)):
        return "", ["SCENE_CONTINUITY_OMITTED"]
    return value, []


def normalize_single_action(
    action: object,
    *,
    frame_intent: object = "",
    key_prop: object = "",
) -> tuple[str, list[str]]:
    """Keep one dominant visible action for a still frame without changing intent."""

    value = _text(action)
    if not value:
        return "", []
    if _SCENE_REFERENCE_RE.search(value) or "当前段落相关" in value or "既有物件状态与动作余波" in value:
        return "", ["SCENE_CONTINUITY_OMITTED"]

    value, staticized = _staticize_temporal_phrase(value)
    match = _MULTI_ACTION_SPLIT_RE.search(value)
    if not match:
        return value, ["STATIC_TEMPORAL_NORMALIZED"] if staticized else []

    primary = value[:match.start()].strip("，、；; ")
    if not primary:
        # A malformed leading connector has no deterministic primary action.
        return value, ["MULTI_ACTION_NORMALIZED"]
    # Existing frame intent/key prop are intentionally inputs only: they make
    # the selection auditable but never cause a new action to be invented.
    _ = (frame_intent, key_prop)
    return primary, _dedupe_warnings([
        "MULTI_ACTION_NORMALIZED",
        "STATIC_TEMPORAL_NORMALIZED" if staticized else "",
    ])


def normalize_visibility_for_static_frame(action: object, shot_size: object) -> tuple[str, list[str]]:
    """Make micro-process wording legible in wide shots while retaining shot size."""

    value = _text(action)
    if not value or _text(shot_size) not in {"超远景", "远景", "全景"}:
        return value, []

    replacements = (
        ("指尖轻蹭", "手指触及"),
        ("指尖轻轻", "手指明显"),
        ("微微凹陷", "清晰凹下"),
        ("轻颤一下", "形成可见轻微起伏"),
        ("轻轻下滑一点", "停在较低位置"),
        ("极轻微", "清晰可见的"),
    )
    normalized = value
    for source, target in replacements:
        normalized = normalized.replace(source, target)
    if normalized != value:
        return normalized, ["STATIC_VISIBILITY_NORMALIZED"]
    return value, []


def project_motion_to_static_frame(
    motion: object,
    *,
    scene: object = "",
    action: object = "",
    shot_size: object = "",
) -> tuple[str, list[str]]:
    """Project video motion to a still composition cue.

    Camera movement and timeline progression are removed.  A remaining static
    relation such as "透过半开的门框形成框景" is retained unless it conflicts
    with the scene truth.  Source fields remain untouched by this function.
    """

    if isinstance(motion, Sequence) and not isinstance(motion, (str, bytes)):
        parts: list[str] = []
        for item in motion:
            if isinstance(item, Mapping):
                parts.extend((_text(item.get("camera_motion")), _text(item.get("action"))))
            else:
                parts.append(_text(item))
        source = "；".join(part for part in parts if part)
    else:
        source = _text(motion)
    if not source:
        return "画面定格在主体与关键物件形成清晰关系的瞬间", []
    if has_static_scene_conflict(scene, source):
        return "画面定格在当前场景中主体与关键物件的关系瞬间", ["STATIC_SCENE_CONFLICT"]

    clauses = [part.strip(" ，、；;") for part in re.split(r"[，、；;。]\s*", source) if part.strip(" ，、；;")]
    retained: list[str] = []
    warnings: list[str] = []
    for clause in clauses:
        static_clause, staticized = _staticize_temporal_phrase(clause)
        if staticized:
            warnings.append("STATIC_TEMPORAL_NORMALIZED")
        if static_clause and not any(marker in static_clause for marker in _TEMPORAL_MOTION_MARKERS):
            retained.append(static_clause)
    if len(retained) != len(clauses):
        warnings.append("VIDEO_MOTION_PROJECTED_TO_STATIC")
    if retained:
        return "，".join(dict.fromkeys(retained)), warnings

    _ = (action, shot_size)
    return "画面定格在主体与关键物件形成清晰关系的瞬间", _dedupe_warnings(
        [*warnings, "VIDEO_MOTION_PROJECTED_TO_STATIC"]
    )


def govern_static_frame_fields(
    *,
    scene: object,
    action: object,
    motion: object,
    staging: object,
    shot_size: object,
    frame_intent: object = "",
    key_prop: object = "",
) -> dict[str, object]:
    """Return non-mutating, deterministic image-only fields plus warnings."""

    warnings: list[str] = []
    continuity, continuity_warnings = render_scene_continuity_for_image(
        staging, scene=scene, action=action,
    )
    warnings.extend(continuity_warnings)
    normalized_action, action_warnings = normalize_single_action(
        action, frame_intent=frame_intent, key_prop=key_prop,
    )
    warnings.extend(action_warnings)
    normalized_action, visibility_warnings = normalize_visibility_for_static_frame(
        normalized_action, shot_size,
    )
    warnings.extend(visibility_warnings)
    static_motion, motion_warnings = project_motion_to_static_frame(
        motion,
        scene=scene,
        action=normalized_action,
        shot_size=shot_size,
    )
    warnings.extend(motion_warnings)
    return {
        "scene": _text(scene),
        "action": normalized_action,
        "static_camera_description": static_motion,
        "staging": continuity,
        "warnings": _dedupe_warnings(warnings),
    }


def normalize_fallback_composition(composition: object, *, known_character_count: int | None) -> str:
    """Keep deterministic fallback composition compatible with known cast size."""

    value = _text(composition)
    if value == "多人层级" and known_character_count is not None and known_character_count <= 1:
        return "纵向主体—物件关系"
    return value
