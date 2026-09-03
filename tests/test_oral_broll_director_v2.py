from __future__ import annotations

import json
from pathlib import Path

from workflow_1256.oral_broll_director_v2 import (
    ContentModelError,
    SemanticModelError,
    build_content_model,
    build_semantic_model,
    build_v2_plan,
    render_user_video_script_markdown,
    validate_v1_adapter_output,
)
from workflow_1256.oral_broll_director_v2.content_model import (
    RuleContentModel,
    validate_content_plan,
)
from workflow_1256.oral_broll_director_v2.semantic_model import (
    ModelCallableSemanticModel,
    RuleSemanticModel,
    run_semantic_model_with_retry,
    validate_semantic_plan,
)
from workflow_1256.oral_broll_director_v2.pipeline import _content_material, _focus_text, _third_level_shot_parts
from workflow_1256.oral_broll_director_v2.story_model import (
    build_segment_types,
    build_story_draft,
    resolve_story_context,
    rule_segment_type,
    validate_story_coverage,
)


ROOT = Path(__file__).resolve().parents[1]


def _real_case():
    review = json.loads((ROOT / "outputs" / "director_story_reviews" / "6bb567c9e1734e5e92be26c807ba6b76" / "story_review.json").read_text(encoding="utf-8"))
    text = review["approved_copy"]
    segments = review["director_result"]["director_output"]["segments"]
    durations = [round(float(value) * 1_000_000) for value in review["director_result"]["tts"]["tts_durations"]]
    timelines = []
    start = 0
    for duration in durations:
        timelines.append({"start": start, "end": start + duration})
        start += duration
    return text, segments, timelines


def test_long_form_v2_generates_on_demand_visual_shots_and_v1_adapter():
    text, segments, timelines = _real_case()
    assert 850 <= len("".join(text.split())) <= 1_050
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.25)

    assert plan["package"] == "oral_broll_director_v2"
    assert len(plan["groups"]) == len(segments)
    assert len(plan["shots"]) > len(plan["groups"])
    assert plan["groups"][0]["section"] == "片头"
    assert plan["groups"][-1]["section"] == "片尾"
    assert plan["shots"][0]["media_type"] != "digital_human_video"
    assert all(sum(shot["scores"].values()) == 100 for shot in plan["shots"])
    assert all(shot["media_type"] in {"digital_human_video", "aigc_video", "static_image"} for shot in plan["shots"])
    assert all("content_plan" in shot for shot in plan["shots"])
    required_visual_fields = {"shot_size", "carrier_mode", "viewpoint", "composition", "visual_focus"}
    assert all(required_visual_fields.issubset(shot["visual_plan"]) for shot in plan["shots"])
    non_digital_shots = [shot for shot in plan["shots"] if shot["media_type"] != "digital_human_video"]
    assert all(shot["content_plan"]["first_frame_prompt"] for shot in non_digital_shots)
    assert all("motion_seed" in shot["content_plan"] for shot in non_digital_shots)
    assert all("camera_motion" in shot["content_plan"] for shot in plan["shots"])
    assert all("camera_fixed" in shot["content_plan"] for shot in plan["shots"])
    for shot in plan["shots"]:
        if shot["media_type"] == "digital_human_video":
            material = shot["content_plan"]
            assert all(
                key not in material
                for key in ("first_frame_material", "first_frame_prompt", "motion_seed", "image_prompt", "video_prompt")
            )
            assert material["role"] == "数字人"
            assert material["appearance_constraints"]
            assert material["speech_action"]
            assert material["sync_requirements"]
    assert any(shot["media_type"] == "aigc_video" for shot in plan["shots"])
    assert all(shot["media_type"] != "digital_human_video" or shot["digital_human_eligible"] for shot in plan["shots"])
    assert all(shot["narration_text"].strip() for shot in plan["shots"])
    assert plan["qa"]["opening_static_hold_ok"] is True
    assert all(shot["duration_us"] >= 1_500_000 for shot in plan["shots"])
    assert all(shot["duration_us"] <= 5_000_000 for shot in plan["shots"])
    opening_shots = [shot for shot in plan["shots"] if shot["section"] == "片头"]
    assert len(opening_shots) >= 2
    assert all(shot["duration_us"] >= 1_500_000 for shot in opening_shots)
    assert plan["qa"]["v1_short_route_ok"] is True
    assert plan["qa"]["v1_visual_fields_complete"] is True
    assert plan["qa"]["v1_frame_prompt_complete"] is True
    assert plan["qa"]["v1_video_prompt_complete"] is True
    assert plan["qa"]["v1_camera_fields_complete"] is True
    assert validate_v1_adapter_output(plan["v1_adapter_output"], expected_shots=len(plan["shots"])) == []

    flat = [shot for group in plan["v1_adapter_output"]["Code_list"] for shot in group["shots"]]
    assert "".join(item["narration_text"] for item in flat) == "".join(segments)
    assert flat[0]["narration_text"] == plan["shots"][0]["narration_text"]


