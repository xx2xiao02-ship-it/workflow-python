"""节点 1904923 的真实 Coze 脱敏结构样本读取器。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


FIXTURE = Path(__file__).parents[1] / "samples" / "real" / "run-7668196429877870619-174651-aigc-animation-real-values-redacted.json"


def load_real_input() -> dict[str, Any]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    infos = json.loads(raw["input"]["video_infos"])
    return {
        "ctype": "UNIFORM_SCALE",
        "offsets": "0|100",
        "values": "1|1.15",
        "segment_infos": [
            {"id": f"redacted-segment-{index:02d}", "start": item["start"], "end": item["end"]}
            for index, item in enumerate(infos)
        ],
    }


__all__ = ["load_real_input"]
