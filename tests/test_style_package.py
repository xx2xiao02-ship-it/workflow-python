import json

import pytest

from workflow_1256.style_package import (
    StylePackageError,
    _json_from_model,
    build_content_analysis_prompts,
    build_case_rewrite_prompts,
    build_copy_review_prompts,
    build_style_distillation_prompts,
    build_style_adaptation_prompts,
    compile_runtime_style_profile,
    distill_style_package,
    extract_expression_habit_library,
    normalize_account_knowledge_context,
    normalize_creative_brief,
    rewrite_case_copy,
    rewrite_case_copy_serial,
)


def test_rewrite_prompts_include_bounded_account_knowledge_context():
    context = {
        "knowledge_status": "REVIEW_PENDING",
        "distillation_status": "READY_FOR_REVIEW",
        "sample_count": 78,
        "evidence_work_ids": ["work-1"],
        "source_policy": "账号 DNA 仅提供抽象线索",
        "notes": {"Writing-DNA.md": {"layer": "L1-L6", "content": {"writing_dna": "先结论后证据"}}},
        "private_field": "must be dropped",
    }
    normalized = normalize_account_knowledge_context(context)
    assert "private_field" not in normalized
    _system, user = build_content_analysis_prompts(
        case_item={"transcript": "足量案例文字" * 30, "source_url": "https://example.com/case"},
        rewrite_goal="重新表达",
        publish_format="知识口播短视频",
        target_duration="60 秒",
        account_knowledge_context=context,
    )
    payload = json.loads(user)
    assert payload["account_knowledge_context"]["sample_count"] == 78
    assert payload["account_knowledge_context"]["notes"]["Writing-DNA.md"]["content"] == {"writing_dna": "先结论后证据"}


def _samples(count=30):
    return [
        {"source_url": f"https://example.com/{index}", "title": f"样本 {index}", "transcript": "这是足量的公开字幕文本。" * 12}
        for index in range(count)
    ]


def test_distill_style_package_requires_minimum_samples():
    with pytest.raises(StylePackageError, match="最低门槛"):
        distill_style_package(
            creator_url="https://space.bilibili.com/1", source_items=_samples(29), transport=lambda *_: "{}"
        )


def test_distill_style_package_normalizes_model_json():
    response = {
        "profile_name": "高密度观点口播",
        "overview": "先提出反常识问题，再给出清晰论证。",
        "hook_patterns": ["反差提问"], "structure_patterns": ["问题-证据-结论"],
        "rhythm_rules": ["短句推进"], "voice_rules": ["口语但不夸张"],
        "rhetorical_devices": ["数字对比"], "avoid_rules": ["不照抄样本"],
        "originality_guardrails": ["不得复用原句"], "quality_checklist": ["事实可追溯"],
    }
    profile, samples = distill_style_package(
        creator_url="https://space.bilibili.com/1", source_items=_samples(), transport=lambda *_: json.dumps(response)
    )
    assert profile["profile_name"] == "高密度观点口播"
    assert "writing_dna" in profile
    assert "language_dna" in profile["writing_dna"]
    assert "generation_protocol" in profile["writing_dna"]
    assert len(samples) == 30


def test_distill_style_package_allows_available_samples_below_reference_target():
    response = {
        "profile_name": "部分样本风格",
        "overview": "使用现有公开样本提炼可复用规则。",
        "hook_patterns": ["反差提问"], "structure_patterns": ["问题-结论"],
        "rhythm_rules": ["短句推进"], "voice_rules": ["口语化"],
        "rhetorical_devices": ["对比"], "avoid_rules": ["不照抄"],
        "originality_guardrails": ["保持原创"], "quality_checklist": ["核验事实"],
    }
    profile, samples = distill_style_package(
        creator_url="https://space.bilibili.com/1",
        source_items=_samples(16),
        minimum_samples=30,
        allow_partial_samples=True,
        transport=lambda *_: json.dumps(response),
    )

    assert profile["profile_name"] == "部分样本风格"
    assert len(samples) == 16


