from __future__ import annotations

import pytest

from workflow_1256.account_knowledge.contracts import KnowledgeContractError, WorkCard
from workflow_1256.account_knowledge.grading import apply_manual_grade, is_normal_retrieval_eligible


def test_manual_grade_is_separate_from_performance_grade() -> None:
    card = WorkCard(work_id="work-1", account_id="creator_a", title="作品", knowledge_status="REVIEW_PENDING")
    grade = apply_manual_grade(card, content_grade="S", performance_grade="B", reviewer="editor")
    assert grade.content_grade == "S"
    assert grade.performance_grade == "B"
    assert card.retrieval_weight == 1.0
    assert is_normal_retrieval_eligible(card)


def test_c_grade_is_never_eligible_for_normal_retrieval() -> None:
    card = WorkCard(work_id="work-1", account_id="creator_a", title="失败案例", knowledge_status="REVIEW_PENDING")
    apply_manual_grade(card, content_grade="C", reviewer="editor")
    assert not is_normal_retrieval_eligible(card)


def test_manual_grade_requires_reviewer() -> None:
    card = WorkCard(work_id="work-1", account_id="creator_a", title="作品")
    with pytest.raises(KnowledgeContractError, match="reviewer"):
        apply_manual_grade(card, content_grade="A", reviewer="")

