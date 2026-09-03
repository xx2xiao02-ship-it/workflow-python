from __future__ import annotations

import copy

import pytest

from workflow_1256.governance.media_route_scoring_v3 import (
    CLASSIFICATION_FEATURE_KEYS,
    MediaRouteScoringV3Error,
    apply_v3_route_lock_to_governance,
    build_director_media_route_lock_v3,
    normalize_v3_dynamic_params,
    validate_classification_features,
)


def _feature_record(shot_id: str, values: dict[str, int]) -> dict:
    features = {key: int(values.get(key, 0)) for key in CLASSIFICATION_FEATURE_KEYS}
    return {
        "shot_id": shot_id,
        **features,
        "evidence": {key: f"{key} 的固定测试证据" for key in CLASSIFICATION_FEATURE_KEYS},
    }


def _v3_inputs() -> tuple[dict, dict]:
    texts = [
        "一间安静的工厂在清晨亮灯。",
        "因为原料价格上升，所以产品利润下降。",
        "与过去相比，企业必须把成本和收入分开看。",
        "结论是，利润改善来自持续执行。",
    ]
    shots = []
    contexts = []
    for index, text in enumerate(texts):
        start_us = index * 3_000_000
        end_us = start_us + 3_000_000
        production_spec = {"narration_text": text, "dynamic_level": "低"}
        if index == 1:
            production_spec["route_candidates"] = ["host"]
        shots.append({
            "shot_id": f"g01_s{index + 1:02d}",
            "group_id": "g01",
            "source_text": text,
            "clip_role": "信息说明",
            "production_spec": production_spec,
            "timeline": {"start_us": start_us, "end_us": end_us},
        })
        contexts.append({
            "shot_id": f"g01_s{index + 1:02d}",
            "timeline": {"start_us": start_us, "end_us": end_us},
            "shot_design_type": "叙事镜头",
            "narrative_job": "信息说明",
            "dynamic_level": "低",
            "visual_direction": {"camera_motion": "固定", "shot_size": "中景"},
            "classification_features": _feature_record(f"g01_s{index + 1:02d}", {
                "motion_need": 90 if index == 0 else 15,
                "temporal_dependency": 85 if index == 0 else 20,
                "static_completeness": 40 if index in (0, 1) else 88,
                "visual_evidence": 75,
                "relation_strength": 96 if index == 1 else 82 if index == 2 else 20,
                "information_density": 80 if index in (1, 2, 3) else 35,
                "presenter_value": 95 if index == 1 else 15,
                "production_feasibility": 82,
            }),
        })
    return (
        {"status": "DIRECTOR_LOCKED", "shots": shots},
        {"shot_contexts": contexts},
    )


def test_v3_lock_preserves_timeline_and_outputs_explanation_and_pip_contracts():
    director_lock, governance = _v3_inputs()
    original_timelines = [copy.deepcopy(shot["timeline"]) for shot in director_lock["shots"]]

    route_lock = build_director_media_route_lock_v3(
        director_lock,
        governance,
        {"digital_human_max_exposure_ratio": 1.0},
    )

    assert route_lock["schema_version"] == "director-media-route-lock-v3"
    assert [route["shot_class"] for route in route_lock["routes"]][0] == "aigc"
    mixed = route_lock["routes"][1]
    assert mixed["shot_class"] == "mixed_explanation"
    assert mixed["media_type"] == "mixed_explanation"
    assert mixed["render_plan"]["template_id"] == "causal_chain"
    assert mixed["render_plan"]["overlay_tracks"][0]["type"] == "digital_human_pip"
    mixed_pip_range = mixed["render_plan"]["overlay_tracks"][0]["pip_time_range"]
    assert mixed_pip_range["start_us"] == mixed["timeline"]["start_us"]
    assert mixed_pip_range["end_us"] == mixed["timeline"]["end_us"]
    assert mixed["required_assets"][-1]["asset"] == "digital_human_video"
    explanation = route_lock["routes"][2]
    assert explanation["shot_class"] == "explanation"
    assert explanation["render_plan"]["template_id"] == "contrast_split"
    assert [shot["timeline"] for shot in director_lock["shots"]] == original_timelines

    applied = apply_v3_route_lock_to_governance(governance, route_lock)
    assert applied["media_route_policy_version"] == "media_route_scoring_v3_1"
    assert applied["shot_contexts"][1]["shot_class"] == "mixed_explanation"
    assert applied["shot_contexts"][1]["render_plan"]["overlay_tracks"]


def test_v3_dynamic_params_reject_unknown_keys_and_keep_all_defaults():
    defaults = normalize_v3_dynamic_params(None)
    assert defaults["opening_video_bonus"] == 15
    assert defaults["static_streak_force"] == 4
    assert defaults["mixed_pip_exposure_ratio"] == 1.0

    with pytest.raises(MediaRouteScoringV3Error, match="不支持的 v3 动态参数"):
        normalize_v3_dynamic_params({"not_a_v3_rule": 1})

    with pytest.raises(MediaRouteScoringV3Error, match="不能小于"):
        normalize_v3_dynamic_params({"static_streak_warning": 5, "static_streak_force": 4})