def test_distill_style_package_falls_back_to_verified_creator_name():
    response = {
        "hook_patterns": ["反差提问"], "structure_patterns": ["问题-结论"],
        "rhythm_rules": ["短句推进"], "voice_rules": ["口语化"],
        "rhetorical_devices": ["对比"], "avoid_rules": ["不照抄"],
        "originality_guardrails": ["保持原创"], "quality_checklist": ["核验事实"],
    }
    profile, _samples_result = distill_style_package(
        creator_url="https://space.bilibili.com/1",
        source_items=_samples(16),
        minimum_samples=30,
        allow_partial_samples=True,
        fallback_profile_name="已核验博主",
        fallback_overview="基于已采集样本提炼的可复用表达规则。",
        transport=lambda *_: json.dumps(response),
    )

    assert profile["profile_name"] == "已核验博主"
    assert profile["overview"] == "基于已采集样本提炼的可复用表达规则。"


def test_distill_style_package_normalizes_model_rule_shapes():
    response = {
        "hook_patterns": {"patterns": [{"description": "先抛出具体问题"}]},
        "structure_patterns": "问题—证据—结论",
        "rhythm_rules": [{"rule": "短句推进"}],
        "voice_rules": ["口语化"],
        "rhetorical_devices": [{"pattern": "对比"}],
        "avoid_rules": {"items": ["不照抄原句"]},
        "originality_guardrails": "重新组织事实",
        "quality_checklist": [{"text": "核验事实"}],
    }
    profile, _samples_result = distill_style_package(
        creator_url="https://space.bilibili.com/1",
        source_items=_samples(16),
        minimum_samples=30,
        allow_partial_samples=True,
        fallback_profile_name="已核验博主",
        fallback_overview="基于已采集样本提炼的可复用表达规则。",
        transport=lambda *_: json.dumps(response, ensure_ascii=False),
    )

    assert profile["hook_patterns"] == ["先抛出具体问题"]
    assert profile["structure_patterns"] == ["问题—证据—结论"]
    assert profile["rhythm_rules"] == ["短句推进"]
    assert profile["quality_checklist"] == ["核验事实"]
    assert profile["normalization_warnings"]


def test_expression_habit_library_is_preserved_as_abstract_runtime_rules():
    response = {
        "profile_name": "表达习惯测试",
        "overview": "抽象表达习惯",
        "hook_patterns": ["具体提问"], "structure_patterns": ["问题—证据—结论"],
        "rhythm_rules": ["短句推进"], "voice_rules": ["口语化"],
        "rhetorical_devices": ["类比"], "avoid_rules": ["不复用独特口头禅"],
        "originality_guardrails": ["重新组织事实"], "quality_checklist": ["核验事实"],
        "expression_habit_library": {
            "habits": [{
                "habit_type": "歇后语/俗语化类比",
                "pattern": "在解释抽象观点后用大众熟悉的生活类比落地",
                "purpose": "降低理解门槛",
                "placement": "解释段末",
                "frequency": "低频",
                "when_to_use": "主题存在抽象概念且有自然类比时",
                "constraints": "每篇最多一次",
                "quotes": ["不应保存的原句"],
            }],
            "selection_policy": ["自然匹配才使用"],
        },
    }
    profile, _samples_result = distill_style_package(
        creator_url="https://example.com/creator",
        source_items=_samples(),
        transport=lambda *_: json.dumps(response, ensure_ascii=False),
    )

    habit = profile["expression_habit_library"]["habits"][0]
    assert habit["category"] == "歇后语/俗语化类比"
    assert habit["usage_pattern"] == "在解释抽象观点后用大众熟悉的生活类比落地"
    assert "quotes" not in habit
    runtime = compile_runtime_style_profile(profile, publish_format="知识口播")
    assert runtime["expression_habit_library"]["habits"][0]["function"] == "降低理解门槛"
    system, _user = build_case_rewrite_prompts(
        case_item={"source_url": "https://example.com/case", "title": "案例", "transcript": "案例字幕内容。" * 15},
        style_profile=profile,
        rewrite_goal="重新表达",
        publish_format="知识口播",
        target_duration="60秒",
    )
    assert "在解释抽象观点后用大众熟悉的生活类比落地" in system


