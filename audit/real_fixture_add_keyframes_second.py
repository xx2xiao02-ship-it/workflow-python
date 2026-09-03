"""节点 1962357 的 17 段真实 Coze 结构脱敏输入构造器。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workflow_1256.keyframes_infos_second import run_keyframes_infos


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "samples" / "real" / "run-7668196429877870619-174651-aigc-animation-real-values-redacted.json"


def build_real_input() -> dict[str, str]:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    infos = json.loads(source["input"]["video_infos"])
    segment_infos = [
        {"id": f"redacted-segment-{index:02d}", "start": item["start"], "end": item["end"]}
        for index, item in enumerate(infos)
    ]
    keyframes = run_keyframes_infos({
        "ctype": "UNIFORM_SCALE",
        "offsets": "0|100",
        "values": "1|1.15",
        "segment_infos": segment_infos,
    })["keyframes_infos"]
    return {
        "draft_url": "http://synthetic.local/openapi/capcut-mate/v1/get_draft?draft_id=redacted-1962357",
        "keyframes": keyframes,
    }


def run_real_structural_audit() -> dict[str, Any]:
    params = build_real_input()
    calls: list[tuple[str, str]] = []

    def executor(draft_url: str, keyframes: str) -> dict[str, str]:
        calls.append((draft_url, keyframes))
        return {"draft_url": draft_url}

    from workflow_1256.add_keyframes_second import run_add_keyframes
    output = run_add_keyframes(params, executor=executor)
    return {
        "node_id": "1962357",
        "input_keyframe_count": len(json.loads(params["keyframes"])),
        "executor_call_count": len(calls),
        "output_field_order": list(output),
        "output": output,
        "remote_write_executed": False,
        "dynamic_values_redacted": True,
    }


__all__ = ["build_real_input", "run_real_structural_audit"]
