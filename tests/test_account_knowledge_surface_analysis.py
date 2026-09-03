from __future__ import annotations

from workflow_1256.account_knowledge.surface_analysis import analyze_corpus, analyze_texts


def test_surface_analysis_reports_l1_and_l6_metrics_without_claiming_cognition() -> None:
    report = analyze_texts([
        "# 标题\n\n这是第一句。第二句？\n\n**重点**\n\n![证据](image.png)",
        "短文开头。然后补充公开资料。",
    ])
    assert report["article_count"] == 2
    assert report["aggregate"]["sentence_count"] >= 3
    assert report["aggregate"]["heading_count"] > 0
    assert report["aggregate"]["image_count"] > 0


def test_surface_analysis_uses_same_corpus_discovery_rules(tmp_path) -> None:
    root = tmp_path / "author" / "raw"
    root.mkdir(parents=True)
    (root / "a.md").write_text("# A\n\n正文。" * 20, encoding="utf-8")
    (root / "Writing-DNA.md").write_text("不计入原始语料", encoding="utf-8")
    report = analyze_corpus(tmp_path / "author")
    assert report["article_count"] == 1
    assert "L1/L6" in report["reliability_note"]