def test_empty_model_habit_library_is_filled_from_repeated_transcript_signals():
    samples = [
        {
            "source_url": f"https://example.com/habit-{index}",
            "title": f"表达习惯样本 {index}",
            "transcript": "为什么会这样？比如把复杂问题想成日常选择。但是关键不在表面，所以最后要回到事实。你先别急着下结论。" * 2,
        }
        for index in range(16)
    ]
    response = {
        "profile_name": "空库补齐测试",
        "overview": "基于样本提炼规则",
        "hook_patterns": ["问题开场"],
        "structure_patterns": ["问题—解释—结论"],
        "rhythm_rules": ["短句推进"],
        "voice_rules": ["口语化"],
        "rhetorical_devices": ["类比"],
        "avoid_rules": ["不照抄"],
        "originality_guardrails": ["保持原创"],
        "quality_checklist": ["核验事实"],
        "expression_habit_library": {"habits": []},
    }

    profile, _samples_result = distill_style_package(
        creator_url="https://example.com/creator",
        source_items=samples,
        minimum_samples=30,
        allow_partial_samples=True,
        transport=lambda *_: json.dumps(response, ensure_ascii=False),
    )

    habits = profile["expression_habit_library"]["habits"]
    assert len(habits) >= 4
    assert {item["category"] for item in habits}.issuperset({"反问/问题开场", "生活类比/举例落地", "转折纠偏", "结论回扣/收束"})
    assert all("quotes" not in item for item in habits)
    assert any("模型返回为空或不足" in item for item in profile["normalization_warnings"])


def test_expression_habit_extractor_stays_empty_when_corpus_has_no_repeatable_signal():
    samples = [
        {
            "source_url": f"https://example.com/plain-{index}",
            "transcript": f"这是第 {index} 条独立的说明文本，内容只用于测试没有可重复表达信号的情况。这里的词语每条都不同。",
        }
        for index in range(16)
    ]
    library = extract_expression_habit_library(samples)

    assert library["habits"] == []


def test_distillation_prompt_requests_expression_habit_library_without_source_quotes():
    system, user = build_style_distillation_prompts(
        creator_url="https://space.bilibili.com/1", samples=_samples(), minimum_samples=30
    )
    assert "表达习惯库" in system
    assert "expression_habit_library" in user
    assert "original_phrases" in system


def test_creative_brief_is_normalized_and_capped_before_prompt_injection():
    brief = normalize_creative_brief({
        "personalized_slogan": "S" * 500,
        "creative_direction": "D" * 1_000,
    })

    assert len(brief["personalized_slogan"]) == 120
    assert len(brief["creative_direction"]) == 500