def test_short_form_does_not_force_three_levels_or_extra_shots():
    segments = ["你以为效率只是更快吗？", "真正的效率，是把时间用在最重要的判断上。", "你怎么看？"]
    timelines = [{"start": 0, "end": 2_300_000}, {"start": 2_300_000, "end": 9_500_000}, {"start": 9_500_000, "end": 11_500_000}]
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines)
    assert len(plan["shots"]) >= len(segments)
    assert len(plan["groups"][0]["shots"]) == 1
    assert len(plan["groups"][-1]["shots"]) == 1
    assert plan["shots"][0]["timeline"]["end"] - plan["shots"][0]["timeline"]["start"] == 2_300_000
    assert plan["shots"][-1]["media_type"] != "digital_human_video"


def test_shot_durations_are_passive_tts_char_proportion_not_planned():
    segments = ["片头钩子。", "这是前半句。后半句。"]
    timelines = [{"start": 0, "end": 2_000_000}, {"start": 2_000_000, "end": 8_000_000}]
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.0)
    shots = plan["shots"]
    body_shots = [shot for shot in shots if shot["group_id"] == "g02"]
    assert len(body_shots) == 2
    short_shot, long_shot = body_shots
    short_chars = len("".join(short_shot["narration_text"].split()))
    long_chars = len("".join(long_shot["narration_text"].split()))
    expected_short_us = round(6_000_000 * short_chars / (short_chars + long_chars))
    assert short_shot["duration_us"] == expected_short_us
    assert short_shot["duration_us"] >= 1_500_000
    assert short_shot["timeline"]["end"] == long_shot["timeline"]["start"]
    assert long_shot["timeline"]["end"] == 8_000_000
    assert plan["policy"]["shot_timeline_source"] == "tts_segment_char_proportion_passive"
    assert plan["policy"]["min_shot_duration_us"] == 1_500_000
    assert plan["policy"]["max_shot_duration_us"] == 5_000_000


def test_shot_durations_stay_within_1_5_to_5_seconds():
    segments = ["很短。\n这是一段很长的话，用来验证镜头时长必须落在一点五秒到五秒之间，超出五秒会继续按语义拆分。"]
    timelines = [{"start": 0, "end": 10_000_000}]
    plan = build_v2_plan(text=segments[0], segments=segments, timelines=timelines, digital_human_target_ratio=0.0)
    assert len(plan["shots"]) >= 2
    assert all(1_500_000 <= shot["duration_us"] <= 5_000_000 for shot in plan["shots"])
    assert "".join(shot["narration_text"] for shot in plan["shots"]) == segments[0]


def test_opening_hook_splits_into_multiple_natural_shots_not_single_shot():
    segments = ["你有没有过被人阴阳完，发火还被说开不起玩笑的憋屈时刻？"]
    timelines = [{"start": 0, "end": 4_968_000}]
    plan = build_v2_plan(text=segments[0], segments=segments, timelines=timelines, digital_human_target_ratio=0.0)
    opening_shots = [shot for shot in plan["shots"] if shot["section"] == "片头"]
    assert len(opening_shots) >= 2
    assert all(shot["duration_us"] >= 1_500_000 for shot in opening_shots)
    assert opening_shots[0]["narration_text"].endswith("，")
    assert opening_shots[1]["narration_text"].startswith("发火")
    assert "".join(shot["narration_text"] for shot in opening_shots) == segments[0]


def test_video_script_markdown_shows_shot_duration_in_time_cell():
    text, segments, timelines = _real_case()
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines)
    markdown = render_user_video_script_markdown(plan)
    first_data_line = markdown.splitlines()[6]
    assert "（" in first_data_line
    assert "s）" in first_data_line


def test_semantic_plan_validation_rejects_lossy_or_unknown_fields():
    plan = {
        "segments": [
            {
                "text": "完整的一句话。",
                "semantic_units": [
                    {"text": "完整的一句话", "role": "观点推进", "visual_task": "直接展示对象"},
                ],
            }
        ]
    }
    errors = validate_semantic_plan(plan, ["完整的一句话。"])
    assert any("拼接未逐字还原" in error for error in errors)

    plan["segments"][0]["semantic_units"][0]["role"] = "不存在的角色"
    errors = validate_semantic_plan(plan, ["完整的一句话。"])
    assert any("role 不在白名单" in error for error in errors)


