from __future__ import annotations

import pytest

from workflow_1256.account_knowledge.contracts import KnowledgeContractError
from workflow_1256.topic_knowledge_v2 import EventCard, RetrievalPolicy, build_topic_plan, validate_topic_plan


def _event() -> EventCard:
    return EventCard(
        event_id="event-1",
        title="监管政策变化",
        verified_facts=["监管部门发布新规"],
        source_refs=[{"source_id": "news-1", "url": "https://news.example.com/1"}],
    )


def test_topic_plan_keeps_news_facts_separate_from_account_evidence() -> None:
    plan = build_topic_plan(
        event_card=_event(),
        target_account_id="creator_a",
        reference_account_ids=["creator_b"],
        recommended_angle="从利益结构解释规则变化",
        author_lenses=[{"account_id": "creator_b", "angle": "先看利益结构"}],
        evidence_refs=[
            {
                "account_id": "creator_b",
                "work_id": "work-b",
                "source_url": "https://creator.example.com/b",
            }
        ],
        retrieval_policy=RetrievalPolicy(top_k_per_account=3),
    )
    assert plan["status"] == "READY"
    assert plan["event_card"]["source_refs"][0]["source_id"] == "news-1"
    assert plan["evidence_refs"][0]["account_id"] == "creator_b"
    assert "approved_copy" not in plan


def test_topic_plan_without_angle_or_evidence_requires_review() -> None:
    plan = build_topic_plan(event_card=_event(), target_account_id="creator_a")
    assert plan["status"] == "REVIEW_REQUIRED"


def test_topic_policy_rejects_c_grade() -> None:
    with pytest.raises(KnowledgeContractError, match="C 级"):
        RetrievalPolicy(allowed_content_grades=("S", "C")).validate()


def test_topic_plan_validator_rejects_accidental_approved_copy() -> None:
    with pytest.raises(KnowledgeContractError, match="approved_copy"):
        validate_topic_plan({"topic_plan_id": "topic-v2-1", "status": "READY", "approved_copy": "误写入"})