def test_serial_rewrite_separates_content_analysis_from_style_adaptation_and_review():
    profile = {
        "profile_name": "测试风格", "overview": "说明", "hook_patterns": ["提问"],
        "structure_patterns": ["问题-结论"], "rhythm_rules": ["短句"], "voice_rules": ["口语"],
        "rhetorical_devices": ["类比"], "avoid_rules": ["不复用独特口头禅"],
        "originality_guardrails": ["不照抄"], "quality_checklist": ["核验事实"],
        "expression_habit_library": {"habits": [{
            "category": "俗语化类比",
            "usage_pattern": "解释抽象观点后用生活类比落地",
            "function": "降低理解门槛",
            "position": "解释段末",
            "formula": "抽象观点 → 生活场景 → 通俗收束",
            "generic_example": "把复杂问题讲成日常选择",
        }]},
    }
    content_response = {
        "content_blueprint": {
            "topic": "效率",
            "core_claim": "效率差距来自系统设计，而不只是个人努力",
            "audience_promise": "理解效率差距的原因",
            "fact_units": [{"id": "fact_1", "statement": "案例中的事实", "source_support": "原文证据"}],
            "argument_chain": [{"id": "step_1", "role": "conclusion", "point": "回到核心观点", "fact_ids": ["fact_1"]}],
            "structure": [{"id": "part_1", "part": "hook", "purpose": "提出反差", "key_points": ["效率问题"]}],
            "must_preserve": ["不改变核心观点"],
            "must_avoid": [],
        }
    }
    style_response = _generation_response("风格适配后的初稿")
    style_response["style_application"] = [{
        "target": "body", "habit_category": "俗语化类比", "transformation": "把抽象概念落到生活场景",
    }]
    prompts = []
    progress = []

    def capture(label, response):
        def transport(system, user):
            prompts.append((label, system, user))
            return json.dumps(response, ensure_ascii=False)
        return transport

    result = rewrite_case_copy_serial(
        case_item={"source_url": "https://example.com/case", "title": "案例", "transcript": "案例字幕内容。" * 15},
        style_profile=profile,
        rewrite_goal="重新表达",
        publish_format="知识口播",
        target_duration="60秒",
        creative_brief={
            "personalized_slogan": "让普通人也能看懂复杂问题",
            "creative_direction": "主要针对在家创业女性，强调低成本试错和第一步行动",
        },
        content_transport=capture("content", content_response),
        style_transport=capture("style", style_response),
        review_transport=capture("review", _review_response()),
        progress_reporter=lambda stage, value, message: progress.append((stage, value, message)),
    )

    assert [label for label, _system, _user in prompts] == ["content", "style", "review"]
    for _label, _system, user in prompts:
        brief = json.loads(user)["creative_brief"]
        assert brief["personalized_slogan"] == "让普通人也能看懂复杂问题"
        assert "在家创业女性" in brief["creative_direction"]
    assert "expression_habit_library" not in prompts[0][1]
    assert "核心观点" in prompts[1][2]
    assert "generic_example" in prompts[1][1]
    assert "content_blueprint" in prompts[2][2]
    assert result["content_blueprint"]["core_claim"].startswith("效率差距")
    assert result["creative_brief"]["personalized_slogan"] == "让普通人也能看懂复杂问题"
    assert result["style_application"][0]["habit_category"] == "俗语化类比"
    assert result["pipeline"]["content_analysis"] == "COMPLETED"
    assert result["pipeline"]["style_adaptation"] == "COMPLETED"
    assert result["pipeline"]["review"] == "PASS"
    assert [item[0] for item in progress] == ["content_analysis", "style_adaptation", "reviewing", "reviewed"]


def test_distill_style_package_fills_missing_rule_for_review():
    response = {
        "profile_name": "部分规则风格",
        "overview": "使用现有公开样本提炼可复用规则。",
        "structure_patterns": ["问题—证据—结论"],
        "rhythm_rules": ["短句推进"],
        "voice_rules": ["口语化"],
        "rhetorical_devices": ["对比"],
        "avoid_rules": ["不照抄原句"],
        "originality_guardrails": ["重新组织事实"],
        "quality_checklist": ["核验事实"],
    }
    profile, _samples_result = distill_style_package(
        creator_url="https://space.bilibili.com/1",
        source_items=_samples(16),
        minimum_samples=30,
        allow_partial_samples=True,
        transport=lambda *_: json.dumps(response, ensure_ascii=False),
    )

    assert profile["hook_patterns"] == ["用具体问题、反差观察或明确结论开场，避免空泛口号。"]
    assert any("hook_patterns" in item and "默认值" in item for item in profile["normalization_warnings"])


def test_distillation_prompt_requires_deep_writing_dna_layers():
    system, user = build_style_distillation_prompts(
        creator_url="https://space.bilibili.com/1", samples=_samples(), minimum_samples=30
    )
    assert "L1" in system and "L6" in system
    assert "minimum_structure_templates" in user
    assert "cognitive_framework" in user


