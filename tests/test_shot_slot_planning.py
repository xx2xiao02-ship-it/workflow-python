from __future__ import annotations

from workflow_1256.shot_slot_planning import (
    merge_visual_design_into_locked_slots,
    plan_stt_caption_shot_slots,
    plan_tts_semantic_shot_slots,
)


def _beats(*rhythms: str):
    return [{"rhythm": rhythm} for rhythm in rhythms]


def test_slot_plan_locks_semantic_narration_and_real_tts_timeline_before_visual_design():
    segments = [
        "这毛病说穿了就是专业诅咒：你要是骑车骑得溜到能撒把，根本想不明白新手为啥跨上车就晃，满脑子都是这有啥难的，当然教不会人，也赚不到外行的钱。",
        "你开个修车铺就懂了？顾客找上门本来就是不懂车的，他既不知道你技术是不是全城第一，也没闲心对比十家店的报价，凭啥选你？",
    ]
    timelines = [{"start": 0, "end": 14_016_000}, {"start": 14_016_000, "end": 25_128_000}]
    result = plan_tts_semantic_shot_slots(timelines, segments, _beats("wrong", "why"))

    assert result["policy"] == "tts_timeline_then_semantic_slot_then_visual_design_v1"
    assert result["pacing_plan"]["required_shot_count"] == 6
    # 全片总数先固定为 6；2～5 秒窗口下两段各分配 3 个语义坑位，
    # 验证节奏优先不会破坏 TTS 时间线。
    assert [group["required_shot_count"] for group in result["groups"]] == [3, 3]
    for source, group, source_timeline in zip(segments, result["groups"], timelines, strict=True):
        assert "".join(slot["narration_text"] for slot in group["slots"]) == source
        assert group["slots"][0]["timeline"]["start"] == source_timeline["start"]
        assert group["slots"][-1]["timeline"]["end"] == source_timeline["end"]
        assert all(2.0 <= slot["duration_s"] <= 5.0 for slot in group["slots"])


def test_visual_design_cannot_change_locked_narration_or_timeline():
    slots = [
        {"narration_text": "先说第一句。", "timeline": {"start": 0, "end": 3_000_000}, "int_duration": 3},
        {"narration_text": "再说第二句。", "timeline": {"start": 3_000_000, "end": 6_000_000}, "int_duration": 3},
    ]
    visual = {
        "shots": [
            {"narration_text": "先说第一句。", "source_text": "观点抛出", "clip_role": "信息触发", "story_beat": "问题被提出"},
            {"narration_text": "再说第二句。", "source_text": "后果显现", "clip_role": "压力升级", "story_beat": "矛盾进一步暴露"},
        ]
    }
    merged = merge_visual_design_into_locked_slots(slots, visual)
    assert merged["timelines"] == [{"start": 0, "end": 3_000_000}, {"start": 3_000_000, "end": 6_000_000}]
    assert merged["clip_duration"] == [3.0, 3.0]
    assert merged["shots"][0]["narration_text"] == "先说第一句。"
    visual["shots"][0]["narration_text"] = "先说第一句，再说第二句。"
    visual["shots"][1].pop("narration_text")
    merged = merge_visual_design_into_locked_slots(slots, visual)
    assert [shot["narration_text"] for shot in merged["shots"]] == [
        "先说第一句。",
        "再说第二句。",
    ]


def test_stt_caption_timeline_merges_below_2_5_and_splits_above_5():
    captions = [
        {"caption_id": "g01.stt.001", "group_id": "g01", "text": "短一", "start_us": 0, "end_us": 1_000_000},
        {"caption_id": "g01.stt.002", "group_id": "g01", "text": "短二", "start_us": 1_000_000, "end_us": 2_300_000},
        {"caption_id": "g01.stt.003", "group_id": "g01", "text": "这一段比较长需要拆分处理。", "start_us": 2_300_000, "end_us": 8_500_000},
        {"caption_id": "g01.stt.004", "group_id": "g01", "text": "最后一句。", "start_us": 8_500_000, "end_us": 12_000_000},
    ]
    result = plan_stt_caption_shot_slots(
        captions,
        [{"start": 0, "end": 12_000_000}],
    )
    slots = result["groups"][0]["slots"]
    assert result["policy"] == "stt_caption_timeline_merge_below_2_5_split_above_5_v1"
    assert slots[0]["source_caption_ids"][:2] == ["g01.stt.001", "g01.stt.002"]
    assert "".join(slot["narration_text"] for slot in slots) == "短一短二这一段比较长需要拆分处理。最后一句。"
    assert slots[0]["timeline"]["start"] == 0
    assert slots[-1]["timeline"]["end"] == 12_000_000
    assert all(2.5 <= slot["duration_s"] <= 5.0 for slot in slots)
    assert len(slots) == 3

def test_locked_narration_preserves_newline_when_model_omits_or_cleans_it():
    """复现真实任务：模型清洗坑位边界换行，也不能让原旁白丢字。"""

    slots = [
        {"narration_text": "第一句。\n", "timeline": {"start": 0, "end": 3_000_000}, "int_duration": 3},
        {"narration_text": "第二句。", "timeline": {"start": 3_000_000, "end": 6_000_000}, "int_duration": 3},
    ]
    visual = {
        "shots": [
            {"narration_text": "第一句。", "source_text": "观点抛出", "clip_role": "信息触发", "story_beat": "问题被提出"},
            {"source_text": "后果显现", "clip_role": "压力升级", "story_beat": "矛盾进一步暴露"},
        ]
    }

    merged = merge_visual_design_into_locked_slots(slots, visual)

    assert "".join(shot["narration_text"] for shot in merged["shots"]) == "第一句。\n第二句。"
