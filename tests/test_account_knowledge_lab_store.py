from __future__ import annotations

import pytest

from workflow_1256.account_knowledge.lab_store import LabArtifactStore, LabArtifactStoreError
from workflow_1256.copywriting_knowledge_v2 import build_copywriting_draft
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
        recommended_angle="从结构解释变化",
        evidence_refs=[{"account_id": "creator_a", "work_id": "work-a"}],
    )


def test_lab_store_persists_topic_plan_and_draft_separately(tmp_path) -> None:
    store = LabArtifactStore(tmp_path / "lab")
    plan = _plan()
    plan_path = store.save_topic_plan(plan)
    assert plan_path.parent.name == "topic_plans"
    assert store.load_topic_plan(plan["topic_plan_id"])["status"] == "READY"
    draft = build_copywriting_draft(topic_plan=plan, style_profile_id="style-a", draft_copy="待审核文案")
    draft_path = store.save_draft(draft)
    assert draft_path.parent.name == "drafts"
    assert store.load_draft(draft["draft_id"])["review_status"] == "REVIEW_PENDING"


def test_lab_store_rejects_non_manual_handoff(tmp_path) -> None:
    store = LabArtifactStore(tmp_path / "lab")
    with pytest.raises(LabArtifactStoreError, match="人工批准"):
        store.save_handoff({"handoff_type": "draft", "draft_id": "draft-1", "approved_copy": "错误"})

