from __future__ import annotations

from dataclasses import replace

import tools.run_account_knowledge_lab as lab_cli
from workflow_1256.account_knowledge.corpus_import import discover_corpus_files, import_corpus
from workflow_1256.account_knowledge.obsidian_repository import ObsidianRepository


def test_import_corpus_copies_raw_sources_and_writes_meta(tmp_path) -> None:
    corpus = tmp_path / "author" / "raw"
    corpus.mkdir(parents=True)
    (corpus / "2026-08-01 观察.md").write_text(
        "---\ntitle: 监管变化\ndate: 2026-08-01\ntags: [AI, 监管]\n---\n\n# 监管变化\n\n最近很多人只看到情绪，但真正变化的是利益结构。" * 4,
        encoding="utf-8",
    )
    (corpus / "短评.txt").write_text("一个没有 frontmatter 的完整作品，先确认事实，再分析利益结构。" * 5, encoding="utf-8")
    (corpus / "Writing-DNA.md").write_text("不应被当作原始作品", encoding="utf-8")
    (corpus / "_meta" ).mkdir()
    (corpus / "_meta" / "old.json").write_text("{}", encoding="utf-8")

    repository = ObsidianRepository(tmp_path / "vault")
    report = import_corpus(repository, account_id="creator_a", corpus_path=corpus, platform="douyin")
    assert report.discovered == 2
    assert report.imported == 2
    assert report.failed == 0
    cards = repository.list_work_cards("creator_a")
    assert len(cards) == 2
    assert all(card.knowledge_status == "RAW" for card in cards)
    for card in cards:
        assert (repository.account_dir("creator_a") / card.source_ref).is_file()
        metadata = repository.load_work_metadata("creator_a", card.work_id)
        assert metadata["work_id"] == card.work_id
        assert metadata["word_count"] > 0
        assert metadata["notable"].startswith("由导入器自动抽取")


def test_import_corpus_is_idempotent_on_second_run(tmp_path) -> None:
    corpus = tmp_path / "raw"
    corpus.mkdir()
    source = corpus / "文章.md"
    source.write_text("# 同一篇作品\n\n内容足够长，用于验证重复导入不产生第二份作品卡。" * 3, encoding="utf-8")
    repository = ObsidianRepository(tmp_path / "vault")
    first = import_corpus(repository, account_id="creator_a", corpus_path=corpus)
    second = import_corpus(repository, account_id="creator_a", corpus_path=corpus)
    assert first.imported == 1
    assert second.imported == 0
    assert second.skipped == 1
    assert len(repository.list_work_cards("creator_a")) == 1


def test_discover_corpus_ignores_distilled_documents(tmp_path) -> None:
    root = tmp_path / "author"
    (root / "raw-corpus").mkdir(parents=True)
    (root / "raw-corpus" / "作品.txt").write_text("正文", encoding="utf-8")
    (root / "raw-corpus" / "语言DNA.md").write_text("规则", encoding="utf-8")
    assert [path.name for path in discover_corpus_files(root)] == ["作品.txt"]


def test_import_corpus_preserves_exporter_provenance_metadata(tmp_path) -> None:
    corpus = tmp_path / "raw"
    corpus.mkdir()
    (corpus / "123456.md").write_text(
        '---\n{"title":"导出作品","source_url":"https://www.douyin.com/video/123456",'
        '"video_id":"123456","source_origin":"douyin-monitor.sqlite.videos",'
        '"source_quality":"description_plus_local_transcript","transcript_status":"local_whisper",'
        '"source_record_id":10,"duration_seconds":5.05}\n---\n\n作品正文足够长，用于验证来源元数据被保留。',
        encoding="utf-8",
    )
    repository = ObsidianRepository(tmp_path / "vault")
    report = import_corpus(repository, account_id="creator_a", corpus_path=corpus)
    assert report.imported == 1
    work_id = repository.list_work_cards("creator_a")[0].work_id
    metadata = repository.load_work_metadata("creator_a", work_id)
    assert metadata["video_id"] == "123456"
    assert metadata["source_origin"] == "douyin-monitor.sqlite.videos"
    assert metadata["source_quality"] == "description_plus_local_transcript"
    assert metadata["transcript_status"] == "local_whisper"
    assert metadata["source_record_id"] == 10
    assert metadata["duration_seconds"] == 5.05


def test_live_work_distill_preserves_importer_provenance_metadata(tmp_path, monkeypatch, capsys) -> None:
    corpus = tmp_path / "raw"
    corpus.mkdir()
    source = corpus / "123456.md"
    source.write_text(
        "---\n"
        '{"title":"一篇完整作品","source_url":"https://www.douyin.com/video/123456",'
        '"date":"2026-08-01T00:00:00+00:00","video_id":"123456",'
        '"source_quality":"description_plus_local_transcript",'
        '"transcript_status":"local_whisper"}\n'
        "---\n\n正文内容足够长，用于验证蒸馏覆盖作品卡时保留来源证据。\n",
        encoding="utf-8",
    )
    vault = tmp_path / "vault"
    repository = ObsidianRepository(vault)
    report = import_corpus(repository, account_id="creator_a", corpus_path=corpus, platform="douyin")
    assert report.imported == 1
    original = repository.list_work_cards("creator_a")[0]

    class FakeProvider:
        def distill_work_card(self, **kwargs):
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

    monkeypatch.setattr(lab_cli.AccountKnowledgeModelProvider, "from_runtime_config", lambda **kwargs: FakeProvider())
    exit_code = lab_cli.main(
        [
            "--vault",
            str(vault),
            "distill-work-live",
            "--account-id",
            "creator_a",
            "--title",
            original.title,
            "--source-file",
            str(source),
            "--source-url",
            original.source_url,
            "--platform",
            original.platform,
            "--published-at",
            original.published_at,
            "--source-ref",
            original.source_ref,
            "--overwrite",
        ]
    )
    assert exit_code == 0
    capsys.readouterr()
    metadata = repository.load_work_metadata("creator_a", original.work_id)
    assert metadata["video_id"] == "123456"
    assert metadata["source_quality"] == "description_plus_local_transcript"
    assert metadata["transcript_status"] == "local_whisper"
    assert metadata["distilled_by"] == "CONTENT_ANALYZER_ARK_MODEL"
