from __future__ import annotations

from workflow_1256.account_knowledge.contracts import WorkCard
from workflow_1256.account_knowledge.indexer import LocalKnowledgeIndex
from tools.verify_account_knowledge_acceptance import build_report


def _index() -> LocalKnowledgeIndex:
    index = LocalKnowledgeIndex()
    index.add_work_card(
        WorkCard(
            work_id="work-a",
            account_id="creator_a",
            title="监管与利益结构",
            angle="从结构解释变化",
            core_thesis="规则变化会重新分配利益",
            facts=["监管新规"],
            content_grade="S",
            knowledge_status="APPROVED",
        ),
        metadata={"domain": "科技", "published_at": "2026-08-20"},
    )
    index.add_work_card(
        WorkCard(
            work_id="work-c",
            account_id="creator_a",
            title="监管失败案例",
            angle="错误归因",
            core_thesis="失败案例只用于避错",
            facts=["监管新规"],
            content_grade="C",
            knowledge_status="APPROVED",
        )
    )
    return index


def test_acceptance_report_checks_trace_and_normal_c_filter() -> None:
    plan = {
        "topic_plan_id": "topic-v2-demo",
        "status": "REVIEW_REQUIRED",
        "event_card": {
            "event_id": "event-1",
            "title": "监管政策变化",
            "verified_facts": ["监管部门发布新规"],
            "source_refs": [{"url": "https://news.example.com/1"}],
        },
        "target_account_id": "creator_a",
        "reference_account_ids": [],
        "author_lenses": [{"account_id": "creator_a", "evidence_refs": [{"account_id": "creator_a", "work_id": "work-a"}]}],
        "evidence_refs": [{"account_id": "creator_a", "work_id": "work-a", "source_url": "https://example.com/work-a"}],
    }
    draft = {
        "draft_id": "draft-v2-demo",
        "topic_plan_id": "topic-v2-demo",
        "target_account_id": "creator_a",
        "style_profile_id": "style-a",
        "draft_copy": "先确认事实，再解释规则变化。",
        "claim_trace": [
            {"claim": "监管部门发布新规", "source_type": "news", "source_refs": [{"url": "https://news.example.com/1"}]},
            {"claim": "从结构解释变化", "source_type": "knowledge", "knowledge_refs": [{"account_id": "creator_a", "work_id": "work-a"}]},
        ],
        "knowledge_refs": [{"account_id": "creator_a", "work_id": "work-a"}],
        "review_status": "REVIEW_PENDING",
    }
    report = build_report(
        index=_index(),
        topic_plan=plan,
        draft=draft,
        expected={"queries": [{"account_id": "creator_a", "query": "监管 利益结构", "expected_work_ids": ["work-a"]}]},
    )
    assert report["status"] == "ready"
    assert report["metrics"]["retrieval"]["hit_rate"] == 1.0
    assert report["metrics"]["retrieval"]["normal_retrieval_c_grade_hits"] == 0


def test_acceptance_report_blocks_untraceable_draft() -> None:
    report = build_report(
        index=_index(),
        draft={
            "draft_id": "draft-v2-demo",
            "topic_plan_id": "topic-v2-demo",
            "target_account_id": "creator_a",
            "style_profile_id": "style-a",
            "draft_copy": "待审核文案",
            "claim_trace": [{"claim": "无来源断言", "source_type": "knowledge"}],
            "review_status": "REVIEW_PENDING",
        },
    )
    assert report["status"] == "blocked"
    assert report["checks"]["draft_claims_traceable"] is False
