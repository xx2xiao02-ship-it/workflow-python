from __future__ import annotations

import json

from workflow_1256.first_frame_grid import build_first_frame_grid_plan
from workflow_1256.first_frame_prompt_reviewer import (
    ExistingModelReviewerAdapter,
    ImagePromptReviewerConfig,
    LightweightImagePromptReviewer,
    build_reviewer_input,
    detect_deterministic_risks,
)


def _cell(prompt: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "position": "右中",
        "scene": "居民楼路边",
        "characters": ["陈岭", "父亲"],
        "character_count": 2,
        "key_props": ["舞鞋"],
        "shot_size": "远景",
        "camera_angle": "高机位",
        "composition": "斜向纵深",
        "frame_intent": "陈岭看向父亲",
        "dominant_action": "陈岭停在父亲身侧",
        "lighting": "侧向主光",
        "compiled_first_frame_prompt": prompt,
    }
    value.update(overrides)
    return value


def _result(status: str, prompt: str | None = None, *, code: str | None = None) -> dict[str, object]:
    return {
        "status": status,
        "risk_score": 0.9 if status != "PASS" else 0.02,
        "issues": [] if code is None else [{"code": code, "severity": "HIGH", "reason": "test"}],
        "repaired_prompt": prompt,
    }


def test_normal_prompt_passes_byte_for_byte_without_runner() -> None:
    prompt = "居民楼路边。陈岭停在父亲身侧。画面采用远景，以高机位视角拍摄，构图突出斜向纵深。光线保持侧向主光。"
    result = LightweightImagePromptReviewer().review_cell(_cell(prompt))
    assert result["review_status"] == "PASS"
    assert result["review_requested"] is False
    assert result["final_first_frame_prompt"] == prompt
    assert result["reviewed_first_frame_prompt"] is None


def test_off_mode_skips_without_model_call() -> None:
    calls: list[object] = []
    reviewer = LightweightImagePromptReviewer(
        config=ImagePromptReviewerConfig(mode="off"),
        runner=lambda payload: calls.append(payload) or _result("PASS"),
    )
    prompt = "居民楼路边。陈岭停在父亲身侧。"
    result = reviewer.review_cell(_cell(prompt))
    assert result["review_status"] == "SKIPPED"
    assert result["review_requested"] is False
    assert calls == []


def test_existing_provider_adapter_is_reused_without_hardcoded_credentials() -> None:
    calls: list[dict[str, object]] = []

    class FakeTransport:
        def run_plugin(self, **payload: object) -> dict[str, str]:
            calls.append(payload)
            return {"output_5_5": '{"status":"PASS","risk_score":0.01,"issues":[],"repaired_prompt":null}'}

    adapter = ExistingModelReviewerAdapter(FakeTransport())
    result = adapter.review({"system_prompt": "review-system", "input": {"scene": "菜场"}})
    assert json.loads(result)["status"] == "PASS"
    assert calls[0]["system_prompt"] == "review-system"
    assert json.loads(str(calls[0]["prompt"]))["scene"] == "菜场"
    assert calls[0]["image_urls"] == []


def test_injected_cases_get_expected_risk_codes() -> None:
    cases = [
        (_cell("菜场摊位前。周姐推招牌，陈岭摸铁盒。", scene="菜场摊位前", characters=["周姐"], character_count=1, key_props=["招牌"]), "SUBJECT_CONFLICT"),
        (_cell("居民楼路边。坐在室内餐桌旁。", characters=["陈岭"], character_count=1), "SCENE_CONFLICT"),
        (_cell("居民楼路边。陈岭和父亲看向周姐。", characters=["陈岭", "父亲"], character_count=2), "SUBJECT_CONFLICT"),
        (_cell("深夜家中桌前。陈岭拿着舞鞋和现金信封。", scene="深夜家中桌前", characters=["陈岭"], character_count=1), "PROP_CONFLICT"),
        (_cell("菜场。按住铁盒，同时站起，然后转身走向门口。", scene="菜场", characters=["陈岭"], character_count=1, key_props=["铁盒"]), "MULTI_ACTION_SEMANTIC_CONFLICT"),
    ]
    for cell, expected in cases:
        _, issues = detect_deterministic_risks(build_reviewer_input(cell))
        assert expected in {item["code"] for item in issues}