def test_rewrite_prompt_includes_writing_dna_generation_protocol():
    profile = {
        "profile_name": "测试风格", "overview": "说明", "hook_patterns": ["提问"],
        "structure_patterns": ["问题-结论"], "rhythm_rules": ["短句"], "voice_rules": ["口语"],
        "rhetorical_devices": ["对比"], "avoid_rules": ["不复刻"],
        "originality_guardrails": ["不照抄"], "quality_checklist": ["核验事实"],
        "writing_dna": {"generation_protocol": {"step_by_step_method": ["先定问题", "再列证据"]}},
    }
    system, user = build_case_rewrite_prompts(
        case_item={"source_url": "https://example.com/case", "title": "案例", "transcript": "案例字幕内容。" * 15},
        style_profile=profile, rewrite_goal="重新表达", publish_format="知识口播", target_duration="60-90 秒",
    )
    assert "generation_protocol" in system
    assert "先定问题" in system
    assert '"style_profile"' not in user
    assert "案例字幕内容" in user


def _generation_response(copy="初稿文案"):
    return {
        "source_summary": "案例讨论效率",
        "creation_outline": {"hook_3s": "为什么效率差距更大了？", "acts": ["提出问题"], "ending": "回到行动"},
        "rewritten_copy": copy,
        "risk_report": {"facts_to_verify": [], "similarity_guardrails": ["不复用原句"], "style_rules_applied": ["短句"]},
    }


def _review_response(status="PASS", revised_copy=""):
    return {
        "status": status,
        "audit_summary": "结构和事实边界可交付",
        "style_score": 8,
        "fact_risks": [],
        "similarity_risks": [],
        "structure_issues": [],
        "language_issues": [],
        "revision_instructions": ["加强开头"] if status == "REVISE" else [],
        "revised_copy": revised_copy,
        "dna_sections_used": ["language_dna", "structure_templates"],
    }


def test_review_prompt_receives_source_and_draft_but_not_full_profile():
    profile = {
        "profile_name": "测试风格", "overview": "说明", "hook_patterns": ["提问"],
        "structure_patterns": ["问题-结论"], "rhythm_rules": ["短句"], "voice_rules": ["口语"],
        "rhetorical_devices": ["对比"], "avoid_rules": ["不复刻"],
        "originality_guardrails": ["不照抄"], "quality_checklist": ["核验事实"],
    }
    system, user = build_copy_review_prompts(
        case_item={"source_url": "https://example.com/case", "title": "案例", "transcript": "原始案例文本。" * 15},
        style_profile=profile,
        draft_result={**_generation_response(), "draft_copy": "初稿文案"},
        rewrite_goal="重新表达", publish_format="知识口播", target_duration="60秒",
    )
    assert "PASS、REVISE 或 BLOCK" in system
    assert "原始案例文本" in user and "初稿文案" in user
    assert '"style_profile"' not in user


def test_rewrite_case_copy_runs_generation_then_review_and_passes():
    profile = {
        "profile_name": "测试风格", "overview": "说明", "hook_patterns": ["提问"],
        "structure_patterns": ["问题-结论"], "rhythm_rules": ["短句"], "voice_rules": ["口语"],
        "rhetorical_devices": ["对比"], "avoid_rules": ["不复刻"],
        "originality_guardrails": ["不照抄"], "quality_checklist": ["核验事实"],
    }
    responses = iter([json.dumps(_generation_response()), json.dumps(_review_response())])
    result = rewrite_case_copy(
        case_item={"source_url": "https://example.com/case", "title": "案例", "transcript": "案例字幕内容。" * 15},
        style_profile=profile, rewrite_goal="重新表达", publish_format="知识口播", target_duration="60秒",
        transport=lambda *_: next(responses), review_transport=lambda *_: next(responses),
    )
    assert result["draft_copy"] == "初稿文案"
    assert result["rewritten_copy"] == "初稿文案"
    assert result["audit_report"]["status"] == "PASS"
    assert result["pipeline"]["gate"] == "READY_FOR_APPROVAL"