def test_custom_semantic_model_units_drive_shot_parts():
    segments = ["这是一个完整句子，用来测试模型分段。"]
    timelines = [{"start": 0, "end": 8_000_000}]

    class FakeSemanticModel:
        name = "fake_semantic"

        def plan(self, segments, timelines):
            return {
                "segments": [
                    {
                        "text": segments[0],
                        "semantic_units": [
                            {"text": "这是一个完整句子，", "role": "观点推进", "visual_task": "直接展示对象", "visual_intent": ""},
                            {"text": "用来测试模型分段。", "role": "观点解释", "visual_task": "解释观点", "visual_intent": ""},
                        ],
                    }
                ]
            }

    plan = build_v2_plan(
        text=segments[0],
        segments=segments,
        timelines=timelines,
        semantic_model=FakeSemanticModel(),
    )
    assert len(plan["shots"]) == 2
    assert "".join(shot["narration_text"] for shot in plan["shots"]) == segments[0]
    assert plan["policy"]["models"]["semantic_model"] == "fake_semantic"


def test_semantic_model_retry_falls_back_to_rule_on_persistent_failure():
    def broken(system, user):
        raise RuntimeError("模型不可用")

    model = ModelCallableSemanticModel(broken, name="broken")
    segments = ["完整的一句话。"]
    timelines = [{"start": 0, "end": 3_000_000}]
    plan, meta = run_semantic_model_with_retry(model, segments, timelines)
    assert meta["retried"] is True
    assert meta["fallback"] == "rule"
    assert validate_semantic_plan(plan, segments) == []


def test_content_model_whitelist_validation_rejects_unknown_values():
    plan = {
        "proposals": {
            "g01_s01": {
                "shot_size": "超广角",
                "carrier_mode": "完整人物",
                "viewpoint": "平视正面",
                "composition": "局部焦点与留白",
                "camera_motion": "固定机位",
            }
        }
    }
    errors = validate_content_plan(plan, ["g01_s01"])
    assert any("shot_size 不在白名单" in error for error in errors)


def test_model_registry_selects_rule_and_rejects_unknown_names():
    assert isinstance(build_semantic_model("rule"), RuleSemanticModel)
    assert isinstance(build_content_model("rule"), RuleContentModel)
    try:
        build_semantic_model("not_a_model")
    except SemanticModelError:
        pass
    else:
        raise AssertionError("未知语义模型名应当报错")
    try:
        build_content_model("not_a_model")
    except ContentModelError:
        pass
    else:
        raise AssertionError("未知画面创意模型名应当报错")


def test_user_script_hides_internal_section_and_score_fields():
    text, segments, timelines = _real_case()
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines)
    item = plan["user_video_script"][0]
    assert set(item) == {"shot_id", "time", "narration", "media_type", "visual_content", "prompt_material"}
    assert "scores" not in item
    assert "section" not in item
    assert item["media_type"] in {"数字人", "AIGC", "图片"}


def test_process_semantics_route_to_aigc_and_ratio_target_can_be_reached_when_capacity_exists():
    segments = [
        "片头先提出一个明确问题。",
        "一个人开始打开电脑，输入资料，然后完成整理并关闭页面。",
        "最后给出结果并邀请观众思考。",
    ]
    timelines = [{"start": 0, "end": 4_000_000}, {"start": 4_000_000, "end": 16_000_000}, {"start": 16_000_000, "end": 20_000_000}]
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.25)
    assert any(shot["media_type"] == "aigc_video" for shot in plan["shots"])
    assert plan["qa"]["digital_human_duration_ratio"] == 0.0
    assert all(sum(shot["scores"].values()) == 100 for shot in plan["shots"])
    aigc = next(shot for shot in plan["shots"] if shot["media_type"] == "aigc_video")
    assert "景别" in aigc["content_plan"]["first_frame_prompt"]
    assert "构图" in aigc["content_plan"]["first_frame_prompt"]
    assert aigc["content_plan"]["video_prompt"]


def test_digital_human_requires_direct_expression_and_tail_is_not_forced():
    segments = ["先提出一个问题。", "我想说，关键是你要做出自己的判断。", "评论区告诉我你的看法。"]
    timelines = [{"start": 0, "end": 4_000_000}, {"start": 4_000_000, "end": 8_000_000}, {"start": 8_000_000, "end": 12_000_000}]
    plan_with_target = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.25)
    # “我想说”是直接表达，且所在段为说明段，因此数字人走叠加说明（数字人底 + 说明层）。
    assert any(shot["media_type"] in {"digital_human_video", "overlay_explanation"} for shot in plan_with_target["shots"])

    plan_without_target = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.0)
    assert all(shot["media_type"] not in {"digital_human_video", "overlay_explanation"} for shot in plan_without_target["shots"])
    assert plan_without_target["shots"][-1]["section"] == "片尾"


