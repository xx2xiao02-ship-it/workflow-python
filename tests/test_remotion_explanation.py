from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from workflow_1256.explanation_style_lock import (
    build_style_lock,
    duration_to_frames,
    normalize_style_override,
    resolve_first_aigc_frame,
)
from workflow_1256.remotion_explanation_transport import TEMPLATE_IDS, _build_requests
from workflow_1256 import remotion_explanation_transport as transport


def _shots():
    return [
        {"shot_id": "aigc-1", "media_type": "aigc_video", "shot_class": "aigc"},
        {
            "shot_id": "explain-1",
            "shot_class": "explanation",
            "start_us": 0,
            "end_us": 1_500_000,
            "render_plan": {"template_id": "contrast_split", "relation_objects": ["甲", "乙"]},
        },
        {
            "shot_id": "mixed-1",
            "shot_class": "mixed_explanation",
            "start_us": 1_500_000,
            "end_us": 2_533_333,
            "render_plan": {"template_id": "causal_chain", "relation_objects": ["因", "果"]},
        },
    ]


def test_duration_and_first_frame_are_deterministic(tmp_path: Path):
    image = tmp_path / "aigc-1.png"
    Image.new("RGB", (20, 20), (240, 220, 200)).save(image)
    first_id, first_path = resolve_first_aigc_frame(
        _shots(),
        {
            "image_paths": [str(image)],
            "grid_batches": [{"cells": [{"cell_index": 0, "shot_id": "aigc-1"}]}],
        },
    )
    assert first_id == "aigc-1"
    assert first_path == image.resolve()
    assert duration_to_frames(1_000_000, 30) == 30
    assert duration_to_frames(1_033_333, 30) == 31


def test_all_seven_template_mappings_are_closed_world():
    assert TEMPLATE_IDS == {
        "causal_chain", "contrast_split", "process_steps", "hierarchy_layers",
        "timeline_path", "data_trend", "concept_map",
    }


def test_style_lock_and_mixed_request_keep_same_shot_mapping(tmp_path: Path):
    image = tmp_path / "aigc-1.png"
    digital = tmp_path / "mixed-1.mp4"
    Image.new("RGB", (20, 20), (20, 30, 80)).save(image)
    digital.write_bytes(b"video")
    style = build_style_lock(
        _shots(),
        {"image_paths": [str(image)], "grid_batches": []},
        tmp_path / "style.json",
    )
    assert style["source_shot_id"] == "aigc-1"
    assert json.loads((tmp_path / "style.json").read_text(encoding="utf-8"))["style_lock_id"] == style["style_lock_id"]
    requests = _build_requests(
        _shots(),
        style,
        {"digital_human_assets": {"shot_results": [{"shot_id": "mixed-1", "status": "succeeded", "video_path": str(digital)}]}},
        width=1920,
        height=1080,
        fps=30,
    )
    assert [item["shot_id"] for item in requests] == ["explain-1", "mixed-1"]
    assert requests[1]["duration_frames"] == 31
    assert requests[1]["digital_human_video_path"] == str(digital.resolve())


def test_style_lock_uses_cell_shot_id_when_images_and_aigc_are_interleaved(tmp_path: Path):
    image_shot = tmp_path / "image.png"
    aigc_first = tmp_path / "aigc-first.png"
    Image.new("RGB", (20, 20), (240, 240, 240)).save(image_shot)
    Image.new("RGB", (20, 20), (10, 20, 30)).save(aigc_first)
    shots = [
        {"shot_id": "image-1", "media_type": "static_image", "shot_class": "image"},
        {"shot_id": "aigc-1", "media_type": "aigc_video", "shot_class": "aigc"},
        {"shot_id": "explain-1", "media_type": "explanation", "shot_class": "explanation"},
    ]
    first_id, first_path = resolve_first_aigc_frame(
        shots,
        {
            "image_paths": [str(image_shot), str(aigc_first)],
            "grid_batches": [{"cells": [
                {"cell_index": 0, "shot_id": "image-1"},
                {"cell_index": 1, "shot_id": "aigc-1"},
            ]}],
        },
    )
    assert first_id == "aigc-1"
    assert first_path == aigc_first.resolve()


def _render_input(tmp_path: Path) -> tuple[list[dict], dict]:
    image = tmp_path / "aigc-1.png"
    Image.new("RGB", (20, 20), (240, 220, 200)).save(image)
    shots = _shots()[:2]
    return shots, {"first_frame_assets": {"image_paths": [str(image)]}, "first_frame_prompt_trace": [{"shot_id": "aigc-1"}]}


def test_render_timeout_is_reported_as_retryable_error(tmp_path: Path, monkeypatch):
    shots, result = _render_input(tmp_path)
    monkeypatch.setattr(transport, "_node_executable", lambda: "node")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("node", 1)

    monkeypatch.setattr(transport.subprocess, "run", timeout)
    with pytest.raises(transport.ExplanationRenderError, match="超时"):
        transport.render_explanation_shots(shots, result, tmp_path / "out", timeout_seconds=1)


def test_render_nonzero_exit_is_reported(tmp_path: Path, monkeypatch):
    shots, result = _render_input(tmp_path)
    monkeypatch.setattr(transport, "_node_executable", lambda: "node")
    monkeypatch.setattr(
        transport.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["node"], 1, stdout=json.dumps({"status": "failed", "error": "renderer crashed"}), stderr="renderer crashed"
        ),
    )
    with pytest.raises(transport.ExplanationRenderError, match="renderer crashed"):
        transport.render_explanation_shots(shots, result, tmp_path / "out")


def test_render_invalid_result_is_rejected(tmp_path: Path, monkeypatch):
    shots, result = _render_input(tmp_path)
    monkeypatch.setattr(transport, "_node_executable", lambda: "node")
    monkeypatch.setattr(
        transport.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["node"], 0,
            stdout=json.dumps({"status": "succeeded", "results": [{"shot_id": "explain-1", "status": "succeeded", "output_path": str(tmp_path / "missing.mp4")}]})
        ),
    )
    with pytest.raises(transport.ExplanationRenderError, match="不存在或为空"):
        transport.render_explanation_shots(shots, result, tmp_path / "out")


def test_packaging_override_wins_over_style_defaults(tmp_path: Path):
    image = tmp_path / "aigc-1.png"
    Image.new("RGB", (20, 20), (240, 220, 200)).save(image)
    style = build_style_lock(
        _shots(), {"image_paths": [str(image)], "grid_batches": []}, tmp_path / "style.json",
        packaging_override={"title_font_size": 72, "pip_radius": 12},
    )
    assert style["style"]["title_font_size"] == 72
    assert style["style"]["pip_radius"] == 12
    assert style["style_source"].endswith("+packaging_override")


def test_remotion_style_override_is_normalized_and_rejects_unknown_values():
    settings = normalize_style_override({
        "background_color": "#f7f4ee",
        "accent_color": "#d97745",
        "title_font_size": "72",
        "pip_width": 30,
        "pip_object_fit": "contain",
    })
    assert settings == {
        "background_color": "#F7F4EE",
        "accent_color": "#D97745",
        "title_font_size": 72,
        "pip_width": "30%",
        "pip_object_fit": "contain",
    }
    with pytest.raises(ValueError, match="不支持"):
        normalize_style_override({"template_id": "contrast_split"})
    with pytest.raises(ValueError, match="标题字号"):
        normalize_style_override({"title_font_size": 120})
