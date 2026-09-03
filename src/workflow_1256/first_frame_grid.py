"""首帧多宫格规划、数字人跳过和等尺寸裁切。

这个模块只负责首帧资产的编排与本地裁切，不直接调用图片 API。图片任务
transport 由上层注入；这样可以先用离线契约验证索引、网格尺寸和跳过规则，
再接入真实的 Image2 请求。
"""

from __future__ import annotations

import json
import math
import re
import threading
import statistics
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .canvas import CanvasValidationError, resolve_canvas
from .first_frame_prompt_governance import govern_static_frame_fields
from .first_frame_prompt_reviewer import (
    ImagePromptReviewerConfig,
    LightweightImagePromptReviewer,
    build_reviewer_input,
    summarize_review_audits,
)


class FirstFrameGridValidationError(ValueError):
    """首帧多宫格输入或布局不符合契约。"""


class FirstFrameGridCropError(RuntimeError):
    """首帧多宫格图片无法按等尺寸网格裁切。"""


class FirstFrameGridBatchError(RuntimeError):
    """首帧多宫格批处理未能完成。"""


_GRID_CHECKPOINT_VERSION = 1
_GRID_CHECKPOINT_LOCK = threading.Lock()


def _grid_checkpoint_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _grid_checkpoint_handle_dir(path: Path) -> Path:
    return path.with_name(f"{path.stem}.handles")


def _grid_checkpoint_handle_path(path: Path, grid_id: str) -> Path:
    safe_grid_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(grid_id or "").strip()) or "grid"
    return _grid_checkpoint_handle_dir(path) / f"{safe_grid_id}.json"