def test_v31_formula_and_action_do_not_remove_image_eligibility():
    director_lock, governance = _v3_inputs()
    # 普通动作/运镜只能影响模型给出的特征，不能触发旧的 image 资格硬排除。
    director_lock["shots"][2]["source_text"] = "人物拿起文件后停在桌边。"
    governance["shot_contexts"][2]["visual_direction"]["camera_motion"] = "推近"
    route_lock = build_director_media_route_lock_v3(director_lock, governance)
    route = route_lock["routes"][2]
    features = route["classification_features"]
    expected_image = round(
        features["static_completeness"] * 0.35
        + features["visual_evidence"] * 0.25
        + (100 - features["temporal_dependency"]) * 0.20
        + features["information_density"] * 0.10
        + features["production_feasibility"] * 0.10,
        2,
    )
    assert route["candidate_scores"]["image"] == expected_image
    assert route["candidate_details"]["image"]["eligible"] is True
    assert route["score_policy_version"] == "media_route_scoring_v3_1"
    assert route["adjustments"] == []


def test_mixed_explanation_requires_stronger_presenter_need_than_explanation():
    director_lock, governance = _v3_inputs()
    features = governance["shot_contexts"][1]["classification_features"]
    features["relation_strength"] = 80
    features["presenter_value"] = 65

    route = build_director_media_route_lock_v3(director_lock, governance)["routes"][1]

    assert route["candidate_details"]["explanation"]["eligible"] is True
    assert route["candidate_details"]["mixed_explanation"]["eligible"] is False
    assert "主播价值达到 70" in route["candidate_details"]["mixed_explanation"]["eligibility_reason"]
    assert route["shot_class"] != "mixed_explanation"


def test_mixed_explanation_must_materially_beat_pure_explanation():
    director_lock, governance = _v3_inputs()
    governance["shot_contexts"][1]["classification_features"] = _feature_record(
        "g01_s02",
        {
            "motion_need": 60,
            "temporal_dependency": 70,
            "static_completeness": 75,
            "visual_evidence": 50,
            "relation_strength": 80,
            "information_density": 70,
            "presenter_value": 70,
            "production_feasibility": 80,
        },
    )

    route = build_director_media_route_lock_v3(director_lock, governance)["routes"][1]

    assert route["candidate_details"]["mixed_explanation"]["eligible"] is False
    assert "至少领先 5 分" in route["candidate_details"]["mixed_explanation"]["eligibility_reason"]
    assert route["shot_class"] == "explanation"


def test_v31_selection_is_independent_of_global_ratio_and_streak_parameters():
    director_lock, governance = _v3_inputs()
    baseline = build_director_media_route_lock_v3(director_lock, governance)
    changed = build_director_media_route_lock_v3(
        director_lock,
        governance,
        {
            "opening_video_bonus": 40,
            "opening_image_penalty": 0,
            "static_streak_warning": 1,
            "static_streak_force": 8,
            "static_streak_force_bonus": 40,
            "aigc_target_ratio": 0.10,
            "image_target_ratio": 0.70,
            "explanation_target_ratio": 0.20,
            "beam_width": 1,
        },
    )
    assert [item["shot_class"] for item in baseline["routes"]] == [item["shot_class"] for item in changed["routes"]]
    assert all(item["adjustments"] == [] for item in changed["routes"])
    assert changed["policy"]["global_distribution_adjustment"] is False
    assert changed["policy"]["static_streak_adjustment"] is False


def test_v31_non_opening_shot_under_three_seconds_is_forced_to_static_image():
    director_lock, governance = _v3_inputs()
    director_lock["shots"][1]["timeline"] = {"start_us": 3_000_000, "end_us": 5_900_000}
    governance["shot_contexts"][1]["timeline"] = {"start_us": 3_000_000, "end_us": 5_900_000}

    route = build_director_media_route_lock_v3(director_lock, governance)["routes"][1]

    assert route["media_type"] == "static_image"
    assert route["shot_class"] == "image"
    assert route["skip"]["skip_aigc_video"] is True
    assert "低于 3 秒" in route["media_reason"]


def test_v31_invalid_feature_contract_has_no_silent_fallback():
    director_lock, governance = _v3_inputs()
    del governance["shot_contexts"][1]["classification_features"]["evidence"]["motion_need"]
    with pytest.raises(MediaRouteScoringV3Error, match="FEATURE_CONTRACT_INVALID"):
        build_director_media_route_lock_v3(director_lock, governance)

    with pytest.raises(MediaRouteScoringV3Error, match="FEATURE_CONTRACT_INVALID"):
        validate_classification_features([{
            "shot_id": "g01_s01",
            "motion_need": 50.5,
            "temporal_dependency": 50,
            "static_completeness": 50,
            "visual_evidence": 50,
            "relation_strength": 50,
            "information_density": 50,
            "presenter_value": 50,
            "production_feasibility": 50,
            "evidence": {key: "证据" for key in CLASSIFICATION_FEATURE_KEYS},
        }])


def test_v31_tie_break_and_review_status_are_deterministic():
    director_lock, governance = _v3_inputs()
    equal = _feature_record("g01_s02", {key: 50 for key in CLASSIFICATION_FEATURE_KEYS})
    governance["shot_contexts"][1]["classification_features"] = equal
    route = build_director_media_route_lock_v3(director_lock, governance)["routes"][1]
    assert route["shot_class"] == "aigc"  # 完全同分按稳定制作顺序裁决
    assert route["confidence"] == "LOW"
    assert route["score_gap"] == 0.0

    low = _feature_record("g01_s02", {key: 10 for key in CLASSIFICATION_FEATURE_KEYS})
    governance["shot_contexts"][1]["classification_features"] = low
    low_route = build_director_media_route_lock_v3(director_lock, governance)["routes"][1]
    assert low_route["review_status"] == "NEEDS_REVIEW"
