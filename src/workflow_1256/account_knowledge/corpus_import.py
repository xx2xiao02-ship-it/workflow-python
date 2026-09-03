"""完整作品语料导入器。

导入阶段只复制用户明确指定的本地 .md/.txt 作品，不做语义臆测：标题、
日期和标签尽量从 frontmatter 读取，其余元数据标记为自动抽取，等待后续
人工/模型审核。内部生成稿和 README 不会被自动当作博主作品导入。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import WorkCard, build_work_id, content_sha256, normalize_text
from .obsidian_repository import KnowledgeConflictError, ObsidianRepository


SUPPORTED_SUFFIXES = frozenset({".md", ".txt"})
_FRONTMATTER_MARKER = "---"


@dataclass
class CorpusImportItem:
    source_path: str
    work_id: str = ""
    title: str = ""
    status: str = ""
    message: str = ""
    source_ref: str = ""


@dataclass
class CorpusImportReport:
    account_id: str
    corpus_path: str
    discovered: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    items: list[CorpusImportItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "corpus_path": self.corpus_path,
            "discovered": self.discovered,
            "imported": self.imported,
            "skipped": self.skipped,
            "failed": self.failed,
            "items": [asdict(item) for item in self.items],
        }


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    normalized = str(text or "")
    if not normalized.startswith("---\n"):
        return {}, normalized
    marker = normalized.find("\n---", len("---\n"))
    if marker < 0:
        return {}, normalized
    raw = normalized[len("---\n") : marker]
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(raw)
    except ImportError:
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            loaded = {}
    except Exception:
        loaded = {}
    return (dict(loaded) if isinstance(loaded, Mapping) else {}), normalized[marker + 4 :].lstrip("\n")


def _title(frontmatter: Mapping[str, Any], body: str, path: Path) -> str:
    for key in ("title", "name"):
        value = normalize_text(frontmatter.get(key))
        if value:
            return value
    for line in body.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            return normalize_text(match.group(1).strip("# "))
    return normalize_text(path.stem)


def _list_value(frontmatter: Mapping[str, Any], key: str) -> list[str]:
    value = frontmatter.get(key)
    if isinstance(value, str):
        return [normalize_text(item) for item in re.split(r"[,，、]", value) if normalize_text(item)]
    if isinstance(value, (list, tuple)):
        return [normalize_text(item) for item in value if normalize_text(item)]
    return []


def _infer_article_type(title: str, body: str) -> str:
    text = title + body[:500]
    if any(token in text for token in ("访谈", "对话", "采访")):
        return "访谈"
    if any(token in text for token in ("问：", "答：", "Q&A", "问答")):
        return "访谈"
    if any(token in text for token in ("复盘", "拆解", "深度")):
        return "深度分析"
    if any(token in text for token in ("总结", "综述")):
        return "综述"
    return "观察"


def _infer_hook_type(body: str) -> str:
    first = next((normalize_text(line) for line in body.splitlines() if normalize_text(line)), "")
    if "？" in first or "?" in first:
        return "问题式"
    if re.search(r"\d", first):
        return "数据式"
    if first.startswith(("今天", "最近", "当", "就在")):
        return "场景式"
    return "观点式"


def _infer_structure(body: str) -> str:
    if re.search(r"(?:问：|答：|Q[:：])", body, flags=re.IGNORECASE):
        return "Q&A"
    if re.search(r"\b20\d{2}\b", body) and body.count("20") >= 2:
        return "时间线"
    if any(token in body for token in ("对比", "相比", "不同的是")):
        return "对比式"
    return "总-分-总"


def _infer_source_types(body: str, frontmatter: Mapping[str, Any]) -> list[str]:
    explicit = _list_value(frontmatter, "source_types")
    if explicit:
        return explicit
    values: list[str] = []
    if any(token in body for token in ("采访", "我看到", "亲历", "现场")):
        values.append("一手素材")
    if any(token in body for token in ("数据", "报告", "论文", "统计")):
        values.append("公开资料")
    if any(token in body for token in ("案例", "比如", "例如")):
        values.append("案例对比")
    return values or ["公开资料"]


def discover_corpus_files(corpus_path: Path | str) -> list[Path]:
    root = Path(corpus_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"语料目录不存在：{root}")
    children = {child.name.lower(): child for child in root.iterdir() if child.is_dir()}
    if root.name.lower() not in {"raw", "raw-corpus"}:
        for candidate in ("raw", "raw-corpus"):
            if candidate in children:
                root = children[candidate]
                break
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if any(part.startswith("_") for part in path.relative_to(root).parts[:-1]):
            continue
        if path.name.lower() in {"readme.md", "writing-dna.md", "语言dna.md", "文章结构模板.md", "写作视角与认知框架.md", "视觉风格指南.md"}:
            continue
        files.append(path)
    return sorted(files, key=lambda item: str(item).lower())


def import_corpus(
    repository: ObsidianRepository,
    *,
    account_id: str,
    corpus_path: Path | str,
    platform: str = "",
    default_content_grade: str = "B",
    copy_raw: bool = True,
) -> CorpusImportReport:
    """导入指定语料，重复执行不会复制第二份作品卡。"""

    files = discover_corpus_files(corpus_path)
    report = CorpusImportReport(account_id=account_id, corpus_path=str(Path(corpus_path).resolve()), discovered=len(files))
    repository.initialize_account(account_id)
    for path in files:
        item = CorpusImportItem(source_path=str(path))
        report.items.append(item)
        try:
            text = path.read_text(encoding="utf-8")
            frontmatter, body = _parse_frontmatter(text)
            title = _title(frontmatter, body, path)
            source_url = normalize_text(frontmatter.get("source_url") or frontmatter.get("url"))
            published_at = normalize_text(frontmatter.get("date") or frontmatter.get("published_at"))
            work_id = build_work_id(
                account_id=account_id,
                title=title,
                source_url=source_url,
                published_at=published_at,
                content=text,
            )
            suffix = path.suffix.lower()
            source_ref = f"raw_works/{work_id}{suffix}"
            card = WorkCard(
                work_id=work_id,
                account_id=account_id,
                title=title,
                platform=platform,
                source_url=source_url,
                published_at=published_at,
                source_ref=source_ref,
                content_hash=content_sha256(text),
                content_grade=default_content_grade.upper(),
                knowledge_status="RAW",
                manual_reviewed=False,
            ).validate()
            existing_card = (repository.account_dir(account_id) / "work_cards" / f"{work_id}.md").is_file()
            if copy_raw:
                repository.save_raw_source(account_id, source_ref, path.read_bytes())
            repository.save_work_card(card, body="")
            metadata = {
                "schema_version": 1,
                "work_id": work_id,
                "account_id": account_id,
                "title": title,
                "date": published_at,
                "author": normalize_text(frontmatter.get("author")),
                "column": normalize_text(frontmatter.get("column")),
                "article_type": normalize_text(frontmatter.get("article_type")) or _infer_article_type(title, body),
                "topic_tags": _list_value(frontmatter, "topic_tags") or _list_value(frontmatter, "tags"),
                "hook_type": normalize_text(frontmatter.get("hook_type")) or _infer_hook_type(body),
                "structure_pattern": normalize_text(frontmatter.get("structure_pattern")) or _infer_structure(body),
                "source_types": _infer_source_types(body, frontmatter),
                "word_count": len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", body)),
                "notable": "由导入器自动抽取，L2-L6 待人工/模型审核",
                "content_hash": card.content_hash,
                "source_ref": source_ref,
            }
            # Preserve importer-specific provenance when the source exporter
            # provides it (for example douyin-monitor video IDs and transcript
            # quality). Unknown frontmatter remains intentionally ignored.
            for key in ("video_id", "source_origin", "source_quality", "transcript_status"):
                value = normalize_text(frontmatter.get(key))
                if value:
                    metadata[key] = value
            if frontmatter.get("source_record_id") is not None:
                metadata["source_record_id"] = frontmatter.get("source_record_id")
            if frontmatter.get("duration_seconds") is not None:
                metadata["duration_seconds"] = frontmatter.get("duration_seconds")
            repository.save_work_metadata(account_id, work_id, metadata)
            item.work_id, item.title, item.source_ref = work_id, title, source_ref
            if existing_card:
                report.skipped += 1
                item.status = "skipped"
                item.message = "作品编号已存在，保留原有作品卡"
            else:
                report.imported += 1
                item.status = "imported"
                item.message = "已写入原文、作品卡和 _meta"
        except KnowledgeConflictError as exc:
            report.skipped += 1
            item.status = "skipped"
            item.message = str(exc)
        except Exception as exc:
            report.failed += 1
            item.status = "failed"
            item.message = f"{type(exc).__name__}: {exc}"
    return report


__all__ = [
    "CorpusImportItem",
    "CorpusImportReport",
    "SUPPORTED_SUFFIXES",
    "discover_corpus_files",
    "import_corpus",
]
