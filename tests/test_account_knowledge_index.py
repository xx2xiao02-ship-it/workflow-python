from __future__ import annotations

from workflow_1256.account_knowledge.contracts import CognitionCard, WorkCard
from workflow_1256.account_knowledge.indexer import LocalKnowledgeIndex, tokenize
from workflow_1256.account_knowledge.obsidian_repository import ObsidianRepository
from workflow_1256.account_knowledge.retrieval import AccountKnowledgeRetriever


def _approved_work(work_id: str, account_id: str, title: str, grade: str = "A") -> WorkCard:
    return WorkCard(
        work_id=work_id,
        account_id=account_id,
        title=title,
        core_thesis="解释利益结构和长期影响",
        facts=["政策变化", "利益结构"],
        reasoning_steps=["先确认事实", "再分析影响"],
        content_grade=grade,
        knowledge_status="APPROVED",
    )


def test_tokenize_supports_chinese_and_latin_terms() -> None:
    terms = tokenize("AI 监管变化")
    assert "ai" in terms
    assert "监管" in terms
    assert "变化" in terms


def test_local_index_filters_c_grade_and_keeps_account_isolation() -> None:
    index = LocalKnowledgeIndex()
    index.add_work_card(_approved_work("work-a", "creator_a", "监管与利益结构", "S"))
    index.add_work_card(_approved_work("work-c", "creator_a", "监管失败案例", "C"))
    index.add_work_card(_approved_work("work-b", "creator_b", "监管与利益结构", "S"))
    results = index.search(account_id="creator_a", query="监管 利益结构", top_k=10)
    assert [item["record"]["work_id"] for item in results] == ["work-a"]
    assert all(item["record"]["account_id"] == "creator_a" for item in results)


def test_negative_case_channel_only_returns_c_grade() -> None:
    index = LocalKnowledgeIndex()
    index.add_work_card(_approved_work("work-a", "creator_a", "监管失败案例", "C"))
    index.add_work_card(_approved_work("work-b", "creator_a", "监管成功案例", "A"))
    results = AccountKnowledgeRetriever(index).retrieve_negative_cases(
        account_id="creator_a", query="监管 案例", top_k=10
    )
    assert [item["record"]["work_id"] for item in results] == ["work-a"]


def test_retriever_returns_traceable_refs_and_separates_multiple_accounts() -> None:
    index = LocalKnowledgeIndex()
    index.add_work_card(_approved_work("work-a", "creator_a", "结构性解释", "A"))
    index.add_cognition_card(
        CognitionCard(
            cognition_id="cog-a",
            account_id="creator_a",
            title="先看结构",
            claim="先确认利益结构，再判断情绪。",
            reasoning_pattern="事实→利益→影响",
            evidence_work_ids=["work-a"],
            confidence=0.9,
            knowledge_status="APPROVED",
        )
    )
    index.add_work_card(_approved_work("work-b", "creator_b", "结构性解释", "S"))
    retriever = AccountKnowledgeRetriever(index)
    many = retriever.retrieve_many_accounts(account_ids=["creator_a", "creator_b"], query="结构", top_k_per_account=3)
    assert set(many) == {"creator_a", "creator_b"}
    assert all(item["evidence_ref"]["account_id"] == "creator_a" for item in many["creator_a"])
    assert any(item["evidence_ref"]["cognition_id"] == "cog-a" for item in many["creator_a"])


def test_index_can_be_dumped_and_rebuilt(tmp_path) -> None:
    index = LocalKnowledgeIndex()
    index.add_work_card(_approved_work("work-a", "creator_a", "可重建索引"))
    path = index.dump(tmp_path / "index.json")
    restored = LocalKnowledgeIndex.load(path)
    assert restored.search(account_id="creator_a", query="重建", top_k=1)[0]["record"]["work_id"] == "work-a"


def test_index_applies_domain_and_published_after_filters() -> None:
    index = LocalKnowledgeIndex()
    index.add_work_card(
        _approved_work("work-tech-new", "creator_a", "监管与利益结构", "A"),
        metadata={"domain": "科技", "published_at": "2026-08-20"},
    )
    index.add_work_card(
        _approved_work("work-finance-old", "creator_a", "监管与利益结构", "S"),
        metadata={"domain": "金融", "published_at": "2025-01-01"},
    )
    results = index.search(
        account_id="creator_a",
        query="监管 利益结构",
        top_k=10,
        domain="科技",
        published_after="2026-01-01",
    )
    assert [item["record"]["work_id"] for item in results] == ["work-tech-new"]


def test_rebuild_reads_importer_metadata_for_domain_and_date_filters(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    card = _approved_work("work-meta", "creator_a", "监管与利益结构", "S")
    card.source_ref = "raw_works/work-meta.txt"
    card.published_at = "2026-08-20"
    repository.save_work_card(card)
    repository.save_work_metadata(
        "creator_a",
        "work-meta",
        {"domain": "科技", "published_at": "2026-08-20", "topic_tags": ["监管"]},
    )
    index = LocalKnowledgeIndex()
    assert index.rebuild_from_repository(repository) == 1
    results = index.search(
        account_id="creator_a",
        query="监管",
        domain="科技",
        published_after="2026-01-01",
    )
    assert results[0]["record"]["work_id"] == "work-meta"