def test_all_mode_accepts_minimal_repair_and_keeps_immutable_facts() -> None:
    before = "居民楼路边。陈岭停在父亲身侧。画面采用远景，以高机位视角拍摄，构图突出斜向纵深。光线保持侧向主光。"
    after = "居民楼路边。陈岭停在父亲身侧。画面采用远景，以高机位视角拍摄，构图突出斜向纵深。光线保持侧向主光。"
    calls: list[dict[str, object]] = []

    def runner(payload: dict[str, object]) -> dict[str, object]:
        calls.append(payload)
        return _result("REPAIRED", after, code="TEMPORAL_SEQUENCE_RESIDUE")

    reviewer = LightweightImagePromptReviewer(
        config=ImagePromptReviewerConfig(mode="ALL"), runner=runner
    )
    result = reviewer.review_cell(_cell(before))
    assert len(calls) == 1
    assert result["review_status"] == "REPAIRED"
    assert result["final_first_frame_prompt"] == after
    assert result["reviewed_first_frame_prompt"] == after
    assert json.loads(result["review_issues_json"])[0]["code"] == "TEMPORAL_SEQUENCE_RESIDUE"


def test_immutable_change_is_rejected_and_falls_back() -> None:
    before = "居民楼路边。陈岭停在父亲身侧。画面采用远景，以高机位视角拍摄，构图突出斜向纵深。光线保持侧向主光。"
    changed = "医院。周姐站在门口。画面采用近景，以低机位视角拍摄，构图突出正面。光线保持顶光。"
    reviewer = LightweightImagePromptReviewer(
        config=ImagePromptReviewerConfig(mode="ALL"),
        runner=lambda _payload: _result("REPAIRED", changed, code="SCENE_CONFLICT"),
    )
    result = reviewer.review_cell(_cell(before))
    codes = {item["code"] for item in json.loads(result["review_issues_json"])}
    assert result["review_status"] == "NEEDS_REVIEW"
    assert result["final_first_frame_prompt"] == before
    assert "IMMUTABLE_FACT_VIOLATION" in codes


def test_change_budget_and_provider_failure_are_non_blocking() -> None:
    before = "居民楼路边。陈岭停在父亲身侧。画面采用远景，以高机位视角拍摄，构图突出斜向纵深。光线保持侧向主光。"
    reviewer = LightweightImagePromptReviewer(
        config=ImagePromptReviewerConfig(mode="ALL", max_changed_ratio=0.2),
        runner=lambda _payload: _result("REPAIRED", "完全重写成另一段无关内容。", code="INTERNAL_LOGIC_CONFLICT"),
    )
    result = reviewer.review_cell(_cell(before))
    assert result["review_status"] == "NEEDS_REVIEW"
    assert result["final_first_frame_prompt"] == before

    failed = LightweightImagePromptReviewer(
        config=ImagePromptReviewerConfig(mode="ALL"),
        runner=lambda _payload: (_ for _ in ()).throw(RuntimeError("provider down")),
    ).review_cell(_cell("居民楼路边。陈岭停在父亲身侧。"))
    assert failed["review_status"] == "ERROR"
    assert failed["final_first_frame_prompt"] == "居民楼路边。陈岭停在父亲身侧。"


def test_task_a_regression_is_marked_without_reviewer_call() -> None:
    calls: list[object] = []
    reviewer = LightweightImagePromptReviewer(
        config=ImagePromptReviewerConfig(mode="ALL"),
        runner=lambda payload: calls.append(payload) or _result("PASS"),
    )
    result = reviewer.review_cell(_cell("居民楼路边。scene_02 中出现自行车/街道场景。"))
    codes = {item["code"] for item in json.loads(result["review_issues_json"])}
    assert result["review_status"] == "NEEDS_REVIEW"
    assert "SYSTEM_PROMPT_BUILD_ERROR" in codes
    assert calls == []


def test_reviewer_cache_uses_compiled_prompt_and_ground_truth(tmp_path) -> None:
    calls: list[object] = []
    cache_path = tmp_path / "review-cache.json"
    reviewer = LightweightImagePromptReviewer(
        config=ImagePromptReviewerConfig(mode="ALL", cache_path=cache_path),
        runner=lambda payload: calls.append(payload) or _result("PASS"),
    )
    cell = _cell("居民楼路边。陈岭停在父亲身侧。")
    first = reviewer.review_cell(cell)
    second = reviewer.review_cell(cell)
    assert first["review_status"] == second["review_status"] == "PASS"
    assert len(calls) == 1
    assert second["cache_hit"] is True
    assert cache_path.is_file()


def test_grid_plan_reviews_each_cell_and_preserves_source_prompt() -> None:
    source = "【场景】菜场【主体与动作】周姐推招牌\n【镜头语言】景别=全景；视角=平视；构图=主体明确"
    plan = build_first_frame_grid_plan(
        [{"shot_id": "s1", "compiled_first_frame_prompt": source, "characters": ["周姐"], "character_count": 1, "key_props": ["招牌"]}],
        aspect_ratio="16:9",
    )
    cell = plan["batches"][0]["cells"][0]
    assert cell["compiled_first_frame_prompt"] == source
    assert cell["review_status"] == "PASS"
    assert cell["final_first_frame_prompt"] == cell["compiled_cell_prompt"]
    assert plan["review_summary"]["total_cells"] == 1
