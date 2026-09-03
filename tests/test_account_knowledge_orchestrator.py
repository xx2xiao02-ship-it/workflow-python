from __future__ import annotations

import pytest

from workflow_1256.account_knowledge.contracts import CognitionCard, WorkCard
from workflow_1256.account_knowledge.indexer import LocalKnowledgeIndex
from workflow_1256.account_knowledge.orchestrator import (
    CopywritingProviderRequired,
    build_topic_plan_from_index,
    run_copywriting_provider,
)
from workflow_1256.topic_knowledge_v2 import EventCard


def _event() -> EventCard:
    return EventCard(
        event_id="event-1",
        title="监管政策变化",
        verified_facts=["监管部门发布新规"],
        source_refs=[{"url": "https://news.example.com/1"}],
    )


def _index() -> LocalKnowledgeIndex:
    index = LocalKnowledgeIndex()
    index.add_work_card(
        WorkCard(
            work_id="work-a",
            account_id="creator_a",
            title="监管与利益结构",
            angle="从利益结构解释监管变化",
            core_thesis="规则变化会重新分配利益",
            facts=["监管新规"],
            content_grade="S",
            knowledge_status="APPROVED",
        )
    )
    index.add_work_card(
        WorkCard(
            work_id="work-b",
            account_id="creator_b",
            title="政策变化的长期影响",
            angle="先看长期影响再看情绪",
            core_thesis="制度变化影响长期预期",
            facts=["监管新规"],
            content_grade="A",
            knowledge_status="APPROVED",
        )
    )
    index.add_cognition_card(
        CognitionCard(
            cognition_id="cog-a",
            account_id="creator_a",
            title="事实到利益",
            claim="先确认事实，再分析利益结构。",
            reasoning_pattern="事实→利益→影响",
            evidence_work_ids=["work-a"],
            confidence=0.9,
            knowledge_status="APPROVED",
        )
    )
    return index


def test_topic_plan_from_index_retrieves_each_account_separately() -> None:
    plan = build_topic_plan_from_index(
        index=_index(),
        event_card=_event(),
        target_account_id="creator_a",
        reference_account_ids=["creator_b"],
    )
    assert plan["status"] == "REVIEW_REQUIRED"
    assert [lens["account_id"] for lens in plan["author_lenses"]] == ["creator_a", "creator_b"]
    assert {ref["account_id"] for ref in plan["evidence_refs"]} == {"creator_a", "creator_b"}
    assert "approved_copy" not in plan


def test_topic_plan_honors_retrieval_domain_and_date_policy() -> None:
    index = LocalKnowledgeIndex()
    index.add_work_card(
        WorkCard(
            work_id="work-tech",
            account_id="creator_a",
            title="监管与利益结构",
            angle="科技监管的利益结构",
            core_thesis="规则变化会重新分配利益",
            facts=["监管新规"],
            content_grade="S",
            knowledge_status="APPROVED",
            published_at="2026-08-20",
        ),
        metadata={"domain": "科技", "published_at": "2026-08-20"},
    )
    index.add_work_card(
        WorkCard(
            work_id="work-old",
            account_id="creator_a",
            title="监管与利益结构",
            angle="金融监管的利益结构",
            core_thesis="规则变化会重新分配利益",
            facts=["监管新规"],
            content_grade="S",
            knowledge_status="APPROVED",
            published_at="2025-01-01",
        ),
        metadata={"domain": "金融", "published_at": "2025-01-01"},
    )
    from workflow_1256.topic_knowledge_v2 import RetrievalPolicy

    plan = build_topic_plan_from_index(
        index=index,
        event_card=_event(),
        target_account_id="creator_a",
        retrieval_policy=RetrievalPolicy(domain="科技", published_after="2026-01-01"),
    )
    assert [ref["work_id"] for ref in plan["evidence_refs"]] == ["work-tech"]


def test_copywriting_provider_boundary_blocks_without_model() -> None:
    with pytest.raises(CopywritingProviderRequired, match="不会自动调用模型"):
        run_copywriting_provider(None, topic_plan={"topic_plan_id": "topic-v2-1", "status": "READY"}, style_profile_id="style-a")


class _FakeProvider:
    def draft(self, *, topic_plan, style_profile_id, requirements):
        return {
            "draft_copy": "这不是情绪问题，而是规则改变了利益分配。",
            "claim_trace": [{"claim": "监管部门发布新规", "source_type": "news"}],
            "knowledge_refs": [{"account_id": "creator_a", "work_id": "work-a"}],
            "style_application": ["先给结论，再拆结构"],
        }


def test_injected_provider_only_outputs_review_pending_draft() -> None:
    topic_plan = build_topic_plan_from_index(
        index=_index(),
        event_card=_event(),
        target_account_id="creator_a",
        reference_account_ids=["creator_b"],
    )
    draft = run_copywriting_provider(
        _FakeProvider(),
        topic_plan=topic_plan,
        style_profile_id="style-a",
        requirements={"audience": "普通用户", "platform": "douyin", "target_duration_seconds": 60},
    )
    assert draft["review_status"] == "REVIEW_PENDING"
    assert "approved_copy" not in draft
