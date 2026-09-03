from __future__ import annotations

import pytest

from workflow_1256.account_knowledge.contracts import KnowledgeContractError
from workflow_1256.account_knowledge.distillation import (
    build_account_distillation,
    DistillationProviderRequired,
    build_cognition_card_from_distillation,
    build_work_card_from_distillation,
    distill_with_provider,
    write_account_distillation,
)
from workflow_1256.account_knowledge.obsidian_repository import ObsidianRepository
from workflow_1256.account_knowledge.contracts import WorkCard


def _result() -> dict:
    return {
        "facts": ["原文事实"],
        "core_thesis": "变化会重新分配利益。",
        "angle": "从结构解释事件",
        "reasoning_steps": ["确认事实", "分析利益", "推导影响"],
        "evidence_types": ["公开数据"],
        "counterpoints": ["存在短期例外"],
        "conclusion_boundary": "不外推到所有行业",
    }


def test_distillation_builds_traceable_review_pending_work_card() -> None:
    card = build_work_card_from_distillation(
        account_id="creator_a",
        title="作品",
        source_content="这是足够长的原文内容，用于验证作品卡会保留内容哈希和来源。" * 2,
        source_url="https://example.com/work/1",
        source_ref="raw_works/work-1.txt",
        result=_result(),
        distilled_by="local-test",
        prompt_version="v1",
    )
    assert card.knowledge_status == "REVIEW_PENDING"
    assert len(card.content_hash) == 64
    assert card.work_id.startswith("work-")


def test_cognition_distillation_requires_evidence() -> None:
    with pytest.raises(KnowledgeContractError, match="evidence_work_ids"):
        build_cognition_card_from_distillation(
            cognition_id="cog-1",
            account_id="creator_a",
            result={"title": "认知", "claim": "主张", "confidence": 0.7},
        )


def test_distill_with_provider_is_blocked_without_explicit_provider() -> None:
    with pytest.raises(DistillationProviderRequired, match="不会自动调用模型"):
        distill_with_provider(
            None,
            account_id="creator_a",
            title="作品",
            source_content="足够长的原文内容" * 5,
            source_url="https://example.com/work/1",
        )


def test_account_distillation_marks_small_corpus_format_only_and_writes_five_documents(tmp_path) -> None:
    works = [WorkCard(work_id="work-1", account_id="creator_a", title="作品")]
    bundle = build_account_distillation(
        account_id="creator_a",
        works=works,
        result={
            "language_dna": {"average_sentence_length": 18},
            "structure_templates": ["观点式开头→总分总"],
            "cognition_framework": [
                {"title": "先看结构", "claim": "先确认利益结构。", "evidence_work_ids": ["work-1"]}
            ],
            "material_strategy": {"preferred": ["公开资料"]},
            "visual_style": {"image_role": "证据型"},
            "writing_dna": "待人工审核的规则摘要",
        },
    )
    assert bundle["status"] == "FORMAT_ONLY_REVIEW_REQUIRED"
    repository = ObsidianRepository(tmp_path / "vault")
    paths = write_account_distillation(repository, bundle)
    assert {path.name for path in paths} == {
        "语言DNA.md",
        "文章结构模板.md",
        "写作视角与认知框架.md",
        "视觉风格指南.md",
        "Writing-DNA.md",
    }
    _, body = repository.read_account_note("creator_a", filename="Writing-DNA.md")
    assert "待人工审核" in body


def test_account_distillation_rejects_unknown_evidence_work() -> None:
    with pytest.raises(KnowledgeContractError, match="不在本账号语料"):
        build_account_distillation(
            account_id="creator_a",
            works=[WorkCard(work_id="work-1", account_id="creator_a", title="作品")],
            result={
                "cognition_framework": [
                    {"title": "认知", "claim": "主张", "evidence_work_ids": ["work-unknown"]}
                ]
            },
        )
