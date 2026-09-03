"""真实 Coze 运行记录的关键帧结构审计（动态 ID 脱敏）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workflow_1256.keyframes_infos import run_keyframes_infos


ROOT = Path(__file__).parents[1]
REAL_RUN_ID = "7668196429877870619"


def load_segment_infos() -> list[dict[str, Any]]:
    source = json.loads(
        (ROOT / "samples" / "real" / f"run-{REAL_RUN_ID}-116930-digital-human-real-values-redacted.json").read_text(encoding="utf-8")
    )
    return [
        {"id": f"<redacted-segment-{index:02d}>", "start": item["start"], "end": item["end"]}
        for index, item in enumerate(source["output"]["segment_infos"])
    ]


def run_real_structural_audit() -> dict[str, Any]:
    params = {
        "ctype": "UNIFORM_SCALE",
        "offsets": "0|100",
        "values": "1|1.15",
        "segment_infos": load_segment_infos(),
    }
    output = json.loads(run_keyframes_infos(params)["keyframes_infos"])
    return {
        "execution_id": REAL_RUN_ID,
        "node_id": "151394",
        "input_segment_count": len(params["segment_infos"]),
        "output_keyframe_count": len(output),
        "output_field_order": list(output[0]) if output else [],
        "first_segment_pattern": output[:2],
        "last_offset": output[-1]["offset"] if output else None,
        "real_dynamic_ids_redacted": True,
        "source_observation": "Coze 页面观察到18个片段、36个关键帧，offset为片段内相对微秒偏移。",
    }


__all__ = ["load_segment_infos", "run_real_structural_audit"]
