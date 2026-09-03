from __future__ import annotations

from workflow_1256.account_knowledge.contracts import WorkCard
from workflow_1256.account_knowledge.offline_candidates import (
    OFFLINE_CANDIDATE_METHOD,
    OFFLINE_CANDIDATE_STATUS,
    build_offline_candidate_bundle,
    write_offline_candidate_notes,
)
from workflow_1256.account_knowledge.distillation import build_account_distillation, write_account_distillation
from workflow_1256.account_knowledge.obsidian_repository import ObsidianRepository


def _work(work_id: str, *, angle: str = "从结构解释事件") -> WorkCard:
    return WorkCard(
        work_id=work_id,
        account_id="creator_a",
        title=f"作品 {work_id}",
        source_ref=f"raw_works/{work_id}.md",
        facts=["事实材料"],
        core_thesis="变化会重新分配利益。",
        angle=angle,
        reasoning_steps=["确认事实", "分析利益"],
        evidence_types=["公开数据"],
        conclusion_boundary="只讨论当前场景",
        knowledge_status="REVIEW_PENDING",
    ).validate()


def test_offline_candidate_keeps_status_and_traceable_evidence() -> None:
    works = [_work("work-1"), _work("work-2", angle="从个体经验解释事件")]
    bundle = build_offline_candidate_bundle(
        account_id="creator_a",
        works=works,
        source_texts={
            "work-1": "这是第一篇原文。先给事实，再给结论。",
            "work-2": "这是第二篇原文？补充一个限制。",
        },
    )
    assert bundle["status"] == OFFLINE_CANDIDATE_STATUS
    assert bundle["distillation_method"] == OFFLINE_CANDIDATE_METHOD
    assert bundle["sample_count"] == 2
    assert bundle["evidence_work_ids"] == ["work-1", "work-2"]
    for rule in bundle["cognition_framework"]:
        assert set(rule["evidence_work_ids"]).issubset(set(bundle["evidence_work_ids"]))
    assert bundle["language_dna"]["source_article_count"] == 2
    assert "不能推断真实视觉风格" in bundle["visual_style"]["visual_evidence"]


def test_offline_candidate_writes_standard_five_notes_as_review_pending(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    works = [_work("work-1")]
    bundle = build_offline_candidate_bundle(
        account_id="creator_a",
        works=works,
        source_texts={"work-1": "这是一篇足够的原文。用于候选版统计。"},
    )
    paths = write_offline_candidate_notes(repository, bundle)
    assert {path.name for path in paths} == {
        "语言DNA.md",
        "文章结构模板.md",
        "写作视角与认知框架.md",
        "视觉风格指南.md",
        "Writing-DNA.md",
    }
    frontmatter, body = repository.read_account_note("creator_a", filename="Writing-DNA.md")
    assert frontmatter["distillation_status"] == OFFLINE_CANDIDATE_STATUS
    assert frontmatter["knowledge_status"] == "REVIEW_PENDING"
    assert frontmatter["distillation_method"] == OFFLINE_CANDIDATE_METHOD
    assert frontmatter["sample_count"] == 1
    assert frontmatter["evidence_work_ids"] == ["work-1"]
    assert "候选版" in body


def test_account_dna_context_requires_all_five_notes_approved(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    bundle = build_offline_candidate_bundle(
        account_id="creator_a",
        works=[_work("work-1")],
        source_texts={"work-1": "这是一篇足够的原文。"},
    )
    write_offline_candidate_notes(repository, bundle)
    pending = repository.load_account_dna_context("creator_a")
    assert pending["available"] is False
    assert pending["knowledge_status"] == "REVIEW_PENDING"
    assert pending["distillation_status"] == OFFLINE_CANDIDATE_STATUS

    for filename in repository.ACCOUNT_DNA_NOTE_FILENAMES:
        frontmatter, body = repository.read_account_note("creator_a", filename=filename)
        repository.write_account_note(
            "creator_a",
            filename=filename,
            frontmatter={**frontmatter, "knowledge_status": "APPROVED"},
            body=body,
            overwrite=True,
        )
    approved = repository.load_account_dna_context("creator_a")
    assert approved["available"] is True
    assert approved["knowledge_status"] == "APPROVED"
    assert set(approved["notes"]) == set(repository.ACCOUNT_DNA_NOTE_FILENAMES)


def test_account_dna_approval_requires_deep_reviewable_bundle(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    works = [WorkCard(work_id=f"work-{index}", account_id="creator_a", title=f"作品 {index}") for index in range(30)]
    bundle = build_account_distillation(
        account_id="creator_a",
        works=works,
        result={
            "cognition_framework": [
                {"title": "认知", "claim": "先看事实", "evidence_work_ids": ["work-0"]}
            ],
            "writing_dna": "深度规则",
        },
    )
    assert bundle["status"] == "READY_FOR_REVIEW"
    write_account_distillation(repository, bundle)
    paths = repository.approve_account_dna("creator_a", reviewer="editor", notes="已抽查证据链")
    assert len(paths) == 5
    context = repository.load_account_dna_context("creator_a")
    assert context["available"] is True
    assert context["knowledge_status"] == "APPROVED"
    frontmatter, _ = repository.read_account_note("creator_a", filename="Writing-DNA.md")
    assert frontmatter["approved_by"] == "editor"
    assert frontmatter["approval_notes"] == "已抽查证据链"


def test_account_dna_approval_rejects_offline_candidate(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    bundle = build_offline_candidate_bundle(
        account_id="creator_a",
        works=[_work("work-1")],
        source_texts={"work-1": "这是一篇足够的原文。"},
    )
    write_offline_candidate_notes(repository, bundle)
    try:
        repository.approve_account_dna("creator_a", reviewer="editor")
    except Exception as exc:
        assert "PROVISIONAL_OFFLINE" in str(exc)
    else:
        raise AssertionError("离线候选版不应被直接批准")
