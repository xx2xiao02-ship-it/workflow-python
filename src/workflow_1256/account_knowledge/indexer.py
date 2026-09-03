"""无需外部服务的本地知识索引。

首版采用轻量级词项索引，中文使用单字与双字词并行，英文/数字使用
连续词。它不是向量数据库替代品，但可在没有模型和网络时稳定运行，
并且保留每条结果的账号、作品和评级来源。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .contracts import CognitionCard, WorkCard


_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z][A-Za-z0-9_-]{1,63}|\d+(?:\.\d+)?")


def tokenize(text: str) -> list[str]:
    """生成稳定、可解释的中英文词项，不把原文发送到外部服务。"""

    normalized = str(text or "").lower()
    tokens = _TOKEN_RE.findall(normalized)
    cjk = "".join(char for char in normalized if "\u4e00" <= char <= "\u9fff")
    tokens.extend(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return [token for token in tokens if token.strip()]


@dataclass
class IndexRecord:
    doc_id: str
    account_id: str
    kind: str
    title: str
    text: str
    content_grade: str = "B"
    retrieval_weight: float = 0.35
    knowledge_status: str = "RAW"
    source_url: str = ""
    work_id: str = ""
    cognition_id: str = ""
    evidence_work_ids: list[str] | None = None
    metadata: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class LocalKnowledgeIndex:
    def __init__(self) -> None:
        self._records: dict[str, IndexRecord] = {}
        self._postings: dict[str, set[str]] = {}

    @property
    def records(self) -> tuple[IndexRecord, ...]:
        return tuple(self._records.values())

    def clear(self) -> None:
        self._records.clear()
        self._postings.clear()

    def rebuild_from_repository(self, repository, *, account_ids: Iterable[str] | None = None) -> int:
        """从 Obsidian 仓库完整重建索引，返回文档数量。"""

        self.clear()
        accounts = list(account_ids) if account_ids is not None else list(repository.list_account_ids())
        for account_id in accounts:
            for card in repository.list_work_cards(account_id):
                _, body = repository.load_work_card(account_id, card.work_id)
                try:
                    metadata = repository.load_work_metadata(account_id, card.work_id)
                except Exception:
                    # 旧作品卡可能还没有 _meta；不能因此阻断整库重建。
                    metadata = {}
                self.add_work_card(card, body=body, metadata=metadata)
            for card in repository.list_cognition_cards(account_id):
                _, body = repository.load_cognition_card(account_id, card.cognition_id)
                self.add_cognition_card(card, body=body)
        return len(self._records)

    def add_work_card(
        self,
        card: WorkCard,
        *,
        body: str = "",
        metadata: Mapping | None = None,
    ) -> IndexRecord:
        card.validate()
        source_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        source_metadata.setdefault("published_at", card.published_at)
        record = IndexRecord(
            doc_id=f"work:{card.account_id}:{card.work_id}",
            account_id=card.account_id,
            kind="work",
            title=card.title,
            text=" ".join(
                [card.title, body, card.core_thesis, card.angle, *card.facts, *card.reasoning_steps, *card.counterpoints]
            ),
            content_grade=card.content_grade,
            retrieval_weight=float(card.retrieval_weight or 0.0),
            knowledge_status=card.knowledge_status,
            source_url=card.source_url,
            work_id=card.work_id,
            metadata={
                **source_metadata,
                "angle": card.angle,
                "core_thesis": card.core_thesis,
                "facts": list(card.facts),
                "reasoning_steps": list(card.reasoning_steps),
            },
        )
        return self._add(record)

    def add_cognition_card(self, card: CognitionCard, *, body: str = "") -> IndexRecord:
        card.validate()
        record = IndexRecord(
            doc_id=f"cognition:{card.account_id}:{card.cognition_id}",
            account_id=card.account_id,
            kind="cognition",
            title=card.title,
            text=" ".join([card.title, body, card.claim, card.reasoning_pattern, *card.suitable_events]),
            content_grade="A",
            retrieval_weight=float(card.confidence),
            knowledge_status=card.knowledge_status,
            cognition_id=card.cognition_id,
            evidence_work_ids=list(card.evidence_work_ids),
            metadata={
                "claim": card.claim,
                "reasoning_pattern": card.reasoning_pattern,
                "suitable_events": list(card.suitable_events),
            },
        )
        return self._add(record)

    def _add(self, record: IndexRecord) -> IndexRecord:
        if record.doc_id in self._records:
            self.remove(record.doc_id)
        self._records[record.doc_id] = record
        for token in set(tokenize(record.text)):
            self._postings.setdefault(token, set()).add(record.doc_id)
        return record

    def remove(self, doc_id: str) -> None:
        record = self._records.pop(doc_id, None)
        if record is None:
            return
        for token in set(tokenize(record.text)):
            docs = self._postings.get(token)
            if docs is None:
                continue
            docs.discard(doc_id)
            if not docs:
                self._postings.pop(token, None)

    def search(
        self,
        *,
        account_id: str,
        query: str,
        top_k: int = 5,
        allowed_content_grades: Iterable[str] = ("S", "A", "B"),
        allowed_statuses: Iterable[str] = ("APPROVED", "PUBLISHED"),
        include_cognition: bool = True,
        domain: str = "",
        published_after: str = "",
        apply_retrieval_weight: bool = True,
    ) -> list[dict]:
        account = str(account_id or "").strip()
        terms = tokenize(query)
        if not account or not terms or top_k <= 0:
            return []
        grades = {str(value).upper() for value in allowed_content_grades}
        statuses = {str(value).upper() for value in allowed_statuses}
        domain_filter = str(domain or "").strip().lower()
        published_after_filter = str(published_after or "").strip()
        candidate_ids: set[str] = set()
        for term in set(terms):
            candidate_ids.update(self._postings.get(term, set()))
        total_docs = max(1, len(self._records))
        scored: list[dict] = []
        for doc_id in candidate_ids:
            record = self._records[doc_id]
            if record.account_id != account or record.knowledge_status.upper() not in statuses:
                continue
            if record.kind == "work" and record.content_grade.upper() not in grades:
                continue
            if record.kind == "cognition" and not include_cognition:
                continue
            if domain_filter and not _record_matches_domain(record, domain_filter):
                continue
            if published_after_filter and not _record_matches_published_after(record, published_after_filter):
                continue
            record_tokens = tokenize(record.text)
            if not record_tokens:
                continue
            frequencies = {term: record_tokens.count(term) for term in set(terms)}
            score = 0.0
            for term, frequency in frequencies.items():
                if not frequency:
                    continue
                document_frequency = len(self._postings.get(term, set()))
                idf = math.log((total_docs + 1) / (document_frequency + 1)) + 1.0
                score += idf * (frequency / (frequency + 1.0))
            if record.title and any(term in tokenize(record.title) for term in terms):
                score *= 1.25
            if apply_retrieval_weight:
                score *= max(0.0, min(1.0, record.retrieval_weight))
            if score <= 0:
                continue
            scored.append({"score": round(score, 6), "record": record.to_dict()})
        scored.sort(key=lambda item: (-item["score"], item["record"]["doc_id"]))
        return scored[:top_k]

    def dump(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "records": [record.to_dict() for record in self.records]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "LocalKnowledgeIndex":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("本地知识索引读取失败") from exc
        if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
            raise ValueError("不支持的本地知识索引版本")
        index = cls()
        records = payload.get("records")
        if not isinstance(records, list):
            raise ValueError("本地知识索引 records 必须是数组")
        for raw in records:
            if not isinstance(raw, Mapping):
                raise ValueError("本地知识索引记录格式错误")
            evidence = raw.get("evidence_work_ids")
            record = IndexRecord(
                doc_id=str(raw.get("doc_id") or ""),
                account_id=str(raw.get("account_id") or ""),
                kind=str(raw.get("kind") or ""),
                title=str(raw.get("title") or ""),
                text=str(raw.get("text") or ""),
                content_grade=str(raw.get("content_grade") or "B"),
                retrieval_weight=float(raw.get("retrieval_weight") or 0.0),
                knowledge_status=str(raw.get("knowledge_status") or "RAW"),
                source_url=str(raw.get("source_url") or ""),
                work_id=str(raw.get("work_id") or ""),
                cognition_id=str(raw.get("cognition_id") or ""),
                evidence_work_ids=list(evidence) if isinstance(evidence, list) else None,
                metadata=dict(raw.get("metadata")) if isinstance(raw.get("metadata"), Mapping) else None,
            )
            if not record.doc_id or not record.account_id or record.kind not in {"work", "cognition"}:
                raise ValueError("本地知识索引记录缺少必要字段")
            index._add(record)
        return index


__all__ = ["IndexRecord", "LocalKnowledgeIndex", "tokenize"]


def _metadata_values(metadata: Mapping | None) -> list[str]:
    """提取可用于领域过滤的人工/导入元数据，不把整篇原文当作领域标签。"""

    if not isinstance(metadata, Mapping):
        return []
    values: list[str] = []
    for key in ("domain", "domains", "topic", "topic_tags", "tags", "column", "article_type", "suitable_events"):
        value = metadata.get(key)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item).strip().lower() for item in value if str(item).strip())
        elif value is not None and str(value).strip():
            values.append(str(value).strip().lower())
    return values


def _record_matches_domain(record: IndexRecord, domain: str) -> bool:
    values = _metadata_values(record.metadata)
    return any(domain in value or value in domain for value in values)


def _record_matches_published_after(record: IndexRecord, published_after: str) -> bool:
    metadata = record.metadata if isinstance(record.metadata, Mapping) else {}
    published_at = str(metadata.get("published_at") or metadata.get("date") or "").strip()
    # 缺少日期时不能证明满足时间门禁，因此过滤掉；ISO 日期/时间可直接稳定比较。
    return bool(published_at) and published_at >= published_after
