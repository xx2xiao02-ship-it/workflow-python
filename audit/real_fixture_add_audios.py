"""135313 解说* 的真实 Coze 脱敏结构回放与本地契约对照。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .coze_adapter_add_audios import run_migrated
from workflow_1256.add_audios import normalize_audio_infos


REAL_RUN_ID = "7668196429877870619"
NODE_ID = "135313"


def _fixture_path() -> Path:
    return Path(__file__).parents[1] / "samples" / "real" / f"run-{REAL_RUN_ID}-135313-add-audios-real-values-redacted.json"


def load_real_redacted_fixture() -> dict[str, Any]:
    return json.loads(_fixture_path().read_text(encoding="utf-8"))


def build_safe_replay_input(fixture: dict[str, Any]) -> dict[str, Any]:
    """用不可访问的占位 URL 重建真实时间线，绝不恢复页面中的签名 URL。"""

    items = []
    for item in fixture["input"]["audio_infos"]["items"]:
        items.append(
            {
                "audio_url": f"https://redacted.example/audio-{item['index']}.mp3",
                "start": item["start"],
                "end": item["end"],
                "audio_effect": item["audio_effect"],
                "volume": item["volume"],
            }
        )
    return {
        "draft_url": "https://redacted.example/draft.json",
        "audio_infos": json.dumps(items, ensure_ascii=False),
    }


def run_real_shape_audit() -> dict[str, Any]:
    fixture = load_real_redacted_fixture()
    replay_input = build_safe_replay_input(fixture)
    normalized = normalize_audio_infos(replay_input["audio_infos"])
    expected_items = fixture["input"]["audio_infos"]["items"]
    differences: list[dict[str, Any]] = []

    expected_item_keys = ["audio_url", "duration", "start", "end", "volume", "audio_effect"]
    if len(normalized) != fixture["input"]["audio_infos"]["item_count"]:
        differences.append({"path": "$.audio_infos.length", "expected": 9, "actual": len(normalized)})
    for index, (actual, expected) in enumerate(zip(normalized, expected_items)):
        if list(actual) != expected_item_keys:
            differences.append({"path": f"$.audio_infos[{index}].keys", "expected": expected_item_keys, "actual": list(actual)})
        for field in ("start", "end", "volume", "audio_effect"):
            if actual[field] != expected[field]:
                differences.append({"path": f"$.audio_infos[{index}].{field}", "expected": expected[field], "actual": actual[field]})
        if actual["duration"] is not None:
            differences.append({"path": f"$.audio_infos[{index}].duration", "expected": None, "actual": actual["duration"]})
        if index and actual["start"] != expected_items[index - 1]["end"]:
            differences.append({"path": f"$.audio_infos[{index}].start", "expected_previous_end": expected_items[index - 1]["end"], "actual": actual["start"]})

    migrated = run_migrated(replay_input)
    output = fixture["output"]
    if list(migrated) != ["audio_ids", "draft_url", "track_id"]:
        differences.append({"path": "$", "expected_keys": ["audio_ids", "draft_url", "track_id"], "actual_keys": list(migrated)})
    if len(migrated["audio_ids"]) != output["audio_ids"]["count"]:
        differences.append({"path": "$.output.audio_ids.length", "expected": output["audio_ids"]["count"], "actual": len(migrated["audio_ids"])})
    if not all(isinstance(value, str) for value in migrated["audio_ids"]):
        differences.append({"path": "$.output.audio_ids.items", "expected": "String", "actual": migrated["audio_ids"]})

    return {
        "run_id": REAL_RUN_ID,
        "node_id": NODE_ID,
        "shape_equivalent": not differences,
        "differences": differences,
        "real_output_values_redacted": True,
        "plugin_behavior_equivalent": False,
        "plugin_behavior_reason": "未执行真实草稿读取、音频下载、轨道创建和草稿保存副作用",
        "normalized_item_count": len(normalized),
        "normalized_timeline": {
            "start": normalized[0]["start"] if normalized else None,
            "end": normalized[-1]["end"] if normalized else None,
            "unit": "microseconds",
        },
    }


__all__ = ["build_safe_replay_input", "load_real_redacted_fixture", "run_real_shape_audit"]
