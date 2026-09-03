"""真实 Coze 添加关键帧节点的结构审计；不执行草稿写入。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workflow_1256.add_keyframes import run_add_keyframes


ROOT = Path(__file__).parents[1]
RUN_ID = "7668196429877870619"


def build_real_keyframes() -> str:
    source = json.loads((ROOT / "samples" / "real" / f"run-{RUN_ID}-116930-digital-human-real-values-redacted.json").read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = []
    for index, segment in enumerate(source["output"]["segment_infos"]):
        segment_id = f"<redacted-segment-{index:02d}>"
        duration = segment["end"] - segment["start"]
        items.extend([
            {"offset": 0, "property": "UNIFORM_SCALE", "segment_id": segment_id, "value": 1.0},
            {"offset": duration, "property": "UNIFORM_SCALE", "segment_id": segment_id, "value": 1.15},
        ])
    return json.dumps(items, ensure_ascii=False)


def run_real_structural_audit() -> dict[str, Any]:
    keyframes = build_real_keyframes()
    calls: list[tuple[str, str]] = []
    draft_url = "http://synthetic.local/get_draft?draft_id=redacted-real-draft"

    def executor(url: str, payload: str) -> dict[str, str]:
        calls.append((url, payload))
        return {"draft_url": url}

    output = run_add_keyframes({"draft_url": draft_url, "keyframes": keyframes}, executor=executor)
    return {
        "execution_id": RUN_ID,
        "node_id": "115137",
        "input_keyframe_count": len(json.loads(keyframes)),
        "executor_call_count": len(calls),
        "output_field_order": list(output),
        "output": output,
        "dynamic_values_redacted": True,
        "remote_write_executed": False,
    }


__all__ = ["build_real_keyframes", "run_real_structural_audit"]
