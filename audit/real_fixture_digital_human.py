"""116930 真实 Coze 成功执行记录的脱敏结构回放。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workflow_1256.digital_human import derive_segment_timeline, normalize_video_infos


REAL_RUN_ID = "7668196429877870619"
NODE_ID = "116930"
FIXTURE = (
    Path(__file__).parents[1]
    / "samples"
    / "real"
    / f"run-{REAL_RUN_ID}-{NODE_ID}-digital-human-real-values-redacted.json"
)


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def audit_real_shape() -> dict[str, Any]:
    fixture = load_fixture()
    raw = fixture["input"]["video_infos"]
    normalized = normalize_video_infos(raw)
    derived = derive_segment_timeline(raw)
    output_rows = fixture["output"]["segment_infos"]
    observed_timeline = [
        {
            "index": index,
            "start": row["start"],
            "end": row["end"],
            "duration": row["end"] - row["start"],
        }
        for index, row in enumerate(output_rows)
    ]
    expected = fixture["output"]["segment_infos_timeline"]
    return {
        "input_count": len(normalized),
        "output_segment_count": fixture["output"]["segment_infos_count"],
        "output_video_id_count": fixture["output"]["video_ids_count"],
        "output_segment_id_count": fixture["output"]["segment_ids_count"],
        "timeline_equal": derived == expected == observed_timeline,
        "input_output_timeline_equal": derived == observed_timeline,
        "derived_timeline": derived,
        "yaml_output_field_order": fixture["output"]["yaml_field_order"],
        "page_detail_field_order": fixture["output"]["page_detail_field_order_observed"],
    }


__all__ = ["audit_real_shape", "load_fixture"]

