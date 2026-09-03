from __future__ import annotations

from dataclasses import replace

import tools.run_account_knowledge_batch_live as batch
from workflow_1256.account_knowledge.corpus_import import import_corpus
from workflow_1256.account_knowledge.obsidian_repository import ObsidianRepository


def test_batch_distill_updates_same_card_and_preserves_provenance(tmp_path, monkeypatch, capsys) -> None:
    corpus = tmp_path / "raw"
    corpus.mkdir()
    (corpus / "123456.md").write_text(
        '---\n{"title":"完整作品","source_url":"https://www.douyin.com/video/123456",'
        '"date":"2026-08-01T00:00:00+00:00","video_id":"123456",'
        '"source_quality":"description_plus_local_transcript","transcript_status":"local_whisper"}\n'
        "---\n\n作品正文足够长，用来验证批量蒸馏写回。",
        encoding="utf-8",
    )
    vault = tmp_path / "vault"
    repository = ObsidianRepository(vault)
    assert import_corpus(repository, account_id="creator_a", corpus_path=corpus, platform="douyin").imported == 1
    original = repository.list_work_cards("creator_a")[0]
    calls = []

    class FakeProvider:
        def distill_work_card(self, **kwargs):
            calls.append(kwargs["title"])
            return replace(
                original,
                facts=["模型提取事实"],
                core_thesis="模型提取命题",
                angle="模型提取角度",
                knowledge_status="REVIEW_PENDING",
                distilled_by="CONTENT_ANALYZER_ARK_MODEL",
                prompt_version="account-knowledge-v1",
                distilled_at="2026-08-28T00:00:00+00:00",
            ).validate()

    monkeypatch.setattr(batch.AccountKnowledgeModelProvider, "from_runtime_config", lambda **kwargs: FakeProvider())
    first = batch.run_batch(vault=vault, account_id="creator_a", report_path=tmp_path / "report.json")
    capsys.readouterr()
    assert first["status"] == "ready"
    assert first["processed"] == 1
    assert first["failed"] == 0
    assert len(repository.list_work_cards("creator_a")) == 1
    card = repository.list_work_cards("creator_a")[0]
    assert card.work_id == original.work_id
    assert card.distilled_by == "CONTENT_ANALYZER_ARK_MODEL"
    metadata = repository.load_work_metadata("creator_a", card.work_id)
    assert metadata["video_id"] == "123456"
    assert metadata["source_quality"] == "description_plus_local_transcript"
    assert metadata["transcript_status"] == "local_whisper"

    second = batch.run_batch(vault=vault, account_id="creator_a", report_path=tmp_path / "report-2.json")
    capsys.readouterr()
    assert second["status"] == "ready"
    assert second["processed"] == 0
    assert second["skipped"] == 1
    assert len(calls) == 1
