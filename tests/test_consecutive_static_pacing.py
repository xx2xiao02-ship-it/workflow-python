from __future__ import annotations

from workflow_1256.governance.consecutive_static_pacing import (
    apply_consecutive_static_video_pacing,
)


def _input(durations_us: list[int]) -> tuple[list[dict], dict, dict]:
    cursor = 0
    shots = []
    routes = []
    code_shots = []
    timelines = []
    for index, duration_us in enumerate(durations_us, start=1):
        start = cursor
        end = start + duration_us
        shot_id = f"g01_s{index:02d}"
        shots.append({
            "shot_id": shot_id,
            "group_id": "g01",
            "timeline": {"start_us": start, "end_us": end},
        })
        routes.append({
            "shot_id": shot_id,
            "media_type": "aigc_video" if index == 1 else "static_image",
        })
        code_shots.append({
            "source_text": f"语义{index}",
            "clip_role": "状态建立",
            "story_beat": f"状态{index}",
            "narration_text": f"旁白{index}",
        })
        timelines.append({"start": start, "end": end})
        cursor = end
    return (
        [{
            "shots": code_shots,
            "timelines": timelines,
            "clip_duration": [round(value / 1_000_000, 3) for value in durations_us],
            "int_duration": [3 for _ in durations_us],
        }],
        {"shots": shots},
        {"routes": routes},
    )


def test_four_static_candidates_merge_last_two_when_same_group_within_five_point_five_seconds() -> None:
    code_list, lock, routes = _input([3_200_000, 3_100_000, 3_200_000, 2_600_000, 2_700_000])

    result = apply_consecutive_static_video_pacing(code_list, lock, routes)

    group = result["code_list"][0]
    assert result["policy"]["original_visual_slot_count"] == 5
    assert result["policy"]["result_visual_slot_count"] == 4
    assert result["policy"]["merged_spans"] == [{
        "source_slot_ids": ["g01_s04", "g01_s05"],
        "duration_us": 5_300_000,
        "static_streak_weight": 1,
    }]
    assert group["shots"][-1]["narration_text"] == "旁白4旁白5"
    assert group["shots"][-1]["media_pacing"]["force_aigc"] is True
    assert group["timelines"][-1] == {"start": 9_500_000, "end": 14_800_000}
    assert group["int_duration"][-1] == 6


def test_four_static_candidates_promote_a_native_length_slot_when_pair_cannot_merge() -> None:
    code_list, lock, routes = _input([3_200_000, 3_100_000, 3_300_000, 3_400_000, 2_700_000])

    result = apply_consecutive_static_video_pacing(code_list, lock, routes)

    group = result["code_list"][0]
    assert result["policy"]["result_visual_slot_count"] == 5
    assert result["policy"]["merged_spans"] == []
    assert result["policy"]["promoted_single_slot_ids"] == ["g01_s04"]
    assert group["shots"][3]["media_pacing"]["action"] == "promote_single_static_to_aigc"
