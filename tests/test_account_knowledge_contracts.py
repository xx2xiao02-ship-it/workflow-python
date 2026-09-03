from __future__ import annotations

import pytest

from workflow_1256.account_knowledge.contracts import (
    CognitionCard,
    KnowledgeContractError,
    WorkCard,
    build_work_id,
    content_sha256,
)


def test_build_work_id_is_stable_for_duplicate_imports() -> None:
    first = build_work_id(
        account_id="creator_a",
        title="同一篇作品",
        source_url="https://example.com/work/1",
        published_at="2026-08-01",
    )
    second = build_work_id(
        account_id="creator_a",
        title="同一篇作品",
        source_url="https://example.com/work/1",
        published_at="2026-08-01",
    )
    assert first == second
    assert first.startswith("work-")


def test_work_card_derives_grade_weight_and_normalizes_lists() -> None:
    card = WorkCard(
        work_id="work-demo",
        account_id="creator_a",
        title="  一篇作品\n",
        content_grade="A",
        facts=["事实一", "事实一", "  事实二  "],
    ).validate()
    assert card.title == "一篇作品"
    assert card.facts == ["事实一", "事实二"]
    assert card.retrieval_weight == 0.8


def test_work_card_rejects_absolute_source_reference_and_bad_grade() -> None:
    with pytest.raises(KnowledgeContractError, match="相对路径"):
        WorkCard(
            work_id="work-demo",
            account_id="creator_a",
            title="作品",
            source_ref="C:/secret/source.txt",
        ).validate()
    with pytest.raises(KnowledgeContractError, match="content_grade"):
        WorkCard(work_id="work-demo", account_id="creator_a", title="作品", content_grade="D").validate()


def test_cognition_card_requires_traceable_evidence_shape() -> None:
    card = CognitionCard(
        cognition_id="cog-demo",
        account_id="creator_a",
        title="先拆利益结构",
        claim="先解释谁从变化中获益，再判断事件影响。",
        evidence_work_ids=["work-demo"],
        confidence=0.9,
    ).validate()
    assert card.evidence_work_ids == ["work-demo"]
    assert content_sha256("abc") == content_sha256("abc")

