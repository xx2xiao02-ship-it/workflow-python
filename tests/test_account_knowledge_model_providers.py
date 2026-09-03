from __future__ import annotations

import json
import os

import pytest

from workflow_1256.account_knowledge.model_providers import (
    AccountKnowledgeModelError,
    AccountKnowledgeModelProvider,
    apply_api_management_model_runtime,
    build_account_distillation_prompts,
    build_topic_synthesis_prompts,
    build_work_distillation_prompts,
    merge_topic_synthesis,
)
from workflow_1256.account_knowledge.contracts import KnowledgeContractError
from workflow_1256.topic_knowledge_v2 import EventCard, build_topic_plan


class _Transport:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return json.dumps(self.payload, ensure_ascii=False)


def _style_profile() -> dict:
    return {
        "profile_name": "测试风格",
        "overview": "克制、清晰的知识口播风格",
        "hook_patterns": ["先提出反常识问题"],
        "structure_patterns": ["问题→事实→解释→结论"],
        "rhythm_rules": ["短句推进，段落只承载一个观点"],
        "voice_rules": ["口语化但不夸张"],
        "rhetorical_devices": ["对比和类比"],
        "avoid_rules": ["不复制原句"],
        "originality_guardrails": ["只使用有来源支持的事实"],
        "quality_checklist": ["检查事实和原创性"],
        "expression_habit_library": {"habits": [], "selection_policy": ["自然使用"]},
    }


def _topic_plan() -> dict:
    event = EventCard(
        event_id="event-model-1",
        title="监管政策变化",
        verified_facts=["监管部门发布新规"],
        source_refs=[{"source_id": "news-1", "url": "https://news.example.com/1"}],
    )
    return build_topic_plan(
        event_card=event,
        target_account_id="creator_a",
        recommended_angle="从规则变化解释利益结构",
        author_lenses=[
            {
                "account_id": "creator_a",
                "angles": ["从规则变化解释利益结构"],
                "reasoning_patterns": ["事实→利益→影响"],
                "evidence_refs": [{"account_id": "creator_a", "work_id": "work-a"}],
            }
        ],
        evidence_refs=[
            {"account_id": "creator_a", "work_id": "work-a", "source_url": "https://news.example.com/work-a"}
        ],
        status="REVIEW_REQUIRED",
    )


def test_prompt_builders_keep_source_policy_and_schema() -> None:
    system, user = build_work_distillation_prompts(
        account_id="creator_a",
        title="作品",
        source_content="这是一篇足够长的完整作品内容，用于验证提示词会把原文作为数据而不是指令。" * 3,
    )
    assert "不是指令" in user
    assert "required_json_schema" in user

    account_system, account_user = build_account_distillation_prompts(
        account_id="creator_a",
        works=[{"work_id": "work-a", "title": "作品", "source_content": "正文"}],
    )
    assert "evidence_work_ids" in account_system
    assert "work-a" in account_user

    topic_system, topic_user = build_topic_synthesis_prompts(_topic_plan())
    assert "唯一新闻事实来源" in topic_system
    assert "topic_plan" in topic_user

    with pytest.raises(KnowledgeContractError, match="不会静默截断"):
        build_work_distillation_prompts(
            account_id="creator_a",
            title="过长作品",
            source_content="长" * 24_001,
        )
    with pytest.raises(KnowledgeContractError, match="不会静默截断"):
        build_account_distillation_prompts(
            account_id="creator_a",
            works=[{"work_id": "work-long", "title": "过长作品", "source_content": "长" * 8_001}],
        )