def persist_grid_checkpoint_handle(path: str | Path, grid_id: str, values: Mapping[str, Any]) -> None:
    """Persist one grid handle so restart can recover it."""

    checkpoint_file = Path(path)
    grid_key = str(grid_id or "").strip()
    if not grid_key:
        return
    handle_path = _grid_checkpoint_handle_path(checkpoint_file, grid_key)
    payload = {"grid_id": grid_key, **dict(values), "updated_at": _grid_checkpoint_timestamp()}
    temporary = handle_path.with_name(f".{handle_path.name}.{threading.get_ident()}.tmp")
    handle_path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(handle_path)
    with _GRID_CHECKPOINT_LOCK:
        checkpoint_payload: dict[str, Any] = {"version": _GRID_CHECKPOINT_VERSION, "updated_at": payload["updated_at"], "batches": {}}
        if checkpoint_file.is_file():
            try:
                existing = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if isinstance(existing, Mapping):
                checkpoint_payload.update({key: value for key, value in existing.items() if key != "batches"})
                batches = existing.get("batches")
                if isinstance(batches, Mapping):
                    checkpoint_payload["batches"] = {str(key): dict(value) if isinstance(value, Mapping) else value for key, value in batches.items()}
        batches = checkpoint_payload.get("batches")
        if not isinstance(batches, dict):
            batches = {}
        existing_batch = dict(batches.get(grid_key) or {})
        existing_batch.update(payload)
        batches[grid_key] = existing_batch
        checkpoint_payload["version"] = _GRID_CHECKPOINT_VERSION
        checkpoint_payload["updated_at"] = payload["updated_at"]
        checkpoint_payload["batches"] = batches
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_temp = checkpoint_file.with_name(f".{checkpoint_file.name}.{threading.get_ident()}.tmp")
        checkpoint_temp.write_text(json.dumps(checkpoint_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        checkpoint_temp.replace(checkpoint_file)


def _load_grid_checkpoint_handles(path: str | Path) -> dict[str, dict[str, Any]]:
    checkpoint_file = Path(path)
    loaded: dict[str, dict[str, Any]] = {}
    if checkpoint_file.is_file():
        try:
            checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            checkpoint = {}
        if isinstance(checkpoint, Mapping):
            batches = checkpoint.get("batches")
            if isinstance(batches, Mapping):
                for grid_id, value in batches.items():
                    grid_key = str(grid_id or "").strip()
                    if grid_key and isinstance(value, Mapping):
                        loaded[grid_key] = dict(value)
    handle_dir = _grid_checkpoint_handle_dir(checkpoint_file)
    if handle_dir.is_dir():
        for handle_path in sorted(handle_dir.glob("*.json")):
            try:
                handle = json.loads(handle_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(handle, Mapping):
                continue
            grid_key = str(handle.get("grid_id") or handle_path.stem).strip()
            if not grid_key:
                continue
            merged = dict(loaded.get(grid_key) or {})
            merged.update({key: value for key, value in handle.items() if value not in (None, "") or key == "grid_id"})
            loaded[grid_key] = merged
    return loaded


GRID_LAYOUT_CHOICES = ("2x2", "3x3")

_GRID_POSITION_NAMES = {
    (2, 2): ("左上", "右上", "左下", "右下"),
    (3, 3): ("左上", "上中", "右上", "左中", "正中", "右中", "左下", "下中", "右下"),
}

# 这是 Image2 代理当前使用的首帧单格尺寸，不是用户输入字段。
# 用户只选择项目级 aspect_ratio，网格单格尺寸由这里统一派生。
_GRID_CELL_DIMENSIONS = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
    "1:1": (1024, 1024),
    "3:2": (1536, 1024),
    "2:3": (1024, 1536),
}


def normalize_grid_layout(value: Any, *, default: str = "2x2") -> str:
    """规范化用户可选的固定宫格布局。"""
    selected = str(value or default).strip().lower().replace("×", "x").replace("*", "x")
    if selected not in GRID_LAYOUT_CHOICES:
        raise FirstFrameGridValidationError(
            f"grid_layout 不受支持：{value!r}；请选择 2x2 或 3x3"
        )
    return selected


def _grid_shape(grid_layout: str) -> tuple[int, int]:
    selected = normalize_grid_layout(grid_layout)
    side = int(selected[0])
    return side, side


def _effective_grid_layout(requested_layout: str, item_count: int) -> str:
    """Resolve the smallest supported layout that fits the current batch.

    ``grid_layout`` is a user-selected maximum layout. A trailing batch with
    one to four shots is always rendered as a 2x2 grid; empty positions remain
    explicit placeholders and are discarded after cropping.
    """

    if item_count <= 0:
        raise FirstFrameGridValidationError("当前批次至少需要一个普通镜头")
    selected = normalize_grid_layout(requested_layout)
    if selected == "3x3" and item_count <= 4:
        return "2x2"
    return selected


HOST_ROUTE_TOKENS = {
    "host",
    "digital_human",
    "digitalhuman",
    "infinitetalk",
    "数字人",
    "数字人*",
    "数字人镜头",
}


@dataclass(frozen=True)
class GridLayout:
    """无分割线、无间隙的等尺寸网格。"""

    rows: int
    columns: int
    cell_width: int
    cell_height: int
    separation_px: int = 0

    def __post_init__(self) -> None:
        if self.rows <= 0 or self.columns <= 0:
            raise FirstFrameGridValidationError("rows 和 columns 必须是正整数")
        if self.cell_width <= 0 or self.cell_height <= 0:
            raise FirstFrameGridValidationError("cell_width 和 cell_height 必须是正整数")
        if self.separation_px != 0:
            raise FirstFrameGridValidationError("首帧多宫格不允许存在分割隔断或间隙")

    @property
    def total_width(self) -> int:
        return self.columns * self.cell_width

    @property
    def total_height(self) -> int:
        return self.rows * self.cell_height

    @property
    def capacity(self) -> int:
        return self.rows * self.columns

    def cell_box(self, cell_index: int) -> tuple[int, int, int, int]:
        if cell_index < 0 or cell_index >= self.capacity:
            raise FirstFrameGridValidationError(f"cell_index 超出网格容量：{cell_index}")
        row, column = divmod(cell_index, self.columns)
        left = column * self.cell_width
        top = row * self.cell_height
        return left, top, left + self.cell_width, top + self.cell_height

    def to_dict(self) -> dict[str, int]:
        return {
            "rows": self.rows,
            "columns": self.columns,
            "cell_width": self.cell_width,
            "cell_height": self.cell_height,
            "separation_px": 0,
            "total_width": self.total_width,
            "total_height": self.total_height,
        }


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return []
    return [value]


def _tokens(value: Any) -> list[str]:
    values = _as_list(value)
    result: list[str] = []
    for item in values:
        text = _text(item).lower().replace("-", "_").replace(" ", "_")
        if text:
            result.append(text)
    return result


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "y", "on", "是", "是的"}


def _is_host_token(value: Any) -> bool:
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    return text in HOST_ROUTE_TOKENS or any(
        token in text for token in ("digital_human", "digitalhuman", "infinitetalk", "数字人")
    )


def is_digital_human_shot(shot: Any) -> bool:
    """只依据显式路由/标记判断数字人镜头。

    ``route_candidates`` 只有在唯一候选为 host 时才会触发跳过；若同时有
    scene/symbol 等候选，说明还未完成路由选择，不能擅自跳过普通镜头。
    """

    if not isinstance(shot, Mapping):
        return False

    for key in (
        "is_digital_human",
        "digital_human",
        "use_digital_human",
        "is_host",
        "use_host",
    ):
        value = shot.get(key)
        if _truthy(value):
            return True

    for key in (
        "selected_route",
        "route",
        "asset_route",
        "generation_route",
        "render_route",
        "clip_role",
        "shot_type",
        "scene_type",
        "material_type",
    ):
        if _is_host_token(shot.get(key)):
            return True

    candidates = _tokens(shot.get("route_candidates"))
    return bool(candidates) and all(_is_host_token(item) for item in candidates)


def _shot_prompt(shot: Mapping[str, Any]) -> str:
    # 素材清单同时携带首帧提示词和视频提示词时，宫格只能读取首帧专用字段。
    # 若先读取通用 prompt，会把视频提示词里的镜头语言覆盖掉，导致景别、视角、构图丢失。
    for key in ("compiled_first_frame_prompt", "grid_prompt", "first_frame_prompt", "prompt", "visual_prompt", "description"):
        value = _text(shot.get(key))
        if value:
            return value
    raise FirstFrameGridValidationError("每个非数字人镜头必须提供 prompt")


def _shot_id(shot: Mapping[str, Any], index: int) -> str:
    return _text(shot.get("shot_id") or shot.get("id")) or f"shot_{index + 1:03d}"


def _reference_images(shot: Mapping[str, Any]) -> list[str]:
    for key in ("ref_images", "ref_image", "image_urls"):
        values = [_text(item) for item in _as_list(shot.get(key))]
        values = [item for item in values if item]
        if values:
            return values
    return []


def _prompt_field(prompt: str, name: str) -> str:
    marker = f"【{name}】"
    if marker not in prompt:
        return ""
    return prompt.split(marker, 1)[1].split("【", 1)[0].strip()


def _shot_visual_value(shot: Mapping[str, Any], key: str, *fallback_keys: str) -> str:
    """显式读取编导镜头字段，并兼容嵌套视觉导演书。"""

    for name in (key, *fallback_keys):
        value = _text(shot.get(name))
        if value:
            return value
    direction = shot.get("visual_direction")
    if isinstance(direction, Mapping):
        for name in (key, *fallback_keys):
            value = _text(direction.get(name))
            if value:
                return value
    return ""


def _camera_prompt_value(prompt: str, label: str) -> str:
    """读取新旧两种“镜头语言”格式中的单项字段。"""

    explicit = _prompt_field(prompt, "镜头语言")
    if not explicit:
        return ""
    marker = f"{label}="
    if marker in explicit:
        value = explicit.split(marker, 1)[1]
        boundary = re.search(r"(?:\n\s*【[^】]+】|[；;。])", value)
        if boundary is not None:
            value = value[:boundary.start()]
        return value.strip()
    parts = [part.strip() for part in explicit.replace("；", "，").split("，") if part.strip()]
    order = {"景别": 0, "视角": 1, "机位": 1, "构图": 2}
    index = order.get(label)
    if index is None or index >= len(parts):
        return ""
    if label == "构图":
        return "，".join(parts[index:])
    return parts[index]


def _camera_motion_text(cell: Mapping[str, Any]) -> str:
    motion = _text(cell.get("motion_seed"))
    camera_motion = cell.get("camera_motion")
    if isinstance(camera_motion, Sequence) and not isinstance(camera_motion, (str, bytes)):
        parts: list[str] = []
        for item in camera_motion:
            if isinstance(item, Mapping):
                value = "，".join(filter(None, (_text(item.get("camera_motion")), _text(item.get("action")))))
            else:
                value = _text(item)
            if value and value not in parts:
                parts.append(value)
        if parts:
            return "；".join(parts)
    return motion


def _clean_prompt_fragment(value: Any) -> str:
    """Remove transport/layout metadata before a fragment enters model prose."""

    text = _text(value)
    if not text:
        return ""
    # Resolution is an API/layout parameter, not scene content.  It often
    # arrives inside the scene field as ``（1920×1080）`` or ``1920x1080``.
    text = re.sub(r"[（(]\s*\d{3,5}\s*[×xX]\s*\d{3,5}\s*[）)]", "", text)
    text = re.sub(r"\b\d{3,5}\s*[×xX]\s*\d{3,5}\b", "", text)
    # Upstream snapshots may carry the director's field label inside an
    # already-extracted value. Strip it so the final prompt never falls back
    # to ``景别=...``/``构图=...`` syntax when a cell is rendered.
    field_prefix = re.compile(
        r"^(?:场景|主体(?:与动作)?|景别|视角|机位|构图|调度|"
        r"光线(?:与氛围)?|镜头(?:动势|语言)?)[：=:]\s*"
    )
    for _ in range(2):
        cleaned = field_prefix.sub("", text).strip()
        if cleaned == text:
            break
        text = cleaned
    return re.sub(r"\s{2,}", " ", text).strip(" \t\r\n；;，,")


def _requests_miniature_scene(value: Any) -> bool:
    """仅在用户明确要求时强化“每格微缩场景”，不擅自改变其他项目画风。"""

    text = _text(value)
    return any(token in text for token in ("微缩场景", "微缩", "微型场景", "迷你场景"))


def _requests_white_background(value: Any) -> bool:
    text = _text(value)
    return any(token in text for token in ("纯白色背景", "纯白背景", "白色背景"))


def _validated_expanded_guidance(guidance: Mapping[str, Any], user_prompt: str) -> list[str]:
    """只接收强化器已审计、且能追溯到用户原文的全局约束。

    不能因为历史快照或任意字段里出现 ``enhanced_directive``，就让未经
    校验的文本进入真正的图片 Prompt。每条强化语必须标明原文短语来源。
    """

    if _text(guidance.get("guidance_expansion_status")).upper() != "EXPANDED":
        return []
    raw_directives = guidance.get("guidance_expansion_directives")
    if not isinstance(raw_directives, Sequence) or isinstance(raw_directives, (str, bytes)):
        return []
    directives: list[str] = []
    for item in raw_directives:
        if not isinstance(item, Mapping):
            continue
        source_phrase = _text(item.get("source_phrase"))
        instruction = _text(item.get("instruction"))
        if not source_phrase or source_phrase not in user_prompt or not instruction:
            continue
        if len(instruction) > 480 or "【" in instruction or re.search(r"\bscene_\d+\b|镜头编号|shot_id", instruction, re.IGNORECASE):
            continue
        if instruction not in directives:
            directives.append(instruction)
    return directives


def _as_sentence(value: Any) -> str:
    text = _clean_prompt_fragment(value)
    if not text:
        return ""
    return text.rstrip("。！？!?；;，, ") + "。"


def _cell_static_projection(cell: Mapping[str, Any]) -> dict[str, object]:
    """Compile grid cell prose from deterministic image-only fields."""

    prompt = _text(cell.get("prompt"))
    scene = _clean_prompt_fragment(_prompt_field(prompt, "场景")) or _clean_prompt_fragment(cell.get("semantic_anchor")) or "叙事场景"
    action = _clean_prompt_fragment(_prompt_field(prompt, "主体与动作")) or _clean_prompt_fragment(cell.get("motion_seed")) or "完成一个清晰可见的单一动作"
    shot_size = _clean_prompt_fragment(cell.get("shot_size")) or _clean_prompt_fragment(_camera_prompt_value(prompt, "景别")) or "中景"
    viewpoint = _clean_prompt_fragment(cell.get("viewpoint")) or _clean_prompt_fragment(_camera_prompt_value(prompt, "视角")) or _clean_prompt_fragment(_camera_prompt_value(prompt, "机位")) or "平视"
    composition = _clean_prompt_fragment(cell.get("composition")) or _clean_prompt_fragment(_camera_prompt_value(prompt, "构图")) or "主体明确、单一动作"
    staging = _clean_prompt_fragment(cell.get("staging")) or _clean_prompt_fragment(_prompt_field(prompt, "调度"))
    lighting = _clean_prompt_fragment(_prompt_field(prompt, "光线与氛围")) or "自然电影光"
    governed = govern_static_frame_fields(
        scene=scene,
        action=action,
        motion=_camera_motion_text(cell),
        staging=staging,
        shot_size=shot_size,
        frame_intent=cell.get("semantic_anchor"),
        key_prop=cell.get("key_prop") or cell.get("key_props"),
    )
    return {
        "scene": scene,
        # Do not resurrect an omitted internal continuity token from source.
        "action": str(governed["action"] or "").strip(),
        "shot_size": shot_size,
        "viewpoint": viewpoint,
        "composition": composition,
        "staging": str(governed["staging"] or "").strip(),
        "lighting": lighting,
        "static_camera_description": str(governed["static_camera_description"] or "").strip(),
        "warnings": list(governed["warnings"]),
    }


def _cell_grid_instruction(cell: Mapping[str, Any]) -> str:
    """把逐镜导演字段投影为自然语言单格指令，不暴露内部字段名。"""

    # Reviewer 只允许在单格范围内返回最小修复；Grid 的位置和顺序仍由
    # 本模块控制。compiled_cell_prompt 永远保留，便于审计和失败回退。
    cached = _text(cell.get("final_first_frame_prompt")) or _text(cell.get("compiled_cell_prompt"))
    if cached:
        return cached
    projection = _cell_static_projection(cell)
    sentences = [
        _as_sentence(projection["scene"]),
        _as_sentence(projection["action"]),
        _as_sentence(f"画面采用{projection['shot_size']}，以{projection['viewpoint']}视角拍摄，构图突出{projection['composition']}"),
        _as_sentence(projection["static_camera_description"]),
        _as_sentence(projection["staging"] or "人物与关键物件完成一个清晰可见的单一动作"),
        _as_sentence(f"光线保持{projection['lighting']}"),
    ]
    return "".join(item for item in sentences if item)


def _columns_for_count(count: int, requested: int | None) -> int:
    if requested is not None:
        if requested <= 0:
            raise FirstFrameGridValidationError("columns 必须是正整数")
        return min(requested, count)
    return max(1, math.ceil(math.sqrt(count)))


def _layout_for_count(
    count: int,
    *,
    cell_width: int,
    cell_height: int,
    columns: int | None = None,
) -> GridLayout:
    if count <= 0:
        raise FirstFrameGridValidationError("多宫格至少需要一个普通镜头")
    selected_columns = _columns_for_count(count, columns)
    rows = math.ceil(count / selected_columns)
    return GridLayout(rows, selected_columns, cell_width, cell_height)


def _format_aspect_ratio(width: int, height: int) -> str:
    """Return a reduced ``width:height`` ratio for prompt and request metadata."""

    if width <= 0 or height <= 0:
        raise FirstFrameGridValidationError("width/height 必须是正整数")
    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def _resolve_grid_dimensions(
    *,
    aspect_ratio: Any = None,
    cell_width: int | None = None,
    cell_height: int | None = None,
) -> tuple[str, int, int]:
    """把编导层画幅解析为多宫格单格尺寸，并兼容旧的显式宽高调用。"""

    has_width = cell_width is not None
    has_height = cell_height is not None
    if has_width != has_height:
        raise FirstFrameGridValidationError(
            "cell_width 和 cell_height 必须同时提供；业务调用应优先只提供 aspect_ratio"
        )
    if has_width and has_height:
        if isinstance(cell_width, bool) or isinstance(cell_height, bool):
            raise FirstFrameGridValidationError("cell_width 和 cell_height 必须是正整数")
        if not isinstance(cell_width, int) or not isinstance(cell_height, int):
            raise FirstFrameGridValidationError("cell_width 和 cell_height 必须是正整数")
        if cell_width <= 0 or cell_height <= 0:
            raise FirstFrameGridValidationError("cell_width 和 cell_height 必须是正整数")
        derived_ratio = _format_aspect_ratio(cell_width, cell_height)
        if aspect_ratio is None or aspect_ratio == "":
            return derived_ratio, cell_width, cell_height
        try:
            resolved = resolve_canvas(aspect_ratio)
        except CanvasValidationError as exc:
            raise FirstFrameGridValidationError(str(exc)) from exc
        if derived_ratio != resolved.aspect_ratio:
            raise FirstFrameGridValidationError(
                f"aspect_ratio={resolved.aspect_ratio} 与 cell_width/cell_height={derived_ratio} 冲突"
            )
        return resolved.aspect_ratio, cell_width, cell_height

    try:
        resolved = resolve_canvas(aspect_ratio)
    except CanvasValidationError as exc:
        raise FirstFrameGridValidationError(str(exc)) from exc
    dimensions = _GRID_CELL_DIMENSIONS.get(resolved.aspect_ratio)
    if dimensions is None:
        raise FirstFrameGridValidationError(
            f"首帧多宫格暂不支持画幅：{resolved.aspect_ratio}"
        )
    return resolved.aspect_ratio, dimensions[0], dimensions[1]


def build_grid_prompt(batch: Mapping[str, Any]) -> str:
    """生成多宫格统一提示词，合并参考图特征和用户注意力引导。"""

    layout = batch["layout"]
    if isinstance(layout, GridLayout):
        layout_data = layout.to_dict()
    else:
        layout_data = dict(layout)

    rows = int(layout_data["rows"])
    columns = int(layout_data["columns"])
    cell_width = int(layout_data["cell_width"])
    cell_height = int(layout_data["cell_height"])
    cell_aspect_ratio = _format_aspect_ratio(cell_width, cell_height)
    canvas_aspect_ratio = _format_aspect_ratio(
        columns * cell_width,
        rows * cell_height,
    )
    has_reference_images = bool(batch.get("has_reference_images")) or any(
        cell.get("reference_images")
        for cell in batch.get("cells", [])
        if isinstance(cell, Mapping)
    )
    attention_prompts = []
    expanded_guidance_lines = []
    for cell in batch.get("cells", []):
        guidance = cell.get("visual_guidance") if isinstance(cell, Mapping) else None
        # 只有已标记为用户输入的文本才进入最终 prompt。早期保存文本由
        # 素材任务/预览入口先规范化为 explicit_user；模型生成的
        # positive/negative/reference_usage 不属于用户原文。
        if not isinstance(guidance, Mapping) or str(guidance.get("source") or "").strip() != "explicit_user":
            continue
        prompt = str(guidance.get("user_prompt") or "").strip()
        if prompt and prompt not in attention_prompts:
            attention_prompts.append(prompt)
        for instruction in _validated_expanded_guidance(guidance, prompt):
            if instruction not in expanded_guidance_lines:
                expanded_guidance_lines.append(instruction)
    miniature_scene_requested = any(_requests_miniature_scene(prompt) for prompt in attention_prompts)
    white_background_requested = any(_requests_white_background(prompt) for prompt in attention_prompts)

    opening = (
        f"根据参考图的画风、笔触、色调与角色基础连续性，生成一张{rows}行{columns}列的无缝多宫格首帧图。"
        if has_reference_images
        else f"生成一张{rows}行{columns}列的无缝多宫格首帧图。"
    )
    lines = [opening]
    # 用户输入的注意力引导必须紧跟在开场句之后，且作为自然语言原文直接
    # 进入模型，不添加“注意力引导”等模板标题；不再自动加入视觉模型的
    # positive/negative 输出，也不改写用户原文。
    if attention_prompts:
        lines.append("；".join(attention_prompts))
    # 只在用户原文之后追加已通过来源校验的增强约束。这样用户仍能一眼
    # 看见自己写了什么，模型扩写也不能伪装成用户输入或覆盖其要求。
    lines.extend(expanded_guidance_lines)
    if miniature_scene_requested:
        background_clause = (
            "；每格画布的外部背景必须保持连续纯白，只保留该格微缩场景必要的地面或底座，"
            "不得用街道、房间、天空、墙面或摄影棚填满整格"
            if white_background_requested
            else ""
        )
        lines.append(
            "必须先分别构思每一格，再排成九宫格：每格都是独立、完整的微缩场景，"
            "以微缩模型或小型舞台的尺度感呈现；主体、关键道具、局部环境与前中后景关系必须在本格内闭合"
            f"{background_clause}。不得把九格当作一张连续大场景后再裁切，"
            "不得将任何一格绘制成真人尺度的电影实景或铺满画幅的连续背景，"
            "不得让人物、道具、道路、桌面或动作跨格延伸。"
        )
    lines.extend([
        "所有宫格必须完全等宽、等高、同一输出尺寸，宫格之间零像素间隙。",
        "严禁分割线、白线、黑线、边框、留白隔断、阴影隔断、网格线和拼贴缝隙。",
        "相邻格的画布边缘应零间隙对齐，但相邻格的叙事空间不得相互延伸；不要绘制格子编号、文字、标题、标签或任何说明。",
        "无缝仅表示没有边框、间隙、网格线或留白隔断；每格的场景、主体、道具与镜头关系都必须在本格内完整闭合。",
        "每个宫格只生成对应镜头画面；相邻格必须使用不同的动作瞬间、人物关系或空间构图，不得机械复制上一格的主体姿态、人物组合、关键道具布局或场景构图。",
        "参考图仅用于继承画风、笔触、色调与角色基础连续性；严禁复制参考图中的具体人物数量、服饰、姿势、道具、地点、构图或情节。",
    ])
    lines.append(
        f"\u6bcf\u4e2a\u5bab\u683c\u753b\u5e45\u56fa\u5b9a\u4e3a {cell_aspect_ratio}\uff0c\u6574\u5f20\u7f51\u683c\u753b\u5e03\u4e5f\u4e3a {canvas_aspect_ratio}\uff1b\u8bf7\u6309\u5bab\u683c\u72ec\u7acb\u5e03\u666f\uff01",
    )
    unused_cell_count = int(batch.get("unused_cell_count", 0))
    if unused_cell_count:
        lines.append(
            f"剩余{unused_cell_count}个未使用宫格必须填充统一的深蓝抽象纹理占位画面："
            "无人物、无文字、无数字、无留白、无纯黑或纯白空格；这些位置将在裁切后丢弃。"
        )
    position_names = {
        (2, 2): ("左上", "右上", "左下", "右下"),
        (3, 3): ("左上", "上中", "右上", "左中", "正中", "右中", "左下", "下中", "右下"),
    }
    positions = position_names.get((rows, columns))
    if positions is not None:
        lines.append("格位必须严格按以下空间位置排列，禁止改成条漫、连环画或任意其他排列。")
    for cell in batch.get("cells", []):
        cell_index = int(cell["cell_index"])
        position = positions[cell_index] if positions is not None else f"第{cell_index + 1}格"
        miniature_rule = ""
        if miniature_scene_requested:
            miniature_rule = (
                "这一格必须是独立、完整的微缩模型小舞台；先完成本格内的主体、关键道具、局部环境和前中后景关系。"
                "即使本镜是近景或特写，也必须表现为该微缩舞台中被放大的局部，不能退化为真人尺度实景。"
                + (
                    "本格外部必须是纯白背景，禁止用街道、房间、天空、墙面或摄影棚填满画幅；"
                    "只保留必要的小型底座或地面。"
                    if white_background_requested
                    else ""
                )
                + "禁止任何人物、道具或动作跨到相邻格。"
            )
        lines.append(f"【{position}】画面安排：{miniature_rule}{_cell_grid_instruction(cell)}")
    return "\n".join(lines)


def build_first_frame_grid_plan(
    shots: Sequence[Mapping[str, Any]],
    *,
    cells_per_grid: int = 4,
    aspect_ratio: Any = None,
    cell_width: int | None = None,
    cell_height: int | None = None,
    columns: int | None = None,
    grid_layout: str | None = None,
    preserve_scene_batch: bool = False,
    reviewer: LightweightImagePromptReviewer | None = None,
    reviewer_config: Mapping[str, Any] | None = None,
    reviewer_runner: Callable[[Mapping[str, Any]], Any] | Any | None = None,
) -> dict[str, Any]:
    """为首帧任务生成网格批次，并保留原始镜头索引。"""

    if cells_per_grid <= 0:
        raise FirstFrameGridValidationError("cells_per_grid 必须是正整数")
    resolved_aspect_ratio, cell_width, cell_height = _resolve_grid_dimensions(
        aspect_ratio=aspect_ratio,
        cell_width=cell_width,
        cell_height=cell_height,
    )
    fixed_layout = normalize_grid_layout(grid_layout) if grid_layout is not None else None
    if fixed_layout is not None:
        requested_rows, requested_columns = _grid_shape(fixed_layout)
        batch_capacity = requested_rows * requested_columns
    else:
        fixed_rows = fixed_columns = None
        batch_capacity = cells_per_grid
    raw_shots = list(shots)
    skipped: list[int] = []
    active: list[tuple[int, Mapping[str, Any]]] = []

    for index, shot in enumerate(raw_shots):
        if not isinstance(shot, Mapping):
            raise FirstFrameGridValidationError(f"shots[{index}] 必须是对象")
        if is_digital_human_shot(shot) or str(shot.get("shot_class") or "") in {"explanation", "mixed_explanation"}:
            skipped.append(index)
        else:
            active.append((index, shot))

    # 3x3 是用户选择的最大宫格，而不是强制把尾数塞入九个格子。
    # 尾数 5--8 也保留为 3x3，占位格在裁切后丢弃；只有尾数 1--4
    # 才降级到 2x2，避免出现几乎空掉的大宫格。
    if fixed_layout == "3x3" and preserve_scene_batch:
        # 审查阶段以故事场景为原子单位：5--9 镜必须同图，空格只做可丢弃占位。
        chunks = [active[offset:offset + batch_capacity] for offset in range(0, len(active), batch_capacity)]
    elif fixed_layout == "3x3":
        chunks: list[list[tuple[int, Mapping[str, Any]]]] = []
        full_count = (len(active) // batch_capacity) * batch_capacity
        for offset in range(0, full_count, batch_capacity):
            chunks.append(active[offset:offset + batch_capacity])
        trailing = active[full_count:]
        if trailing:
            chunks.append(trailing)
    else:
        chunks = [active[offset:offset + batch_capacity] for offset in range(0, len(active), batch_capacity)]

    batches: list[dict[str, Any]] = []
    reviewer_instance = reviewer or LightweightImagePromptReviewer(
        config=ImagePromptReviewerConfig.from_mapping(reviewer_config),
        runner=reviewer_runner,
    )
    all_review_audits: list[dict[str, Any]] = []
    for batch_index, chunk in enumerate(chunks):
        if fixed_layout is not None:
            effective_layout = _effective_grid_layout(fixed_layout, len(chunk))
            effective_rows, effective_columns = _grid_shape(effective_layout)
            layout = GridLayout(
                effective_rows,
                effective_columns,
                cell_width,
                cell_height,
            )
        else:
            layout = _layout_for_count(
                len(chunk),
                cell_width=cell_width,
                cell_height=cell_height,
                columns=columns,
            )
        cells: list[dict[str, Any]] = []
        for cell_index, (shot_index, shot) in enumerate(chunk):
            left, top, right, bottom = layout.cell_box(cell_index)
            position_names = _GRID_POSITION_NAMES.get((layout.rows, layout.columns))
            position = position_names[cell_index] if position_names is not None else f"第{cell_index + 1}格"
            cell_prompt = _shot_prompt(shot)
            resolved_shot_size = _shot_visual_value(shot, "shot_size") or _camera_prompt_value(cell_prompt, "景别")
            resolved_viewpoint = (
                _shot_visual_value(shot, "viewpoint", "camera_angle")
                or _camera_prompt_value(cell_prompt, "视角")
                or _camera_prompt_value(cell_prompt, "机位")
            )
            resolved_composition = _shot_visual_value(shot, "composition") or _camera_prompt_value(cell_prompt, "构图")
            resolved_staging = _shot_visual_value(shot, "staging") or _prompt_field(cell_prompt, "调度")
            characters = shot.get("characters") or shot.get("character_names") or []
            key_props = shot.get("key_props") or shot.get("key_prop") or []
            if not isinstance(characters, (list, tuple)):
                characters = [characters] if _text(characters) else []
            if not isinstance(key_props, (list, tuple)):
                key_props = [key_props] if _text(key_props) else []
            cell: dict[str, Any] = {
                "cell_index": cell_index,
                "shot_index": shot_index,
                "shot_id": _shot_id(shot, shot_index),
                "prompt": cell_prompt,
                # 原始 Task A 编译结果只读保留；Reviewer 不覆盖它。
                "compiled_first_frame_prompt": cell_prompt,
                "position": position,
                "semantic_anchor": _text(shot.get("semantic_anchor")),
                "motion_seed": _text(shot.get("motion_seed")),
                "camera_motion": shot.get("camera_motion") if isinstance(shot.get("camera_motion"), Sequence) else [],
                "shot_size": resolved_shot_size,
                "viewpoint": resolved_viewpoint,
                "composition": resolved_composition,
                "staging": resolved_staging,
                "scene": _text(shot.get("scene")) or _prompt_field(cell_prompt, "场景") or _text(shot.get("semantic_anchor")),
                "characters": list(characters),
                "character_count": shot.get("character_count", len(characters)),
                "key_props": list(key_props),
                "frame_intent": _text(shot.get("frame_intent")) or _text(shot.get("semantic_anchor")),
                "dominant_action": _text(shot.get("dominant_action")) or _prompt_field(cell_prompt, "主体与动作"),
                "camera_angle": resolved_viewpoint,
                "lighting": _text(shot.get("lighting")) or _prompt_field(cell_prompt, "光线与氛围"),
                "character_relation": _text(shot.get("character_relation") or shot.get("relation")),
                "duration_s": shot.get("duration_s"),
                "reference_images": _reference_images(shot),
                "visual_guidance": shot.get("visual_guidance") if isinstance(shot.get("visual_guidance"), Mapping) else {},
                "row": cell_index // layout.columns,
                "column": cell_index % layout.columns,
                "box": [left, top, right, bottom],
            }
            projection = _cell_static_projection(cell)
            cell["static_frame_audit"] = {
                "source_cell_prompt": cell_prompt,
                "warnings": list(projection["warnings"]),
                "compiled_fields": dict(projection),
            }
            cell["compiled_cell_prompt"] = _cell_grid_instruction(cell)
            reviewer_input = build_reviewer_input(cell, compiled_prompt=cell["compiled_cell_prompt"])
            review_audit = reviewer_instance.review_cell(reviewer_input)
            cell["review_input"] = reviewer_input
            cell["review_audit"] = review_audit
            for audit_key in (
                "review_mode", "review_requested", "review_status", "review_risk_score",
                "review_model", "review_model_version", "review_issues_json",
                "reviewed_first_frame_prompt", "review_input_hash", "review_latency_ms",
                "review_error", "final_first_frame_prompt",
            ):
                cell[audit_key] = review_audit.get(audit_key)
            all_review_audits.append(review_audit)
            cells.append(cell)
        actual_grid_layout = (
            effective_layout if fixed_layout is not None else f"{layout.rows}x{layout.columns}"
        )
        placeholder_cell_indices = list(range(len(cells), layout.capacity))
        batch: dict[str, Any] = {
            "grid_id": f"first_frame_grid_{batch_index + 1:03d}",
            "mode": "multi_grid" if len(chunk) > 1 else "single",
            # 保留旧 mode 兼容旧消费者；固定宫格仍按 grid_layout 发起真实网格任务。
            "generation_mode": "fixed_grid" if fixed_layout is not None else "count_adaptive_grid",
            "grid_layout": actual_grid_layout,
            "requested_grid_layout": fixed_layout,
            "layout_policy": "max_capacity_auto_downgrade" if fixed_layout else "count_adaptive",
            "aspect_ratio": resolved_aspect_ratio,
            "layout": layout.to_dict(),
            "cells": cells,
            "active_shot_indices": [item[0] for item in chunk],
            "real_shot_count": len(chunk),
            "grid_capacity": layout.capacity,
            "unused_cell_count": len(placeholder_cell_indices),
            "placeholder_cell_indices": placeholder_cell_indices,
            "discarded_placeholder_cell_indices": placeholder_cell_indices,
            "placeholder_policy": "background_only_discard_after_crop",
        }
        batch["prompt"] = build_grid_prompt(batch)
        # 同一组镜头通常共享同一批风格参考图。若按宫格逐格直接展开，
        # 9 个镜头 × 8 张参考图会错误地向服务商提交 72 个 image_urls。
        # 保持首次出现顺序去重，既保留用户选图，也不重复消耗接口配额。
        batch["reference_images"] = list(dict.fromkeys(
            ref
            for cell in cells
            for ref in cell["reference_images"]
        ))
        batches.append(batch)

    placeholder_cell_count = sum(batch["unused_cell_count"] for batch in batches)
    planned_first_frame_shot_indices = [
        cell["shot_index"]
        for batch in batches
        for cell in batch["cells"]
    ]
    return {
        "mode": "multi_grid" if any(batch["mode"] == "multi_grid" for batch in batches) else "single",
        "aspect_ratio": resolved_aspect_ratio,
        "cell_width": cell_width,
        "cell_height": cell_height,
        "shot_count": len(raw_shots),
        "active_shot_indices": [item[0] for item in active],
        "planned_first_frame_shot_indices": planned_first_frame_shot_indices,
        "first_frame_coverage": {
            "ordinary_shot_count": len(active),
            "planned_first_frame_count": len(planned_first_frame_shot_indices),
            "complete": len(active) == len(planned_first_frame_shot_indices),
        },
        "skipped_digital_human_shot_indices": skipped,
        "cells_per_grid": batch_capacity,
        "grid_layout": fixed_layout,
        "requested_grid_layout": fixed_layout,
        "layout_policy": "max_capacity_auto_downgrade" if fixed_layout else "count_adaptive",
        "separation_px": 0,
        "placeholder_cell_count": placeholder_cell_count,
        "discarded_placeholder_cell_count": placeholder_cell_count,
        "batches": batches,
        "review_summary": summarize_review_audits(all_review_audits),
    }


def _pixel_rgb(pixel: Any) -> tuple[int, int, int]:
    if isinstance(pixel, int):
        value = max(0, min(255, int(pixel)))
        return value, value, value
    values = list(pixel) if isinstance(pixel, (tuple, list)) else [0, 0, 0]
    values = (values + [0, 0, 0])[:3]
    return tuple(max(0, min(255, int(value))) for value in values)  # type: ignore[return-value]


def _line_profile(image: Any, *, axis: str, coordinate: int) -> tuple[float, float, float]:
    """Sample one image column/row and return luminance mean, deviation, saturation."""

    if axis == "vertical":
        length = int(image.height)
    else:
        length = int(image.width)
    if length <= 0:
        return 0.0, 0.0, 0.0

    sample_count = min(512, length)
    positions = (
        [0]
        if sample_count == 1
        else [round(index * (length - 1) / (sample_count - 1)) for index in range(sample_count)]
    )
    luminance: list[float] = []
    saturation: list[float] = []
    for position in positions:
        pixel = image.getpixel((coordinate, position) if axis == "vertical" else (position, coordinate))
        red, green, blue = _pixel_rgb(pixel)
        luminance.append(0.2126 * red + 0.7152 * green + 0.0722 * blue)
        saturation.append(float(max(red, green, blue) - min(red, green, blue)))
    return (
        statistics.fmean(luminance),
        statistics.pstdev(luminance) if len(luminance) > 1 else 0.0,
        statistics.fmean(saturation),
    )


def _looks_like_separator(image: Any, *, axis: str, coordinate: int) -> bool:
    if axis == "vertical":
        limit = int(image.width)
    else:
        limit = int(image.height)
    if coordinate <= 0 or coordinate >= limit - 1:
        return False
    current_mean, current_deviation, current_saturation = _line_profile(
        image, axis=axis, coordinate=coordinate
    )
    if current_deviation > 18 or current_saturation > 28:
        return False
    previous_mean = _line_profile(image, axis=axis, coordinate=coordinate - 1)[0]
    next_mean = _line_profile(image, axis=axis, coordinate=coordinate + 1)[0]
    neighbor_contrast = max(abs(current_mean - previous_mean), abs(current_mean - next_mean))
    is_extreme = current_mean <= 45 or current_mean >= 230
    return neighbor_contrast >= 28 and (is_extreme or current_saturation <= 12)


def _detect_separator_band(image: Any, *, axis: str, boundary: int) -> int:
    """Detect a neutral white/black separator near one internal boundary."""

    limit = int(image.width if axis == "vertical" else image.height)
    radius = min(8, max(1, limit // 100))
    start = max(1, boundary - radius)
    end = min(limit - 2, boundary + radius)
    candidates = [
        coordinate
        for coordinate in range(start, end + 1)
        if _looks_like_separator(image, axis=axis, coordinate=coordinate)
    ]
    if not candidates:
        return 0

    runs: list[tuple[int, int]] = []
    run_start = previous = candidates[0]
    for coordinate in candidates[1:]:
        if coordinate == previous + 1:
            previous = coordinate
            continue
        runs.append((run_start, previous))
        run_start = previous = coordinate
    runs.append((run_start, previous))
    touching = [
        run
        for run in runs
        if run[0] <= boundary <= run[1] + 1
    ]
    if not touching:
        touching = sorted(
            runs,
            key=lambda run: min(abs(run[0] - boundary), abs(run[1] + 1 - boundary)),
        )[:1]
    start, end = max(touching, key=lambda run: run[1] - run[0])
    return end - start + 1


def _max_separator_band(image: Any, *, rows: int, columns: int) -> tuple[int, int]:
    vertical = max(
        (
            _detect_separator_band(
                image,
                axis="vertical",
                boundary=round(column * image.width / columns),
            )
            for column in range(1, columns)
        ),
        default=0,
    )
    horizontal = max(
        (
            _detect_separator_band(
                image,
                axis="horizontal",
                boundary=round(row * image.height / rows),
            )
            for row in range(1, rows)
        ),
        default=0,
    )
    return vertical, horizontal


def crop_equal_grid_image(
    source: str | Path,
    output_dir: str | Path,
    batch: Mapping[str, Any],
    *,
    crop_inset_px: int = 3,
    separator_trim_px: int = 0,
    auto_trim_separators: bool = True,
) -> list[dict[str, Any]]:
    """按实际返回尺寸裁切；余数像素居中舍弃，随后统一内缩并保持等尺寸输出。"""

    for name, value in (("crop_inset_px", crop_inset_px), ("separator_trim_px", separator_trim_px)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise FirstFrameGridCropError(f"{name} must be a non-negative integer")

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise FirstFrameGridCropError("裁切多宫格需要安装 Pillow") from exc

    source_path = Path(source)
    output_path = Path(output_dir)
    if not source_path.is_file():
        raise FirstFrameGridCropError(f"多宫格源图不存在：{source_path}")

    layout_data = batch.get("layout")
    if not isinstance(layout_data, Mapping):
        raise FirstFrameGridCropError("batch.layout 缺失")
    rows = int(layout_data.get("rows", 0))
    columns = int(layout_data.get("columns", 0))
    if rows <= 0 or columns <= 0:
        raise FirstFrameGridCropError("batch.layout 的 rows/columns 无效")

    cells = list(batch.get("cells", []))
    if len(cells) > rows * columns:
        raise FirstFrameGridCropError("cells 数量超过网格容量")

    output_path.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    with Image.open(source_path) as image:
        if image.width < columns or image.height < rows:
            raise FirstFrameGridCropError(
                f"源图尺寸 {image.width}x{image.height} 无法被 {rows}x{columns} 等分"
            )
        usable_width = image.width - (image.width % columns)
        usable_height = image.height - (image.height % rows)
        grid_offset_x = (image.width - usable_width) // 2
        grid_offset_y = (image.height - usable_height) // 2
        cell_width = usable_width // columns
        cell_height = usable_height // rows
        source_image = image.convert("RGB")
        detected_vertical = detected_horizontal = 0
        if auto_trim_separators:
            detected_vertical, detected_horizontal = _max_separator_band(
                source_image,
                rows=rows,
                columns=columns,
            )
        auto_separator_inset = max(
            math.ceil(detected_vertical / 2),
            math.ceil(detected_horizontal / 2),
        )
        total_inset = crop_inset_px + separator_trim_px + auto_separator_inset
        output_cell_width = cell_width - 2 * total_inset
        output_cell_height = cell_height - 2 * total_inset
        if output_cell_width <= 0 or output_cell_height <= 0:
            raise FirstFrameGridCropError(
                f"内缩 {total_inset}px 后单格尺寸无效：{cell_width}x{cell_height}"
            )
        for cell in cells:
            cell_index = int(cell["cell_index"])
            row, column = divmod(cell_index, columns)
            box = (
                grid_offset_x + column * cell_width + total_inset,
                grid_offset_y + row * cell_height + total_inset,
                grid_offset_x + (column + 1) * cell_width - total_inset,
                grid_offset_y + (row + 1) * cell_height - total_inset,
            )
            shot_index = int(cell["shot_index"])
            target = output_path / f"first_frame_shot_{shot_index + 1:03d}.png"
            source_image.crop(box).save(target, format="PNG")
            results.append({
                "shot_index": shot_index,
                "shot_id": _text(cell.get("shot_id")),
                "status": "ready",
                "path": str(target),
                "cell_index": cell_index,
                "width": output_cell_width,
                "height": output_cell_height,
                "box": list(box),
                "crop_inset_px": crop_inset_px,
                "separator_trim_px": separator_trim_px,
                "detected_separator_band_px": {
                    "vertical": detected_vertical,
                    "horizontal": detected_horizontal,
                },
                "total_inset_px": total_inset,
                "grid_offset_px": {"x": grid_offset_x, "y": grid_offset_y},
            })
    return results


def _api_grid_size(layout: Mapping[str, Any]) -> str:
    """将网格方向映射到已支持的图像画布，不向 API 发送任意尺寸字符串。"""
    rows = int(layout["rows"])
    columns = int(layout["columns"])
    cell_width = int(layout["cell_width"])
    cell_height = int(layout["cell_height"])
    ratio = _format_aspect_ratio(columns * cell_width, rows * cell_height)
    supported = {
        "1:1": "1024x1024",
        "16:9": "1280x720",
        "9:16": "720x1280",
        "3:2": "1536x1024",
        "2:3": "1024x1536",
    }
    if ratio in supported:
        return supported[ratio]
    if columns * cell_width >= rows * cell_height:
        return "1536x1024"
    return "1024x1536"


def run_first_frame_grid_batch(
    shots: Sequence[Mapping[str, Any]],
    *,
    create_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    query_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    materialize_runner: Callable[[str, Mapping[str, Any], str | Path], str | Path] | None = None,
    output_dir: str | Path = ".",
    image_model: str = "image2",
    cells_per_grid: int = 4,
    aspect_ratio: Any = None,
    cell_width: int | None = None,
    cell_height: int | None = None,
    columns: int | None = None,
    grid_layout: str = "2x2",
    preserve_scene_batch: bool = False,
    reviewer: LightweightImagePromptReviewer | None = None,
    reviewer_config: Mapping[str, Any] | None = None,
    reviewer_runner: Callable[[Mapping[str, Any]], Any] | Any | None = None,
    checkpoint_path: str | Path | None = None,
    resume: bool = False,
    crop_inset_px: int = 3,
    separator_trim_px: int = 0,
    auto_trim_separators: bool = True,
    max_workers: int = 1,
) -> dict[str, Any]:
    """执行首帧多宫格批处理；所有外部调用都必须显式注入。"""

    if create_runner is None or query_runner is None:
        raise FirstFrameGridBatchError("未注入首帧 create/query transport，禁止伪造多宫格结果")
    if materialize_runner is None:
        def materialize_runner(
            image_url: str,
            _batch: Mapping[str, Any],
            _output_dir: str | Path,
        ) -> str | Path:
            path = Path(image_url)
            if not path.is_file():
                raise FirstFrameGridBatchError(
                    "多宫格结果不是本地文件；真实 URL 必须注入 materialize_runner 下载后再裁切"
                )
            return path

    plan = build_first_frame_grid_plan(
        shots,
        cells_per_grid=cells_per_grid,
        aspect_ratio=aspect_ratio,
        cell_width=cell_width,
        cell_height=cell_height,
        columns=columns,
        grid_layout=grid_layout,
        preserve_scene_batch=preserve_scene_batch,
        reviewer=reviewer,
        reviewer_config=reviewer_config,
        reviewer_runner=reviewer_runner,
    )
    checkpoint_records = _load_grid_checkpoint_handles(checkpoint_path) if resume and checkpoint_path is not None else {}
    aligned_outputs = [""] * len(shots)
    cropped_assets: list[dict[str, Any]] = []
    task_records: list[dict[str, Any]] = []

    # Fresh runs use a strict two-phase protocol: all POST creates finish (and
    # are checkpointed) before any GET query starts.  This prevents a slow
    # first query from serialising creation of later paid grids.
    precreated: dict[str, Mapping[str, Any]] = {}

    def process_batch(batch: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        grid_config = dict(batch["layout"])
        grid_config.update(
            {
                "aspect_ratio": plan["aspect_ratio"],
                "cell_aspect_ratio": _format_aspect_ratio(
                    grid_config["cell_width"], grid_config["cell_height"]
                ),
                "canvas_aspect_ratio": _format_aspect_ratio(
                    grid_config["total_width"],
                    grid_config["total_height"],
                ),
                "crop_inset_px": crop_inset_px,
                "separator_trim_px": separator_trim_px,
                "auto_trim_separators": auto_trim_separators,
            }
        )
        request = {
            "prompt": batch["prompt"],
            "ref_image": batch["reference_images"],
            "image_urls": batch["reference_images"],
            "image_model": image_model,
            "n": 1,
            "size": _api_grid_size(batch["layout"]),
            "grid_config": grid_config,
            "grid_layout": batch["grid_layout"],
            "grid_id": batch["grid_id"],
        }
        resume_record = dict(checkpoint_records.get(str(batch["grid_id"])) or {}) if resume else {}
        task_record: dict[str, Any] | None = None
        task_id = str(resume_record.get("task_id") or "").strip()
        image_url = str(resume_record.get("image_url") or "").strip()
        local_grid_path = str(resume_record.get("local_grid_path") or "").strip()
        # 查询必须复用创建任务时的接入点和鉴权序号。只传 task_id 会在
        # 多接入点配置下把 GET 发到错误账号，表现为“后台已完成但页面查不到”。
        query_context = {
            key: resume_record.get(key)
            for key in ("channel_id", "key_index", "endpoint", "model_id")
            if resume_record.get(key) not in (None, "")
        }
        local_grid: Path | None = None

        if resume and local_grid_path:
            local_candidate = Path(local_grid_path)
            if local_candidate.is_file():
                local_grid = local_candidate
                task_record = {
                    "grid_id": batch["grid_id"],
                    "task_id": task_id,
                    "status": str(resume_record.get("status") or "completed"),
                    "image_url": image_url,
                    "local_grid_path": str(local_candidate.resolve()),
                    "resume_mode": "local_grid_path",
                }
            else:
                local_grid_path = ""
        if local_grid is None and resume and (image_url or task_id):
            if not image_url and task_id:
                queried = query_runner({"task_id": task_id, **query_context})
                if not isinstance(queried, Mapping):
                    raise FirstFrameGridBatchError(f"{batch['grid_id']} query 返回必须是对象")
                image_url = str(queried.get("image_url") or "").strip()
                task_record = {
                    "grid_id": batch["grid_id"],
                    "task_id": task_id,
                    "status": queried.get("status", "unknown"),
                    "resume_mode": "checkpoint_query",
                }
            else:
                task_record = {
                    "grid_id": batch["grid_id"],
                    "task_id": task_id,
                    "status": str(resume_record.get("status") or "completed"),
                    "resume_mode": "checkpoint_image_url",
                }
            if not image_url:
                raise FirstFrameGridBatchError(f"{batch['grid_id']} 的断点没有可恢复的 image_url")
            local_grid = Path(materialize_runner(image_url, batch, output_dir))
            if checkpoint_path is not None:
                persist_grid_checkpoint_handle(
                    checkpoint_path,
                    batch["grid_id"],
                    {
                        "grid_id": batch["grid_id"],
                        "status": "completed",
                        "task_id": task_id,
                        "image_url": image_url,
                        "local_grid_path": str(local_grid.resolve()),
                        "task_record": dict(task_record),
                    },
                )
        if local_grid is None and resume:
            raise FirstFrameGridBatchError(
                f"{batch['grid_id']} 恢复断点缺少 task_id/image_url/local_grid_path，"
                "已阻止自动创建新任务；请补齐该宫格句柄后再恢复。"
            )
        if local_grid is None:
            created = precreated.get(str(batch["grid_id"])) or create_runner(request)
            if not isinstance(created, Mapping):
                raise FirstFrameGridBatchError(f"{batch['grid_id']} create 返回必须是对象")
            task_id = str(created.get("task_id") or "").strip()
            image_url = str(created.get("image_url") or "").strip()
            query_context = {
                key: created.get(key)
                for key in ("channel_id", "key_index", "endpoint", "model_id")
                if created.get(key) not in (None, "")
            }
            task_record = None
            if task_id:
                queried = query_runner({"task_id": task_id, **query_context})
                if not isinstance(queried, Mapping):
                    raise FirstFrameGridBatchError(f"{batch['grid_id']} query 返回必须是对象")
                image_url = str(queried.get("image_url") or "").strip()
                task_record = {
                    "grid_id": batch["grid_id"],
                    "task_id": task_id,
                    "status": queried.get("status", "unknown"),
                    "resume_mode": "fresh_query",
                }
            if not image_url:
                raise FirstFrameGridBatchError(f"{batch['grid_id']} 未返回可裁切的 image_url")
            local_grid = Path(materialize_runner(image_url, batch, output_dir))
            if checkpoint_path is not None:
                persist_grid_checkpoint_handle(
                    checkpoint_path,
                    batch["grid_id"],
                    {
                        "grid_id": batch["grid_id"],
                        "status": "completed",
                        "task_id": task_id,
                        "image_url": image_url,
                        "local_grid_path": str(Path(local_grid).resolve()),
                        "task_record": dict(task_record) if isinstance(task_record, Mapping) else {"grid_id": batch["grid_id"], "task_id": task_id, "status": "completed"},
                    },
                )
        assert local_grid is not None
        _validate_grid_image_layout(local_grid, batch)
        cropped = crop_equal_grid_image(
            local_grid,
            output_dir,
            batch,
            crop_inset_px=crop_inset_px,
            separator_trim_px=separator_trim_px,
            auto_trim_separators=auto_trim_separators,
        )
        if checkpoint_path is not None:
            persist_grid_checkpoint_handle(
                checkpoint_path,
                batch["grid_id"],
                {
                    "grid_id": batch["grid_id"],
                    "status": "completed",
                    "task_id": task_id,
                    "image_url": image_url,
                    "local_grid_path": str(Path(local_grid).resolve()),
                    "task_record": dict(task_record) if isinstance(task_record, Mapping) else {"grid_id": batch["grid_id"], "task_id": task_id, "status": "completed"},
                },
            )
        return cropped, task_record

    # 新任务先批量创建所有宫格；resume 绝不创建，只查询/复用已有句柄。
    if not resume and plan["batches"]:
        create_workers = max(1, min(int(max_workers or 1), len(plan["batches"])))
        def submit_one(batch: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
            grid_id = str(batch["grid_id"])
            grid_config = dict(batch["layout"])
            grid_config.update({"aspect_ratio": plan["aspect_ratio"], "cell_aspect_ratio": _format_aspect_ratio(grid_config["cell_width"], grid_config["cell_height"]), "canvas_aspect_ratio": _format_aspect_ratio(grid_config["total_width"], grid_config["total_height"]), "crop_inset_px": crop_inset_px, "separator_trim_px": separator_trim_px, "auto_trim_separators": auto_trim_separators})
            request = {"prompt": batch["prompt"], "ref_image": batch["reference_images"], "image_urls": batch["reference_images"], "image_model": image_model, "n": 1, "size": _api_grid_size(batch["layout"]), "grid_config": grid_config, "grid_layout": batch["grid_layout"], "grid_id": grid_id}
            return grid_id, create_runner(request)
        with ThreadPoolExecutor(max_workers=create_workers) as executor:
            futures = [executor.submit(submit_one, batch) for batch in plan["batches"]]
            for future in as_completed(futures):
                grid_id, created = future.result()
                if not isinstance(created, Mapping):
                    raise FirstFrameGridBatchError(f"{grid_id} create 返回必须是对象")
                precreated[grid_id] = created

    # 默认保持单线程，保证既有调用的可重复性；真实素材层可显式开启并行。
    worker_count = max(1, min(int(max_workers or 1), len(plan["batches"]) or 1))
    if worker_count == 1:
        completed_batches = [process_batch(batch) for batch in plan["batches"]]
    else:
        completed_batches = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(process_batch, batch) for batch in plan["batches"]]
            for future in as_completed(futures):
                completed_batches.append(future.result())

    for cropped, task_record in completed_batches:
        if task_record is not None:
            task_records.append(task_record)
        for asset in cropped:
            shot_index = int(asset["shot_index"])
            aligned_outputs[shot_index] = str(asset["path"])
            cropped_assets.append(asset)

    return {
        "image_url_list": aligned_outputs,
        "grid_batches": plan["batches"],
        "review_summary": dict(plan.get("review_summary") or {}),
        "cropped_assets": cropped_assets,
        "skipped_digital_human_shot_indices": plan["skipped_digital_human_shot_indices"],
        "task_records": task_records,
        "grid_config": {
            "aspect_ratio": plan["aspect_ratio"],
            "cell_aspect_ratio": _format_aspect_ratio(
                plan["cell_width"], plan["cell_height"]
            ),
            "canvas_aspect_ratio": plan["aspect_ratio"],
            "crop_inset_px": crop_inset_px,
            "separator_trim_px": separator_trim_px,
            "auto_trim_separators": auto_trim_separators,
        },
        "status": "completed",
    }


def _validate_grid_image_layout(source: str | Path, batch: Mapping[str, Any]) -> None:
    """拒绝供应商擅自改变宫格行列数的图片，避免错误裁切后继续下游。"""

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise FirstFrameGridBatchError("校验宫格尺寸需要 Pillow") from exc
    layout = batch["layout"]
    if isinstance(layout, Mapping):
        total_width = int(layout["total_width"])
        total_height = int(layout["total_height"])
    else:
        total_width = int(layout.total_width)
        total_height = int(layout.total_height)
    expected_ratio = total_width / total_height
    with Image.open(source) as image:
        actual_ratio = image.width / image.height
    if abs(actual_ratio - expected_ratio) / expected_ratio > 0.08:
        raise FirstFrameGridBatchError(
            f"{batch['grid_id']} 原图画幅与请求的 {batch['grid_layout']} 宫格不符，拒绝裁切"
        )


__all__ = [
    "FirstFrameGridCropError",
    "FirstFrameGridValidationError",
    "FirstFrameGridCropError",
    "FirstFrameGridValidationError",
    "GridLayout",
    "build_first_frame_grid_plan",
    "build_grid_prompt",
    "crop_equal_grid_image",
    "is_digital_human_shot",
    "FirstFrameGridBatchError",
    "run_first_frame_grid_batch",
]