def test_digital_human_material_excludes_visual_generation_prompts():
    shot = {
        "shot_id": "g02_s01",
        "narration_text": "我直接告诉你，先做自己的判断。",
        "visual_task": "解释观点",
        "content_proposal": {
            "shot_size": "中景",
            "carrier_mode": "完整人物",
            "viewpoint": "平视正面",
            "composition": "局部焦点与留白",
            "camera_motion": "固定机位",
        },
    }
    material = _content_material(shot, "digital_human_video", 0, 1)

    assert material["role"] == "数字人"
    assert material["speaker"] == "口播人物"
    assert material["speech_text"] == shot["narration_text"]
    assert material["appearance_constraints"]
    assert material["speech_action"]
    assert material["expression"]
    assert material["framing"]
    assert material["sync_requirements"]
    assert material["camera_motion"] == "固定机位"
    assert material["camera_fixed"] is True
    assert all(
        key not in material
        for key in ("first_frame_material", "first_frame_prompt", "motion_seed", "image_prompt", "video_prompt", "negative")
    )


def test_digital_human_markdown_lists_speech_fields_without_prompt_material():
    material = _content_material(
        {
            "shot_id": "g02_s01",
            "narration_text": "我直接告诉你，先做自己的判断。",
            "visual_task": "解释观点",
            "content_proposal": {
                "shot_size": "中景",
                "carrier_mode": "完整人物",
                "viewpoint": "平视正面",
                "composition": "局部焦点与留白",
                "camera_motion": "固定机位",
            },
        },
        "digital_human_video",
        0,
        1,
    )
    markdown = render_user_video_script_markdown(
        {
            "shots": [],
            "user_video_script": [
                {
                    "shot_id": "g02_s01",
                    "time": {"start_us": 0, "end_us": 4_000_000},
                    "narration": "我直接告诉你，先做自己的判断。",
                    "media_type": "数字人",
                    "visual_content": "中景，平视正面",
                    "prompt_material": material,
                }
            ],
        }
    )
    row = next(line for line in markdown.splitlines() if "g02_s01" in line)
    assert "人物外观约束" in row
    assert "TTS同步" in row
    assert "首帧提示词" not in row
    assert "motion_seed" not in row
    assert "完整首帧/视频提示词" not in row


def test_digital_human_quota_fill_reaches_target_with_audience_address_shots():
    segments = [
        "开头只做铺垫，不直接面对观众。",
        "这句话里有你，是直接对观众说的观点。",
        "这句也有你，继续面向观众讲清楚。",
        "最后这句同样有你，用来收尾互动。",
    ]
    timelines = [
        {"start": 0, "end": 4_000_000},
        {"start": 4_000_000, "end": 8_000_000},
        {"start": 8_000_000, "end": 12_000_000},
        {"start": 12_000_000, "end": 16_000_000},
    ]
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.20)
    assert plan["qa"]["digital_human_duration_ratio"] >= 0.19
    assert any(
        shot["media_type"] in {"digital_human_video", "overlay_explanation"}
        and shot["digital_human_candidate_tier"] == "audience_address"
        for shot in plan["shots"]
    )


