"""L1/L6 的确定性文本与 Markdown 统计，不声称完成认知蒸馏。"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from .corpus_import import discover_corpus_files


_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z][A-Za-z0-9_-]{1,63}|\d+(?:\.\d+)?")
_SENTENCE_RE = re.compile(r"[^。！？!?；;]+[。！？!?；;]?", flags=re.S)


def _article_stats(text: str) -> dict[str, Any]:
    body = str(text or "")
    sentences = [item.strip() for item in _SENTENCE_RE.findall(body) if item.strip()]
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", body) if item.strip()]
    tokens = _TOKEN_RE.findall(body.lower())
    chars = len(re.sub(r"\s+", "", body))
    return {
        "characters": chars,
        "word_count": len(tokens),
        "sentence_count": len(sentences),
        "average_sentence_length": round(sum(len(item) for item in sentences) / max(1, len(sentences)), 2),
        "short_sentence_ratio": round(sum(len(item) <= 15 for item in sentences) / max(1, len(sentences)), 4),
        "long_sentence_ratio": round(sum(len(item) >= 50 for item in sentences) / max(1, len(sentences)), 4),
        "paragraph_count": len(paragraphs),
        "paragraph_average_sentences": round(len(sentences) / max(1, len(paragraphs)), 2),
        "heading_count": len(re.findall(r"^#{1,6}\s+", body, flags=re.M)),
        "bold_count": len(re.findall(r"\*\*.+?\*\*", body, flags=re.S)),
        "quote_count": len(re.findall(r"^>\s+", body, flags=re.M)),
        "image_count": len(re.findall(r"!\[[^\]]*\]\([^)]*\)", body)),
        "punctuation": {
            "question": body.count("？") + body.count("?"),
            "exclamation": body.count("！") + body.count("!"),
            "dash": body.count("—") + body.count("--"),
            "parentheses": body.count("（") + body.count("(") + body.count("）") + body.count(")"),
            "quote": body.count("“") + body.count("”") + body.count('"'),
        },
        "top_terms": Counter(tokens).most_common(30),
    }


def analyze_texts(texts: Iterable[str]) -> dict[str, Any]:
    articles = [_article_stats(text) for text in texts]
    if not articles:
        return {"article_count": 0, "articles": [], "aggregate": {}}
    numeric_keys = (
        "characters",
        "word_count",
        "sentence_count",
        "average_sentence_length",
        "short_sentence_ratio",
        "long_sentence_ratio",
        "paragraph_count",
        "paragraph_average_sentences",
        "heading_count",
        "bold_count",
        "quote_count",
        "image_count",
    )
    aggregate = {
        key: round(sum(float(article[key]) for article in articles) / len(articles), 4) for key in numeric_keys
    }
    punctuation = Counter()
    terms = Counter()
    for article in articles:
        punctuation.update(article["punctuation"])
        terms.update(dict(article["top_terms"]))
    aggregate["punctuation_total"] = dict(punctuation)
    aggregate["top_terms"] = terms.most_common(100)
    return {"article_count": len(articles), "articles": articles, "aggregate": aggregate}


def analyze_corpus(corpus_path: Path | str) -> dict[str, Any]:
    files = discover_corpus_files(corpus_path)
    texts = [path.read_text(encoding="utf-8") for path in files]
    report = analyze_texts(texts)
    report["files"] = [str(path) for path in files]
    report["reliability_note"] = "这是 L1/L6 的确定性统计；不等同于 L3-L5 认知框架蒸馏。"
    return report


__all__ = ["analyze_corpus", "analyze_texts"]
