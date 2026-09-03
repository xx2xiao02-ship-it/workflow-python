"""背景音乐*（110576）的本地传输适配器。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from workflow_1256.background_music import run_background_music


def synthetic_executor(
    draft_url: str,
    _audio_infos: str,
    normalized: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "audio_ids": [f"synthetic-bgm-audio-{index + 1}" for index in range(len(normalized))],
        "draft_url": f"{draft_url}-updated",
        "track_id": "synthetic-bgm-track-1",
    }


def run_migrated(params: Any, *, executor=synthetic_executor) -> dict[str, Any]:
    return run_background_music(copy.deepcopy(params), executor=executor)


def load_sample() -> dict[str, Any]:
    path = Path(__file__).parents[1] / "samples" / "synthetic" / "background_music_input.json"
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["load_sample", "run_migrated", "synthetic_executor"]