def test_serial_media_selection_digital_then_aigc_then_image():
    segments = [
        "先提出一个问题。",
        "以前我总觉得，遇到这种情况，画面需要表现前后的明显变化。",
        "真正重要的是保留这个结果。",
    ]
    timelines = [
        {"start": 0, "end": 4_000_000},
        {"start": 4_000_000, "end": 10_000_000},
        {"start": 10_000_000, "end": 16_000_000},
    ]
    plan = build_v2_plan(
        text="".join(segments),
        segments=segments,
        timelines=timelines,
        digital_human_target_ratio=0.20,
    )
    assert plan["policy"]["media_selection_mode"] == "explanation_route_then_serial_digital_then_aigc_then_image"
    digital_indices = {shot["index"] for shot in plan["shots"] if shot["media_type"] == "digital_human_video"}
    aigc_indices = {shot["index"] for shot in plan["shots"] if shot["media_type"] == "aigc_video"}
    assert digital_indices.isdisjoint(aigc_indices)
    assert any(shot["aigc_eligible"] and shot["media_type"] == "aigc_video" for shot in plan["shots"])
    assert plan["shots"][0]["media_type"] == "aigc_video"
    assert all(
        shot["media_type"] == "static_image"
        for shot in plan["shots"]
        if shot["index"] != 0 and shot["duration_us"] < 3_000_000
    )
    assert plan["policy"]["first_shot_must_be_aigc"] is True
    assert plan["policy"]["segment_timeline_unit"] == "microseconds"
    assert plan["group_timelines"] == [
        {"segment_index": 0, "segment_id": "g01", "segment_text": segments[0], "start_us": 0, "end_us": 4_000_000, "duration_us": 4_000_000},
        {"segment_index": 1, "segment_id": "g02", "segment_text": segments[1], "start_us": 4_000_000, "end_us": 10_000_000, "duration_us": 6_000_000},
        {"segment_index": 2, "segment_id": "g03", "segment_text": segments[2], "start_us": 10_000_000, "end_us": 16_000_000, "duration_us": 6_000_000},
    ]
    assert plan["policy"]["segment_timeline_source"] == "shots"
    assert plan["segment_timelines"] == plan["secondary_segment_timelines"]
    assert isinstance(plan["opening_title"], str)
    assert plan["opening_title"].strip()
    assert plan["policy"]["secondary_segment_timeline_unit"] == "microseconds"
    assert plan["policy"]["secondary_segment_timeline_source"] == "shots"
    assert plan["policy"]["transition_timeline_source"] == "secondary_segment_timelines"
    assert plan["secondary_segment_timelines"] == [
        {
            "secondary_segment_index": index,
            "shot_index": shot["index"],
            "shot_id": shot["shot_id"],
            "group_id": shot["group_id"],
            "segment_text": shot["narration_text"],
            "start_us": shot["timeline"]["start"],
            "end_us": shot["timeline"]["end"],
            "duration_us": shot["duration_us"],
        }
        for index, shot in enumerate(plan["shots"])
    ]
    assert plan["transition_timeline_unit"] == "microseconds"
    assert [item["at_us"] for item in plan["transition_points"]] == [
        item["end_us"] for item in plan["secondary_segment_timelines"][:-1]
    ]
    assert all(sum(shot["scores"].values()) == 100 for shot in plan["shots"])


def test_longer_than_two_seconds_adjusts_image_raw_score_and_redistributes_points():
    short_plan = build_v2_plan(
        text="开头。呈现一个结果。结尾。",
        segments=["开头。", "呈现一个结果。", "结尾。"],
        timelines=[{"start": 0, "end": 1_000_000}, {"start": 1_000_000, "end": 2_500_000}, {"start": 2_500_000, "end": 3_500_000}],
        digital_human_target_ratio=0.0,
    )
    long_plan = build_v2_plan(
        text="开头。呈现一个结果。结尾。",
        segments=["开头。", "呈现一个结果。", "结尾。"],
        timelines=[{"start": 0, "end": 1_000_000}, {"start": 1_000_000, "end": 5_000_000}, {"start": 5_000_000, "end": 6_000_000}],
        digital_human_target_ratio=0.0,
    )
    short_shot = short_plan["shots"][1]
    long_shot = long_plan["shots"][1]
    assert short_shot["long_shot_image_score_adjustment"] is False
    assert long_shot["long_shot_image_score_adjustment"] is True
    assert long_shot["scores"]["image"] < short_shot["scores"]["image"]
    assert long_shot["scores"]["digital_human"] > short_shot["scores"]["digital_human"]
    assert long_shot["scores"]["aigc"] > short_shot["scores"]["aigc"]
    assert sum(long_shot["scores"].values()) == 100


def test_over_three_seconds_image_penalty_is_steeper_than_over_two_seconds():
    two_second_plan = build_v2_plan(
        text="开头。呈现一个结果。结尾。",
        segments=["开头。", "呈现一个结果。", "结尾。"],
        timelines=[{"start": 0, "end": 1_000_000}, {"start": 1_000_000, "end": 3_500_000}, {"start": 3_500_000, "end": 4_500_000}],
        digital_human_target_ratio=0.0,
    )
    three_second_plan = build_v2_plan(
        text="开头。呈现一个结果。结尾。",
        segments=["开头。", "呈现一个结果。", "结尾。"],
        timelines=[{"start": 0, "end": 1_000_000}, {"start": 1_000_000, "end": 5_000_000}, {"start": 5_000_000, "end": 6_000_000}],
        digital_human_target_ratio=0.0,
    )
    two_shot = two_second_plan["shots"][1]
    three_shot = three_second_plan["shots"][1]
    assert three_shot["scores"]["image"] < two_shot["scores"]["image"]
    assert three_shot["scores"]["aigc"] > two_shot["scores"]["aigc"]
    assert sum(three_shot["scores"].values()) == 100