def test_rewrite_case_copy_applies_reviewer_revision():
    profile = {
        "profile_name": "测试风格", "overview": "说明", "hook_patterns": ["提问"],
        "structure_patterns": ["问题-结论"], "rhythm_rules": ["短句"], "voice_rules": ["口语"],
        "rhetorical_devices": ["对比"], "avoid_rules": ["不复刻"],
        "originality_guardrails": ["不照抄"], "quality_checklist": ["核验事实"],
    }
    responses = iter([json.dumps(_generation_response()), json.dumps(_review_response("REVISE", "审核修改稿"))])
    result = rewrite_case_copy(
        case_item={"source_url": "https://example.com/case", "title": "案例", "transcript": "案例字幕内容。" * 15},
        style_profile=profile, rewrite_goal="重新表达", publish_format="知识口播", target_duration="60秒",
        transport=lambda *_: next(responses), review_transport=lambda *_: next(responses),
    )
    assert result["draft_copy"] == "初稿文案"
    assert result["rewritten_copy"] == "审核修改稿"
    assert result["pipeline"]["revision_applied"] is True


def test_rewrite_case_copy_blocks_without_approved_gate():
    profile = {
        "profile_name": "测试风格", "overview": "说明", "hook_patterns": ["提问"],
        "structure_patterns": ["问题-结论"], "rhythm_rules": ["短句"], "voice_rules": ["口语"],
        "rhetorical_devices": ["对比"], "avoid_rules": ["不复刻"],
        "originality_guardrails": ["不照抄"], "quality_checklist": ["核验事实"],
    }
    responses = iter([json.dumps(_generation_response()), json.dumps(_review_response("BLOCK"))])
    result = rewrite_case_copy(
        case_item={"source_url": "https://example.com/case", "title": "案例", "transcript": "案例字幕内容。" * 15},
        style_profile=profile, rewrite_goal="重新表达", publish_format="知识口播", target_duration="60秒",
        transport=lambda *_: next(responses), review_transport=lambda *_: next(responses),
    )
    assert result["pipeline"]["gate"] == "BLOCKED"
    assert "approved_copy" not in result


def test_rewrite_case_copy_returns_auditable_sections():
    profile = {
        "profile_name": "高密度观点口播", "overview": "清晰论证", "hook_patterns": ["提问"],
        "structure_patterns": ["问题-结论"], "rhythm_rules": ["短句"], "voice_rules": ["口语"],
        "rhetorical_devices": ["对比"], "avoid_rules": ["不复刻"], "originality_guardrails": ["不照抄"],
        "quality_checklist": ["核验事实"],
    }
    response = {
        "source_summary": "案例讨论效率", "creation_outline": {"hook_3s": "为什么效率差距更大了？", "acts": ["提出问题"], "ending": "回到行动"},
        "rewritten_copy": "这是一段全新表达的二创文案。", "risk_report": {"facts_to_verify": [], "similarity_guardrails": ["不复用原句"], "style_rules_applied": ["短句"]},
    }
    result = rewrite_case_copy(
        case_item={"source_url": "https://example.com/case", "title": "案例", "transcript": "案例字幕内容。" * 15},
        style_profile=profile, rewrite_goal="重新表达", publish_format="知识口播", target_duration="60-90 秒",
        transport=lambda *_: json.dumps(response),
    )
    assert result["creation_outline"]["hook_3s"]
    assert result["risk_report"]["similarity_guardrails"]


def test_model_json_parser_recovers_object_wrapped_in_explanation():
    response = "已按要求整理如下：\n```json\n{\"source_summary\": \"案例摘要\"}\n```\n请审核。"
    assert _json_from_model(response) == {"source_summary": "案例摘要"}


def test_model_json_parser_repairs_raw_line_break_inside_json_string():
    response = '{"source_summary":"摘要","creation_outline":{"hook_3s":"钩子"},"rewritten_copy":"第一句\n第二句"}'
    parsed = _json_from_model(response)
    assert parsed["source_summary"] == "摘要"
    assert parsed["rewritten_copy"] == "第一句\n第二句"
