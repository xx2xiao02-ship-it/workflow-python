from __future__ import annotations

import pytest

from workflow_1256.account_knowledge.contracts import KnowledgeContractError
from workflow_1256.account_knowledge.obsidian_repository import ObsidianRepository
from workflow_1256.copywriting_knowledge_v2 import (
    build_copywriting_draft,
    promote_v2_draft,
    validate_copywriting_draft,
)
from workflow_1256.topic_knowledge_v2 import EventCard, build_topic_plan


def _plan() -> dict:
    return build_topic_plan(
        event_card=EventCard(
            event_id="event-1",
            title="政策变化",
            verified_facts=["部门发布新规"],
            source_refs=[{"url": "https://news.example.com/1"}],
        ),
        target_account_id="creator_a",
        recommended_angle="解释规则变化背后的利益结构",
        evidence_refs=[{"account_id": "creator_a", "work_id": "work-a"}],
    )


def test_copywriting_draft_is_review_pending_and_traceable() -> None:
    draft = build_copywriting_draft(
        topic_plan=_plan(),
        style_profile_id="style_creator_a",
        draft_copy="这条规则变化，真正改变的是利益分配方式。",
        claim_trace=[{"claim": "部门发布新规", "source_type": "news", "source_id": "news-1"}],
        knowledge_refs=[{"account_id": "creator_a", "work_id": "work-a"}],
    )
    assert draft["review_status"] == "REVIEW_PENDING"
    assert "approved_copy" not in draft
    assert validate_copywriting_draft(draft)["draft_id"] == draft["draft_id"]


def test_unsupported_claim_blocks_draft() -> None:
    draft = build_copywriting_draft(
        topic_plan=_plan(),
        style_profile_id="style_creator_a",
        draft_copy="无法核验的说法。",
        unsupported_claims=["无法核验的说法"],
    )
    assert draft["review_status"] == "BLOCKED"
    assert "存在未追溯事实" in draft["review_flags"]


def test_blocked_draft_cannot_be_promoted_even_with_manual_flag() -> None:
    draft = build_copywriting_draft(
        topic_plan=_plan(),
        style_profile_id="style_creator_a",
        draft_copy="审核阻断文案",
        review_status="BLOCKED",
    )
    with pytest.raises(KnowledgeContractError, match="审核已阻断"):
        promote_v2_draft(draft, manual_approval=True)


def test_promotion_requires_explicit_manual_approval() -> None:
    draft = build_copywriting_draft(topic_plan=_plan(), style_profile_id="style_creator_a", draft_copy="可审核文案")
    with pytest.raises(KnowledgeContractError, match="manual_approval"):
        promote_v2_draft(draft, manual_approval=False)
    handoff = promote_v2_draft(draft, manual_approval=True)
    assert handoff["approved_copy"] == "可审核文案"
    assert handoff["handoff_type"] == "v2_manual_approval"


def test_validator_rejects_approved_copy_in_v2_draft() -> None:
    with pytest.raises(KnowledgeContractError, match="approved_copy"):
        validate_copywriting_draft(
            {"draft_id": "draft-v2-1", "review_status": "REVIEW_PENDING", "approved_copy": "不应存在"}
        )


def test_manual_promotion_can_write_to_separate_approved_vault(tmp_path) -> None:
    draft = build_copywriting_draft(topic_plan=_plan(), style_profile_id="style_creator_a", draft_copy="人工确认文案")
    handoff = promote_v2_draft(draft, manual_approval=True)
    path = ObsidianRepository(tmp_path / "vault").save_approved_copy_note(handoff)
    assert path.parent.name == "approved"
    assert "人工确认文案" in path.read_text(encoding="utf-8")