def test_third_level_keeps_one_second_level_case_block_and_splits_parallel_cases_by_semantics():
    second_level = (
        "就像职场上那种人啊："
        "你拿了好成绩，他说你就是运气好；"
        "你带饭上班，他调侃你是为了省钱；"
        "你单身没谈恋爱，他追着问是不是眼光太高。"
    )
    parts = _third_level_shot_parts(second_level, 16_000_000)
    assert parts == [
        "就像职场上那种人啊：",
        "你拿了好成绩，他说你就是运气好；",
        "你带饭上班，他调侃你是为了省钱；",
        "你单身没谈恋爱，他追着问是不是眼光太高。",
    ]
    assert "".join(parts) == second_level


def test_duration_soft_target_never_splits_a_complete_semantic_unit_by_itself():
    sentence = "这一段虽然时长超过了软目标，但是同一对象、同一动作和同一状态都没有发生变化。"
    assert _third_level_shot_parts(sentence, 9_000_000) == [sentence]


def test_long_case_block_uses_visual_transition_not_equal_duration_split():
    text = "他先打开电脑输入资料，然后整理文件，最后关闭页面。"
    parts = _third_level_shot_parts(text, 9_000_000)
    assert "".join(parts) == text
    assert len(parts) == 2
    assert parts[0].endswith("，")
    assert parts[1].startswith("然后")


def test_quoted_question_keeps_closing_quote_with_question():
    second_level = "他就反问：“你好像对我这事儿挺关心啊？”\n就这一句，聪明人自己就找台阶下了。"
    parts = _third_level_shot_parts(second_level, 12_000_000)
    assert "".join(parts) == second_level
    assert parts[0] == "他就反问：“你好像对我这事儿挺关心啊？”"
    assert parts[1] == "\n就这一句，聪明人自己就找台阶下了。"


def test_hard_stop_inside_quote_does_not_orphan_trailing_punctuation():
    second_level = "你说顾虑：“实在凑不开”。\n对方自然就明白了。"
    parts = _third_level_shot_parts(second_level, 10_000_000)
    assert "".join(parts) == second_level
    assert parts[0] == "你说顾虑：“实在凑不开”。"
    assert parts[1] == "\n对方自然就明白了。"


def test_visual_focus_clips_at_punctuation_boundary_not_mid_word():
    long_text = "他问你赚多少钱、怎么还不结婚、方案是不是靠别人帮的，你就笑着看着他说：“你好像对我这事儿挺关心啊？”"
    focus = _focus_text(long_text)
    assert focus == "他问你赚多少钱、怎么还不结婚、方案是不是靠别人帮的，你就笑着看着他说："


def test_segment_type_rule_classifies_story_explanation_and_mixed():
    assert rule_segment_type("就像那个同事，每天最早到公司，却用最笨的方法整理数据。", "还原案例") == ("story", False)
    assert rule_segment_type("真正的原因很简单：效率不是做更多，而是把时间花在最重要的判断上。", "解释观点") == ("explanation", False)
    assert rule_segment_type("一组数字可以说明：同样八小时，主动复盘的人产出是埋头重复的人的三倍。", "呈现结果") == ("explanation", False)
    # 表现过程带方法标记 → 说明；带人物情节 → 故事；否则模糊交模型。
    assert rule_segment_type("第一招，先列清单；第二招，再复盘。", "表现过程") == ("explanation", False)
    assert rule_segment_type("他先打开电脑，然后整理文件，最后关闭页面。", "表现过程") == ("story", False)
    assert rule_segment_type("这个过程需要一步一步来。", "表现过程") == ("mixed", True)


def test_segment_type_model_retry_falls_back_to_rule_defaults():
    segments = ["开头。", "这个过程需要一步一步来。", "结尾。"]
    timelines = [{"start": 0, "end": 2_000_000}, {"start": 2_000_000, "end": 5_000_000}, {"start": 5_000_000, "end": 7_000_000}]

    class BrokenSegmentTypeModel:
        name = "broken_segment_type"

        def plan(self, groups):
            raise RuntimeError("模型不可用")

    result = build_segment_types(segments, timelines, ["直接展示对象", "表现过程", "直接展示对象"], model=BrokenSegmentTypeModel())
    assert result["segment_types"]["g02"] == "mixed"
    assert result["meta"]["fallback"] == "rule"
    assert result["meta"]["retried"] is True


