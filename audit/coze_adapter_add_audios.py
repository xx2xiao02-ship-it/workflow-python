"""解说* 节点的脱敏运行适配器。"""

from __future__ import annotations

import copy
import json
from typing import Any

from workflow_1256.add_audios import run_add_audios


def synthetic_executor(draft_url: str, _audio_infos: str, normalized: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "audio_ids": [f"synthetic-audio-{index + 1}" for index in range(len(normalized))],
        "draft_url": f"{draft_url}-updated",
        "track_id": "synthetic-track-1",
    }


def run_migrated(params: Any, *, executor=synthetic_executor) -> dict[str, Any]:
    return run_add_audios(copy.deepcopy(params), executor=executor)


def load_sample() -> dict[str, Any]:
    from pathlib import Path

    path = Path(__file__).parents[1] / "samples" / "synthetic" / "add_audios_input.json"
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["load_sample", "run_migrated", "synthetic_executor"]
