from __future__ import annotations

import os

import pytest

from workflow_1256.account_knowledge.contracts import CognitionCard, WorkCard
from workflow_1256.account_knowledge.obsidian_repository import (
    KnowledgeConflictError,
    ObsidianRepository,
    ObsidianRepositoryError,
    VaultConfigError,
)


def _work_card() -> WorkCard:
    return WorkCard(
        work_id="work-demo",
        account_id="creator_a",
        title="测试作品",
        platform="douyin",
        source_url="https://example.com/work/1",
        source_ref="raw_works/work-demo.txt",
        facts=["新闻事实"],
        core_thesis="变化首先影响分配关系。",
        content_grade="S",
        knowledge_status="APPROVED",
        manual_reviewed=True,
    )


def test_repository_roundtrip_keeps_frontmatter_and_body(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    path = repository.save_work_card(_work_card(), body="# 原文\n这是作品原文。")
    assert path.is_file()
    card, body = repository.load_work_card("creator_a", "work-demo")
    assert card.account_id == "creator_a"
    assert card.content_grade == "S"
    assert "作品原文" in body
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    assert "source_ref:" in raw


def test_repository_load_raw_source_is_vault_bound(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    repository.initialize_account("creator_a")
    repository.save_raw_source("creator_a", "raw_works/work-demo.txt", "完整原文".encode("utf-8"))
    assert repository.load_raw_source("creator_a", "raw_works/work-demo.txt") == "完整原文"
    with pytest.raises(ObsidianRepositoryError):
        repository.load_raw_source("creator_a", "../secret.txt")


def test_duplicate_save_is_idempotent_and_does_not_create_second_file(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    first = repository.save_work_card(_work_card(), body="正文")
    second = repository.save_work_card(_work_card(), body="重复导入正文")
    assert first == second
    assert len(list(first.parent.glob("*.md"))) == 1
    _, body = repository.load_work_card("creator_a", "work-demo")
    assert body == "正文\n"


def test_repository_requires_overwrite_for_existing_card(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    repository.save_work_card(_work_card())
    changed = _work_card()
    changed.title = "新标题"
    with pytest.raises(KnowledgeConflictError):
        repository.save_work_card(changed)
    repository.save_work_card(changed, overwrite=True)
    loaded, _ = repository.load_work_card("creator_a", "work-demo")
    assert loaded.title == "新标题"


def test_repository_keeps_accounts_isolated_and_blocks_path_traversal(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    repository.save_work_card(_work_card())
    other = WorkCard(work_id="work-other", account_id="creator_b", title="另一账号作品")
    repository.save_work_card(other)
    assert [card.account_id for card in repository.list_work_cards("creator_a")] == ["creator_a"]
    assert [card.account_id for card in repository.list_work_cards("creator_b")] == ["creator_b"]
    with pytest.raises(ObsidianRepositoryError):
        repository.account_dir("../escape")
    with pytest.raises(ObsidianRepositoryError):
        repository.read_account_note("creator_a", filename="../secret.md")


def test_repository_roundtrip_cognition_card_and_env_config(tmp_path, monkeypatch) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    card = CognitionCard(
        cognition_id="cog-demo",
        account_id="creator_a",
        title="结构性解释",
        claim="先看结构，再看情绪。",
        evidence_work_ids=["work-demo"],
        confidence=0.8,
    )
    repository.save_cognition_card(card, body="认知卡说明")
    loaded, body = repository.load_cognition_card("creator_a", "cog-demo")
    assert loaded.claim == card.claim
    assert body == "认知卡说明\n"
    monkeypatch.setenv("ACCOUNT_KNOWLEDGE_VAULT_PATH", str(tmp_path / "env-vault"))
    assert ObsidianRepository.from_env().vault_path == (tmp_path / "env-vault").resolve()


def test_repository_env_config_does_not_guess_path(monkeypatch) -> None:
    monkeypatch.delenv("ACCOUNT_KNOWLEDGE_VAULT_PATH", raising=False)
    with pytest.raises(VaultConfigError, match="不会猜测"):
        ObsidianRepository.from_env()


def test_initialize_account_creates_governed_directory_layout(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    account_note = repository.initialize_account("creator_a", account_name="测试账号")
    assert account_note.name == "account.md"
    assert (repository.vault_path / "账号知识库" / "accounts" / "creator_a" / "raw_works").is_dir()
    assert (repository.vault_path / "账号知识库" / "generated" / "approved").is_dir()
    assert repository.initialize_account("creator_a") == account_note


def test_approved_copy_writeback_requires_explicit_handoff(tmp_path) -> None:
    repository = ObsidianRepository(tmp_path / "vault")
    payload = {
        "handoff_type": "v2_manual_approval",
        "draft_id": "draft-v2-demo",
        "topic_plan_id": "topic-v2-demo",
        "style_profile_id": "style-a",
        "account_id": "creator_a",
        "approved_copy": "人工批准的优秀文案",
        "approved_at": "2026-08-27T00:00:00+00:00",
    }
    path = repository.save_approved_copy_note(payload)
    assert path.is_file()
    raw = path.read_text(encoding="utf-8")
    assert "knowledge_status: APPROVED" in raw
    with pytest.raises(ObsidianRepositoryError, match="v2_manual_approval"):
        repository.save_approved_copy_note({**payload, "handoff_type": "draft"})
