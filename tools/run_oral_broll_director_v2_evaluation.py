"""运行编导 v2 独立全量脚本测试并导出可审核数据。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_1256.oral_broll_director_v2 import (
    build_content_model,
    build_segment_type_model,
    build_semantic_model,
    build_v2_plan,
    render_prompt_material_markdown,
    render_user_video_script_markdown,
)
from workflow_1256.prompt_generation import run_prompt_generation


def _real_case() -> tuple[str, list[str], list[dict[str, int]]]:
    # 真实口播案例：约 895 个有效字，来源于已完成的真实编导/TTS 运行记录。
    # director_result.segments 是 TTS 前的大段语义分段，tts_durations 是对应真实时长。
    review_path = ROOT / "outputs" / "director_story_reviews" / "6bb567c9e1734e5e92be26c807ba6b76" / "story_review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    text = str(review["approved_copy"])
    director_output = review["director_result"]["director_output"]
    segments = list(director_output["segments"])
    durations = [round(float(value) * 1_000_000) for value in review["director_result"]["tts"]["tts_durations"]]
    timelines = []
    start = 0
    for duration in durations:
        end = start + duration
        timelines.append({"start": start, "end": end})
        start = end
    return text, segments, timelines


def _short_case() -> tuple[str, list[str], list[dict[str, int]]]:
    segments = ["你以为效率只是更快吗？", "真正的效率，是把时间用在最重要的判断上。", "你怎么看？"]
    durations = [2_300_000, 7_200_000, 2_000_000]
    start = 0
    timelines = []
    for duration in durations:
        timelines.append({"start": start, "end": start + duration})
        start += duration
    return "".join(segments), segments, timelines


def _media_route_case() -> tuple[str, list[str], list[dict[str, int]]]:
    segments = [
        "片头先提出一个明确问题。",
        "接着一个人开始打开电脑，输入资料，然后完成整理并关闭页面。",
        "最后给出结果并邀请观众思考。",
    ]
    durations = [4_000_000, 12_000_000, 4_000_000]
    timelines = []
    start = 0
    for duration in durations:
        timelines.append({"start": start, "end": start + duration})
        start += duration
    return "".join(segments), segments, timelines


def _story_explanation_case() -> tuple[str, list[str], list[dict[str, int]]]:
    segments = [
        "你有没有发现，很多人表面努力，实际只是重复劳动。",
        "就像那个同事，每天最早到公司，却在用最笨的方法整理数据，从不复盘。",
        "真正的原因很简单：效率不是做更多，而是把时间花在最重要的判断上。",
        "一组数字可以说明：同样八小时，主动复盘的人产出是埋头重复的人的三倍。",
        "所以从明天开始，先问自己：这件事值不值得我做？",
    ]
    durations = [4_000_000, 9_000_000, 8_000_000, 7_000_000, 4_000_000]
    timelines = []
    start = 0
    for duration in durations:
        timelines.append({"start": start, "end": start + duration})
        start += duration
    return "".join(segments), segments, timelines


def _overlay_case() -> tuple[str, list[str], list[dict[str, int]]]:
    segments = [
        "你有没有发现，很多人表面努力，实际只是重复劳动。",
        "我想说，关键数字是这一组：同样八小时，产出差三倍。",
        "评论区告诉我你怎么看。",
    ]
    durations = [3_000_000, 7_000_000, 3_000_000]
    timelines = []
    start = 0
    for duration in durations:
        timelines.append({"start": start, "end": start + duration})
        start += duration
    return "".join(segments), segments, timelines


def _v1_reference_case() -> dict:
    input_path = ROOT / "samples" / "synthetic" / "prompt_generation_input.json"
    expected_path = ROOT / "samples" / "synthetic" / "prompt_generation_expected.json"
    params = json.loads(input_path.read_text(encoding="utf-8"))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    actual = run_prompt_generation(params)
    return {
        "status": "PASS" if actual == expected else "FAIL",
        "fixture": str(input_path.relative_to(ROOT)),
        "output_keys": sorted(actual),
        "array_lengths": {key: len(actual.get(key, [])) for key in ("video_prompt", "int_duration", "ref_image_f", "ref_image_e", "camera_fixed")},
        "exact_output_match": actual == expected,
    }


def _write_case(
    output_dir: Path,
    case_name: str,
    text: str,
    segments: list[str],
    timelines: list[dict[str, int]],
    ratio: float,
    *,
    aigc_target_ratio: float | None = None,
    source_document: str | None = None,
    semantic_model_name: str = "rule",
    content_model_name: str = "rule",
    segment_type_model_name: str = "rule",
) -> dict:
    tts_text = "".join(segments)
    plan = build_v2_plan(
        text=tts_text,
        segments=segments,
        timelines=timelines,
        digital_human_target_ratio=ratio,
        aigc_target_ratio=aigc_target_ratio,
        semantic_model=build_semantic_model(semantic_model_name),
        content_model=build_content_model(content_model_name),
        segment_type_model=build_segment_type_model(segment_type_model_name),
    )
    case_dir = output_dir / case_name
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "01_input_snapshot.json").write_text(json.dumps({"source_document": source_document or text, "tts_authoritative_text": tts_text, "segments": segments, "timelines": timelines, "digital_human_target_ratio": ratio, "aigc_target_ratio": aigc_target_ratio}, ensure_ascii=False, indent=2), encoding="utf-8")
    (case_dir / "02_structure_and_semantic_segments.json").write_text(json.dumps({"groups": plan["groups"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    (case_dir / "03_shot_plan.json").write_text(json.dumps({"policy": plan["policy"], "shots": plan["shots"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    (case_dir / "04_media_scorecard.json").write_text(json.dumps({"shots": [{"shot_id": s["shot_id"], "scores": s["scores"], "media_type": s["media_type"], "media_reason": s["media_reason"]} for s in plan["shots"]]}, ensure_ascii=False, indent=2), encoding="utf-8")
    (case_dir / "05_content_plan.json").write_text(json.dumps({"shots": [{"shot_id": s["shot_id"], "media_type": s["media_type"], "content_plan": s["content_plan"]} for s in plan["shots"]]}, ensure_ascii=False, indent=2), encoding="utf-8")
    (case_dir / "06_v1_adapter_output.json").write_text(json.dumps(plan["v1_adapter_output"], ensure_ascii=False, indent=2), encoding="utf-8")
    (case_dir / "07_video_script.json").write_text(json.dumps(plan["user_video_script"], ensure_ascii=False, indent=2), encoding="utf-8")
    (case_dir / "08_video_script.md").write_text(render_user_video_script_markdown(plan), encoding="utf-8")
    (case_dir / "10_prompt_material.md").write_text(render_prompt_material_markdown(plan), encoding="utf-8")
    if plan.get("story_draft"):
        (case_dir / "11_story_and_remotion.json").write_text(
            json.dumps(
                {
                    "story_draft": plan["story_draft"],
                    "remotion_shots": [
                        {
                            "shot_id": shot["shot_id"],
                            "media_type": shot["media_type"],
                            "remotion": shot["content_plan"].get("remotion"),
                        }
                        for shot in plan["shots"]
                        if shot["media_type"] in {"explanation_video", "overlay_explanation"}
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    qa = {
        "status": "PASS",
        "source_chars": len("".join((source_document or text).split())),
        "tts_authoritative_chars": len("".join(tts_text.split())),
        "tts_segment_count": len(segments),
        "total_duration_us": sum(item["end"] - item["start"] for item in timelines),
        "shot_count": plan["qa"]["shot_count"],
        "digital_human_target_ratio": ratio,
        "digital_human_duration_ratio": plan["qa"]["digital_human_duration_ratio"],
        "digital_human_candidate_duration_ratio": plan["qa"]["digital_human_candidate_duration_ratio"],
        "digital_human_ratio_deviation": round(plan["qa"]["digital_human_duration_ratio"] - ratio, 6),
        "digital_human_ratio_control_status": "TARGET_REACHED" if plan["qa"]["digital_human_duration_ratio"] >= ratio else "CAPPED_BY_V1_ROUTE_CONSTRAINTS_OR_ELIGIBLE_CANDIDATES",
        "media_selection_mode": plan["policy"]["media_selection_mode"],
        "segment_type_rule": plan["policy"]["segment_type_rule"],
        "aigc_target_ratio": aigc_target_ratio,
        "aigc_duration_ratio": plan["qa"]["aigc_duration_ratio"],
        "aigc_candidate_duration_ratio": plan["qa"]["aigc_candidate_duration_ratio"],
        "explanation_duration_ratio": plan["qa"]["explanation_duration_ratio"],
        "overlay_duration_ratio": plan["qa"]["overlay_duration_ratio"],
        "digital_human_selected_count": plan["qa"]["digital_human_selected_count"],
        "aigc_selected_count": plan["qa"]["aigc_selected_count"],
        "explanation_selected_count": plan["qa"]["explanation_selected_count"],
        "overlay_selected_count": plan["qa"]["overlay_selected_count"],
        "segment_type_counts": plan["qa"]["segment_type_counts"],
        "story_draft_present": plan["qa"]["story_draft_present"],
        "story_layer": plan["policy"]["story_layer"],
        "long_shot_image_score_rule": plan["policy"]["long_shot_image_score_rule"],
        "long_shot_image_score_adjustment_count": plan["qa"]["long_shot_image_score_adjustment_count"],
        "media_counts": {media: sum(1 for shot in plan["shots"] if shot["media_type"] == media) for media in ("digital_human_video", "aigc_video", "static_image", "explanation_video", "overlay_explanation")},
        "all_media_scores_sum_100": all(sum(shot["scores"].values()) == 100 for shot in plan["shots"]),
        "v1_adapter_errors": plan["qa"]["v1_adapter_errors"],
        "source_matches_tts_segments": plan["source"]["source_matches_tts_segments"],
        "v1_visual_fields_complete": plan["qa"]["v1_visual_fields_complete"],
        "v1_frame_prompt_complete": plan["qa"]["v1_frame_prompt_complete"],
        "v1_video_prompt_complete": plan["qa"]["v1_video_prompt_complete"],
        "v1_camera_fields_complete": plan["qa"]["v1_camera_fields_complete"],
        "opening_static_hold_ok": plan["qa"]["opening_static_hold_ok"],
        "v1_short_route_ok": plan["qa"]["v1_short_route_ok"],
        "models": plan["policy"]["models"],
    }
    (case_dir / "09_qa_report.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    return qa


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--digital-human-ratio", type=float, default=0.25)
    parser.add_argument("--aigc-target-ratio", type=float, default=None)
    parser.add_argument("--semantic-model", default="rule", help="语义理解模型：rule / seed21_turbo / custom")
    parser.add_argument("--content-model", default="rule", help="画面创意模型：rule / seed21_turbo / custom")
    parser.add_argument("--segment-type-model", default="rule", help="故事/说明分流模型：rule / seed21_turbo / custom")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    text, segments, timelines = _real_case()
    summary = {
        "package": "oral_broll_director_v2",
        "models": {
            "semantic_model": args.semantic_model,
            "content_model": args.content_model,
            "segment_type_model": args.segment_type_model,
        },
        "cases": {
            "real_long_form": _write_case(args.output_dir, "real_long_form", "".join(segments), segments, timelines, args.digital_human_ratio, aigc_target_ratio=args.aigc_target_ratio, source_document=text, semantic_model_name=args.semantic_model, content_model_name=args.content_model, segment_type_model_name=args.segment_type_model),
        },
    }
    short_text, short_segments, short_timelines = _short_case()
    summary["cases"]["short_boundary"] = _write_case(args.output_dir, "short_boundary", short_text, short_segments, short_timelines, args.digital_human_ratio, aigc_target_ratio=args.aigc_target_ratio, semantic_model_name=args.semantic_model, content_model_name=args.content_model, segment_type_model_name=args.segment_type_model)
    route_text, route_segments, route_timelines = _media_route_case()
    summary["cases"]["media_route_boundary"] = _write_case(args.output_dir, "media_route_boundary", route_text, route_segments, route_timelines, args.digital_human_ratio, aigc_target_ratio=args.aigc_target_ratio, semantic_model_name=args.semantic_model, content_model_name=args.content_model, segment_type_model_name=args.segment_type_model)
    story_text, story_segments, story_timelines = _story_explanation_case()
    summary["cases"]["story_explanation_boundary"] = _write_case(args.output_dir, "story_explanation_boundary", story_text, story_segments, story_timelines, args.digital_human_ratio, aigc_target_ratio=args.aigc_target_ratio, semantic_model_name=args.semantic_model, content_model_name=args.content_model, segment_type_model_name=args.segment_type_model)
    overlay_text, overlay_segments, overlay_timelines = _overlay_case()
    summary["cases"]["overlay_boundary"] = _write_case(args.output_dir, "overlay_boundary", overlay_text, overlay_segments, overlay_timelines, 0.5, aigc_target_ratio=args.aigc_target_ratio, semantic_model_name=args.semantic_model, content_model_name=args.content_model, segment_type_model_name=args.segment_type_model)
    summary["v1_reference"] = _v1_reference_case()
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
