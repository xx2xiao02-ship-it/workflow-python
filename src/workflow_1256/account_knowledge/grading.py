"""知识资产分级和正常检索资格。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .contracts import ALLOWED_CONTENT_GRADES, ALLOWED_PERFORMANCE_GRADES, GradeCard, KnowledgeContractError, WorkCard, utc_now


GRADE_WEIGHTS = {"S": 1.0, "A": 0.8, "B": 0.35, "C": 0.0}


def is_normal_retrieval_eligible(card: WorkCard) -> bool:
    card.validate()
    return card.content_grade != "C" and card.knowledge_status in {"APPROVED", "PUBLISHED"}


def apply_manual_grade(
    card: WorkCard,
    *,
    content_grade: str,
    performance_grade: str = "UNRATED",
    reviewer: str,
    notes: str = "",
) -> GradeCard:
    card.validate()
    content = str(content_grade or "").upper().strip()
    performance = str(performance_grade or "UNRATED").upper().strip()
    reviewer_text = str(reviewer or "").strip()
    if content not in ALLOWED_CONTENT_GRADES:
        raise KnowledgeContractError("content_grade 必须是 S/A/B/C")
    if performance not in ALLOWED_PERFORMANCE_GRADES:
        raise KnowledgeContractError("performance_grade 必须是 S/A/B/C/UNRATED")
    if not reviewer_text:
        raise KnowledgeContractError("评级必须记录人工 reviewer")
    card.content_grade = content
    card.performance_grade = performance
    card.retrieval_weight = GRADE_WEIGHTS[content]
    card.manual_reviewed = True
    card.knowledge_status = "APPROVED"
    return GradeCard(
        grade_id="grade-" + uuid4().hex[:20],
        work_id=card.work_id,
        account_id=card.account_id,
        content_grade=content,
        performance_grade=performance,
        reviewer=reviewer_text,
        notes=notes,
        created_at=utc_now(),
    ).validate()


__all__ = ["GRADE_WEIGHTS", "apply_manual_grade", "is_normal_retrieval_eligible"]
