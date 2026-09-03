"""V3 说明镜头的 Python → Remotion 本地执行适配层。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .explanation_style_lock import (
    ExplanationStyleLockError,
    build_style_lock,
    duration_to_frames,
    normalize_style_override,
)


REMOTION_ROOT = Path(__file__).resolve().parents[2] / "remotion-explanation"
TEMPLATE_IDS = {
    "causal_chain",
    "contrast_split",
    "process_steps",
    "hierarchy_layers",
    "timeline_path",
    "data_trend",
    "concept_map",
}


class ExplanationRenderError(RuntimeError):
    """Remotion 渲染失败。"""


class ExplanationRenderBlocked(ExplanationRenderError):
    """输入或外部运行环境尚未就绪。"""


def _node_executable() -> str:
    configured = str(os.environ.get("REMOTION_NODE_BIN") or "").strip()
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("node")
    if found:
        return found
    raise ExplanationRenderBlocked("未找到 Node.js；请配置 REMOTION_NODE_BIN")


def _shot_map(records: object) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("shot_id") or ""): item
        for item in (records if isinstance(records, list) else [])
        if isinstance(item, Mapping) and str(item.get("shot_id") or "")
    }


def _digital_human_paths(asset_result: Mapping[str, Any]) -> dict[str, Path]:
    assets = asset_result.get("digital_human_assets")
    records = assets.get("shot_results") if isinstance(assets, Mapping) else []
    result: dict[str, Path] = {}
    for item in records if isinstance(records, list) else []:
        if not isinstance(item, Mapping) or str(item.get("status") or "").lower() != "succeeded":
            continue
        path = Path(str(item.get("video_path") or "")).expanduser()
        if path.is_file() and str(item.get("shot_id") or ""):
            result[str(item["shot_id"])] = path.resolve()
    return result


def _build_requests(
    shots: Sequence[Mapping[str, Any]],
    style_lock: Mapping[str, Any],
    asset_result: Mapping[str, Any],
    *,
    width: int,
    height: int,
    fps: int,
    retry_shot_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    digital_paths = _digital_human_paths(asset_result)
    requests: list[dict[str, Any]] = []
    for shot in shots:
        shot_class = str(shot.get("shot_class") or "")
        if shot_class not in {"explanation", "mixed_explanation"}:
            continue
        shot_id = str(shot.get("shot_id") or "")
        if retry_shot_ids and shot_id not in retry_shot_ids:
            continue
        plan = shot.get("render_plan") if isinstance(shot.get("render_plan"), Mapping) else {}
        template_id = str(plan.get("template_id") or "")
        if template_id not in TEMPLATE_IDS:
            raise ExplanationRenderBlocked(f"{shot_id}包含未知说明模板：{template_id}")
        start_us = int(shot.get("start_us") or 0)
        end_us = int(shot.get("end_us") or 0)
        if end_us <= start_us:
            raise ExplanationRenderBlocked(f"{shot_id}时间线无效")
        item: dict[str, Any] = {
            "shot_id": shot_id,
            "shot_class": shot_class,
            "template_id": template_id,
            "template_version": str(plan.get("template_version") or "v3.0"),
            "start_us": start_us,
            "end_us": end_us,
            "duration_us": end_us - start_us,
            "duration_frames": duration_to_frames(end_us - start_us, fps),
            "width": int(width),
            "height": int(height),
            "fps": int(fps),
            "narration": str(shot.get("narration") or ""),
            "semantic_anchor": str(shot.get("semantic_anchor") or ""),
            "relation_type": str(plan.get("relation_type") or ""),
            "relation_objects": [str(value) for value in (plan.get("relation_objects") or []) if str(value).strip()],
            "style_lock_id": str(style_lock.get("style_lock_id") or ""),
            "style_lock": dict(style_lock.get("style") or {}),
            "overlay_tracks": list(plan.get("overlay_tracks") or []),
        }
        if shot_class == "mixed_explanation":
            digital_path = digital_paths.get(shot_id)
            if digital_path is None:
                raise ExplanationRenderBlocked(f"{shot_id}缺少同一任务已完成的数字人本地视频")
            item["digital_human_video_path"] = str(digital_path)
        requests.append(item)
    return requests


def render_explanation_shots(
    shots: Sequence[Mapping[str, Any]],
    asset_result: Mapping[str, Any],
    output_dir: str | Path,
    *,
    retry_shot_ids: set[str] | None = None,
    packaging_override: Mapping[str, Any] | None = None,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """一次构建并渲染当前任务的说明镜头，返回可持久化结果。"""

    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    explanation_shots = [
        shot for shot in shots if str(shot.get("shot_class") or "") in {"explanation", "mixed_explanation"}
    ]
    if not explanation_shots:
        return {"status": "skipped", "shot_results": [], "succeeded_count": 0, "failed_count": 0}
    first_frames = asset_result.get("first_frame_assets")
    if not isinstance(first_frames, Mapping):
        raise ExplanationRenderBlocked("说明镜头等待首帧，但当前任务没有 first_frame_assets")
    normalized_override = normalize_style_override(packaging_override)
    style_lock = build_style_lock(
        shots,
        first_frames,
        target_dir / "explanation_style_lock.json",
        packaging_override=normalized_override,
    )
    requests = _build_requests(
        shots,
        style_lock,
        asset_result,
        width=width,
        height=height,
        fps=fps,
        retry_shot_ids=retry_shot_ids,
    )
    if not requests:
        raise ExplanationRenderBlocked("没有可执行的说明镜头请求")
    request_path = target_dir / "remotion_render_request.json"
    request_path.write_text(
        json.dumps({"schema_version": "explanation-render-request-v1", "output_dir": str(target_dir), "render_concurrency": 3, "renders": requests}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    node = _node_executable()
    started = time.time()
    try:
        completed = subprocess.run(
            [node, str(REMOTION_ROOT / "render.mjs"), str(request_path)],
            cwd=str(REMOTION_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExplanationRenderError(
            f"Remotion渲染超时（{timeout_seconds}秒）；成功镜头不会被覆盖，可按镜头重试"
        ) from exc
    except OSError as exc:
        raise ExplanationRenderError(f"Remotion进程启动失败：{exc}") from exc
    stdout_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    renderer_result: dict[str, Any] = {}
    if stdout_lines:
        try:
            renderer_result = json.loads(stdout_lines[-1])
        except json.JSONDecodeError:
            renderer_result = {"status": "failed", "error": "Remotion未返回有效JSON"}
    if completed.returncode != 0 or renderer_result.get("status") != "succeeded":
        raise ExplanationRenderError(
            f"Remotion渲染失败（退出码 {completed.returncode}）："
            f"{renderer_result.get('error') or completed.stderr[-1200:] or '无错误详情'}"
        )
    results = []
    for item in renderer_result.get("results") or []:
        if not isinstance(item, Mapping):
            raise ExplanationRenderError("Remotion结果包含无效镜头记录")
        output_path = Path(str(item.get("output_path") or "")).expanduser()
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise ExplanationRenderError(f"Remotion输出文件不存在或为空：{output_path}")
        results.append(dict(item))
    expected_ids = {str(item.get("shot_id") or "") for item in requests}
    actual_ids = {str(item.get("shot_id") or "") for item in results}
    if actual_ids != expected_ids or len(results) != len(requests):
        raise ExplanationRenderError(
            f"Remotion返回镜头集合不完整：请求 {sorted(expected_ids)}，结果 {sorted(actual_ids)}"
        )
    result = {
        "status": "succeeded",
        "renderer": "remotion",
        "renderer_version": "4.0.518",
        "style_lock_id": style_lock["style_lock_id"],
        "style_lock_path": str(target_dir / "explanation_style_lock.json"),
        "shot_results": results,
        "succeeded_count": len(results),
        "failed_count": 0,
        "duration_seconds": round(time.time() - started, 3),
        "request_path": str(request_path),
        "style_override": normalized_override,
    }
    (target_dir / "explanation_render.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


__all__ = [
    "ExplanationRenderBlocked",
    "ExplanationRenderError",
    "render_explanation_shots",
]