def test_explanation_segment_produces_fullscreen_remotion_shots():
    segments = ["先提出问题。", "一组数字可以说明：同样八小时，主动复盘的人产出是埋头重复的人的三倍。", "你怎么看？"]
    timelines = [{"start": 0, "end": 3_000_000}, {"start": 3_000_000, "end": 8_000_000}, {"start": 8_000_000, "end": 10_000_000}]
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.0)
    body_group = next(group for group in plan["groups"] if group["segment_type"] == "explanation")
    explanation_shots = [shot for shot in plan["shots"] if shot["group_id"] == body_group["group_id"]]
    assert explanation_shots
    assert all(shot["media_type"] == "explanation_video" for shot in explanation_shots)
    assert all(shot["explanation_candidate"] for shot in explanation_shots)
    assert all(shot["content_plan"]["remotion"]["role"] == "fullscreen" for shot in explanation_shots)
    assert all(
        shot["content_plan"]["remotion"]["template_type"]
        in {"text_outline", "step_flow", "data_compare", "quote_highlight", "timeline", "chart"}
        for shot in explanation_shots
    )
    assert all(shot["content_plan"]["remotion"]["props"] for shot in explanation_shots)


def test_direct_expression_explanation_shot_becomes_overlay_with_digital_human_base():
    segments = ["开头。", "我想说，关键是你要做出自己的判断。", "结尾。"]
    timelines = [{"start": 0, "end": 3_000_000}, {"start": 3_000_000, "end": 7_000_000}, {"start": 7_000_000, "end": 9_000_000}]
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=1.0)
    overlay = [shot for shot in plan["shots"] if shot["media_type"] == "overlay_explanation"]
    assert overlay
    for shot in overlay:
        assert shot["explanation_mode"] == "overlay"
        assert shot["content_plan"]["remotion"]["role"] == "overlay"
        assert shot["content_plan"]["remotion"]["base_media_type"] == "digital_human_video"
    assert plan["qa"]["digital_human_duration_ratio"] > 0
    assert plan["qa"]["overlay_selected_count"] == len(overlay)


def test_mixed_segment_splits_explanation_candidate_only_for_explanation_sub_shots():
    segments = ["开头。", "这个过程需要一步一步完成，一组数字可以说明：同样八小时，产出差三倍。", "结尾。"]
    timelines = [{"start": 0, "end": 3_000_000}, {"start": 3_000_000, "end": 12_000_000}, {"start": 12_000_000, "end": 14_000_000}]
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.0)
    mixed_group = next(group for group in plan["groups"] if group["segment_type"] == "mixed")
    shots = [shot for shot in plan["shots"] if shot["group_id"] == mixed_group["group_id"]]
    assert len(shots) >= 2
    explanation_shots = [shot for shot in shots if shot["explanation_candidate"]]
    non_explanation_shots = [shot for shot in shots if not shot["explanation_candidate"]]
    assert explanation_shots and non_explanation_shots
    assert all(shot["media_type"] == "explanation_video" for shot in explanation_shots)
    assert all(shot["media_type"] != "explanation_video" for shot in non_explanation_shots)


def test_story_draft_rule_scaffold_covers_only_story_segments_contiguously():
    segments = [
        "片头钩子。",
        "就像那个同事，每天最早到公司，却用最笨的方法整理数据。",
        "真正的原因很简单：效率是把时间花在最重要的判断上。",
        "所以他开始每天先复盘，再做最重要的那一件事。",
        "片尾互动。",
    ]
    timelines = [
        {"start": 0, "end": 4_000_000},
        {"start": 4_000_000, "end": 9_000_000},
        {"start": 9_000_000, "end": 14_000_000},
        {"start": 14_000_000, "end": 19_000_000},
        {"start": 19_000_000, "end": 23_000_000},
    ]
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.0)
    story_draft = plan["story_draft"]
    assert story_draft is not None
    assert story_draft["origin"] == "rule_scaffold"
    assert story_draft["story_group_ids"] == ["g02", "g04"]
    assert validate_story_coverage(story_draft["story"], story_draft["story_group_ids"]) == []
    story_shots = [shot for shot in plan["shots"] if shot["group_id"] in story_draft["story_group_ids"]]
    assert all(shot["story_context"]["scene_id"] for shot in story_shots)
    assert all(shot["content_plan"]["story_context"]["scene_id"] for shot in story_shots)
    assert all(shot["media_type"] not in {"explanation_video", "overlay_explanation"} for shot in story_shots)