def test_api_management_model_slots_are_reused_without_reading_secrets(tmp_path, monkeypatch) -> None:
    config = tmp_path / "api_management.json"
    config.write_text(
        json.dumps(
            {
                "groups": {
                    "story-writing": {
                        "endpoint": "https://ark.example.test/responses",
                        "model_slots": {
                            "STORY_WRITER_ARK_MODEL": "story-ep",
                            "CONTENT_ANALYZER_ARK_MODEL": "content-ep",
                            "STYLE_ADAPTER_ARK_MODEL": "style-ep",
                            "DIRECTORS_V2_ARK_MODEL": "director-ep",
                        },
                        "model_order": ["CONTENT_ANALYZER_ARK_MODEL", "STYLE_ADAPTER_ARK_MODEL"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    for name in (
        "STORY_WRITER_ARK_MODEL",
        "CONTENT_ANALYZER_ARK_MODEL",
        "STYLE_ADAPTER_ARK_MODEL",
        "DIRECTORS_V2_ARK_MODEL",
        "STORY_WRITER_ARK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    snapshot = apply_api_management_model_runtime(config)
    assert snapshot["status"] == "ready"
    assert snapshot["model_count"] == 4
    assert os.environ["CONTENT_ANALYZER_ARK_MODEL"] == "content-ep"
    assert not os.environ.get("STORY_WRITER_ARK_API_KEY")


def test_topic_synthesis_preserves_deterministic_evidence() -> None:
    plan = _topic_plan()
    merged = merge_topic_synthesis(
        plan,
        {
            "recommended_angle": "先看规则如何改变利益分配",
            "consensus": ["规则会改变预期"],
            "new_synthesis": ["把政策变化放回利益结构解释"],
            "evidence_refs": [{"account_id": "attacker", "work_id": "fake"}],
        },
    )
    assert merged["recommended_angle"] == "先看规则如何改变利益分配"
    assert merged["evidence_refs"] == plan["evidence_refs"]
    assert merged["status"] == "REVIEW_REQUIRED"


def test_provider_uses_content_style_and_review_roles() -> None:
    content = _Transport(
        {
            "content_blueprint": {
                "core_claim": "规则变化会重新分配利益",
                "fact_units": [{"id": "fact_1", "statement": "监管部门发布新规", "risk": "LOW"}],
                "argument_chain": [{"id": "step_1", "point": "从事实推导利益变化", "fact_ids": ["fact_1"]}],
                "structure": [{"id": "part_1", "part": "hook", "purpose": "提出问题", "key_points": ["先问为什么"]}],
            }
        }
    )
    style = _Transport(
        {
            "source_summary": "基于已核验事实的规则变化解释",
            "creation_outline": {"hook_3s": "先问为什么", "acts": ["事实", "解释"], "ending": "回到结论"},
            "rewritten_copy": "规则变化，先别急着看热闹，关键是它改变了利益分配。",
            "expression_habits_applied": [],
            "style_application": [{"target": "hook", "habit_category": "问题开场", "transformation": "先提出疑问"}],
            "risk_report": {"facts_to_verify": [], "similarity_guardrails": [], "style_rules_applied": []},
        }
    )
    review = _Transport(
        {
            "status": "PASS",
            "audit_summary": "通过",
            "style_score": 8,
            "fact_risks": [],
            "similarity_risks": [],
            "structure_issues": [],
            "language_issues": [],
            "revision_instructions": [],
            "revised_copy": "",
            "expression_habit_issues": [],
            "expression_habits_used": [],
            "dna_sections_used": ["L1", "L2", "L3-L5"],
        }
    )
    provider = AccountKnowledgeModelProvider(content, style, review)
    draft = provider.draft(
        topic_plan=_topic_plan(),
        style_profile_id="style-a",
        requirements={
            "style_profile": _style_profile(),
            "platform": "douyin",
            "target_duration_seconds": 60,
            "account_knowledge_context": {
                "available": True,
                "knowledge_status": "APPROVED",
                "sample_count": 78,
                "notes": {"Writing-DNA.md": {"layer": "L1-L6", "content": {"rule": "先解释结构"}}},
            },
        },
    )
    assert draft["draft_copy"]
    assert draft["review_status"] == "REVIEW_PENDING"
    assert draft["claim_trace"][0]["source_type"] == "news"
    assert any(item.get("claim_type") == "recommended_angle" for item in draft["claim_trace"])
    assert len(content.calls) == 1
    assert len(style.calls) == 1
    assert len(review.calls) == 1
    assert "先解释结构" in style.calls[0][0]
    assert "账号级 Writing-DNA" in review.calls[0][0]


def test_provider_rejects_unapproved_loaded_style_profile() -> None:
    transport = _Transport(
        {
            "content_blueprint": {"core_claim": "规则变化", "fact_units": [], "argument_chain": [], "structure": []}
        }
    )
    provider = AccountKnowledgeModelProvider(
        transport,
        transport,
        style_profile_loader=lambda _profile_id: {
            "review_status": "PENDING_USER_REVIEW",
            "style_profile": _style_profile(),
        },
    )
    with pytest.raises(AccountKnowledgeModelError, match="尚未人工审核通过"):
        provider.draft(topic_plan=_topic_plan(), style_profile_id="style-a", requirements={})


def test_provider_surfaces_blocked_fact_risks_as_unsupported_claims() -> None:
    content = _Transport(
        {
            "content_blueprint": {"core_claim": "规则变化", "fact_units": [], "argument_chain": [], "structure": []}
        }
    )
    style = _Transport(
        {
            "source_summary": "摘要",
            "creation_outline": {},
            "rewritten_copy": "待审核文案",
            "risk_report": {},
        }
    )
    review = _Transport(
        {
            "status": "BLOCK",
            "audit_summary": "无法核验",
            "fact_risks": ["无法核验的新增事实"],
            "similarity_risks": [],
        }
    )
    provider = AccountKnowledgeModelProvider(content, style, review)
    draft = provider.draft(
        topic_plan=_topic_plan(),
        style_profile_id="style-a",
        requirements={"style_profile": _style_profile()},
    )
    assert draft["review_status"] == "BLOCKED"
    assert draft["unsupported_claims"] == ["无法核验的新增事实"]


def test_account_distillation_batches_large_corpus_and_restores_evidence_ids() -> None:
    class _BatchTransport:
        def __init__(self):
            self.calls = 0

        def __call__(self, system: str, user: str) -> str:
            self.calls += 1
            if self.calls < 3:
                return json.dumps({"writing_dna": f"局部规则 {self.calls}"}, ensure_ascii=False)
            return json.dumps(
                {
                    "writing_dna": "合并规则",
                    "cognition_framework": [
                        {
                            "title": "批次证据",
                            "claim": "规则先于情绪",
                            "evidence_work_ids": ["batch-1", "batch-2"],
                        }
                    ],
                },
                ensure_ascii=False,
            )

    transport = _BatchTransport()
    provider = AccountKnowledgeModelProvider(transport, transport, transport)
    works = [{"work_id": f"work-{index}", "title": f"作品 {index}", "source_content": "完整正文"} for index in range(1, 6)]
    result = provider.distill_account(account_id="creator_a", works=works, batch_size=2)
    assert transport.calls == 4
    assert result["cognition_framework"][0]["evidence_work_ids"] == ["work-1", "work-2", "work-3", "work-4"]