def test_story_draft_model_transport_remaps_group_ids_to_original():
    segments = [
        "片头钩子。",
        "他先打开电脑，然后整理文件，最后关闭页面。",
        "真正的原因很简单：效率是把时间花在最重要的判断上。",
        "所以他开始每天先复盘，再做最重要的那一件事。",
        "片尾互动。",
    ]
    timelines = [
        {"start": 0, "end": 3_000_000},
        {"start": 3_000_000, "end": 8_000_000},
        {"start": 8_000_000, "end": 12_000_000},
        {"start": 12_000_000, "end": 17_000_000},
        {"start": 17_000_000, "end": 20_000_000},
    ]
    visual_tasks = ["直接展示对象", "表现过程", "解释观点", "表现过程", "直接展示对象"]
    segment_types = {"g01": "hook", "g02": "story", "g03": "explanation", "g04": "story", "g05": "ending"}

    def fake_transport(system, user):
        return json.dumps(
            {
                "movie_outline": {
                    "protagonist": "主角",
                    "protagonist_goal": "目标",
                    "central_conflict": "冲突",
                    "character_arc": "变化",
                    "ending_hook": "悬念",
                },
                "silent_story_text": "故事正文。",
                "scene_groups": [
                    {
                        "scene_id": "scene_01",
                        "group_ids": ["g01"],
                        "scene_purpose": "场景任务",
                        "state_before": "开始状态",
                        "visible_conflict": "可见冲突",
                        "turn": "本场变化",
                        "state_after": "结束状态",
                    },
                    {
                        "scene_id": "scene_02",
                        "group_ids": ["g02"],
                        "scene_purpose": "场景任务",
                        "state_before": "开始状态",
                        "visible_conflict": "可见冲突",
                        "turn": "本场变化",
                        "state_after": "结束状态",
                    },
                ],
                "narration_mappings": [
                    {
                        "group_id": "g01",
                        "scene_id": "scene_01",
                        "semantic_mapping": "映射一",
                        "silent_action": "动作一",
                        "metaphor": "隐喻一",
                        "state_before": "承接状态",
                        "state_after": "落点",
                        "bridge_to_next": "承接",
                    },
                    {
                        "group_id": "g02",
                        "scene_id": "scene_01",
                        "semantic_mapping": "映射二",
                        "silent_action": "动作二",
                        "metaphor": "隐喻二",
                        "state_before": "承接状态",
                        "state_after": "落点",
                        "bridge_to_next": "承接",
                    },
                ],
            }
        )

    draft = build_story_draft(
        "".join(segments),
        segments,
        timelines,
        segment_types,
        visual_tasks,
        transport=fake_transport,
    )
    assert draft is not None
    assert draft["origin"] == "cinematic_model"
    assert draft["story_group_ids"] == ["g02", "g04"]
    flattened = [gid for scene in draft["story"]["scene_groups"] for gid in scene["group_ids"]]
    assert flattened == ["g02", "g04"]
    assert [mapping["group_id"] for mapping in draft["story"]["narration_mappings"]] == ["g02", "g04"]


def test_explanation_short_shots_bypass_v1_short_image_route_constraint():
    segments = ["开头。", "原因很简单：这个数字是三点五。", "结尾。"]
    timelines = [{"start": 0, "end": 2_000_000}, {"start": 2_000_000, "end": 4_000_000}, {"start": 4_000_000, "end": 6_000_000}]
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.0)
    explanation_shots = [shot for shot in plan["shots"] if shot["media_type"] == "explanation_video"]
    assert explanation_shots
    assert any(shot["duration_us"] < 3_000_000 for shot in explanation_shots)
    assert plan["qa"]["v1_short_route_ok"] is True


def test_user_script_supports_five_media_states_and_hides_internal_fields():
    segments = ["开头。", "我想说，关键是你要做出自己的判断。", "就像那个同事，每天最早到公司，却用最笨的方法整理数据。", "结尾。"]
    timelines = [
        {"start": 0, "end": 3_000_000},
        {"start": 3_000_000, "end": 7_000_000},
        {"start": 7_000_000, "end": 12_000_000},
        {"start": 12_000_000, "end": 14_000_000},
    ]
    plan = build_v2_plan(text="".join(segments), segments=segments, timelines=timelines, digital_human_target_ratio=0.4)
    item = plan["user_video_script"][0]
    assert set(item) == {"shot_id", "time", "narration", "media_type", "visual_content", "prompt_material"}
    assert all(
        shot["media_type"] in {"数字人", "AIGC", "图片", "说明动效", "叠加说明"}
        for shot in plan["user_video_script"]
    )
    assert any(shot["media_type"] == "叠加说明" for shot in plan["user_video_script"])
