"""选题中心 V2 的纯契约层。

这里不执行新闻抓取或模型综合，只负责把事实卡、检索结果和人工/模型
生成的候选角度组装成可审核的 topic plan，保证事实来源与账号知识来源
分离，且永远不会产出旧链路的 approved_copy。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

from .account_knowledge.contracts import KnowledgeContractError, normalize_text, utc_now


SCHEMA_VERSION = 1
TOPIC_PLAN_STATUSES = frozenset({"READY", "REVIEW_REQUIRED", "BLOCKED"})


def _http_url(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise KnowledgeContractError(f"{field_name} 必须是 HTTP(S) 地址")
    return text


@dataclass
class EventCard:
    event_id: str
    title: str
    verified_facts: list[str]
    source_refs: list[dict[str, str]]
    persons: list[str] = field(default_factory=list)
    occurred_at: str = ""
    controversies: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> "EventCard":
        self.event_id = normalize_text(self.event_id)
        self.title = normalize_text(self.title)
        if not self.event_id or not self.title:
            raise KnowledgeContractError("event_id 和 title 不能为空")
        self.verified_facts = [normalize_text(value) for value in self.verified_facts if normalize_text(value)]
        if not self.verified_facts:
            raise KnowledgeContractError("event_card 至少需要一条已核验事实")
        clean_refs: list[dict[str, str]] = []
        for ref in self.source_refs:
            if not isinstance(ref, Mapping):
                raise KnowledgeContractError("source_refs 必须是对象数组")
            source_id = normalize_text(ref.get("source_id"))
            url = _http_url(str(ref.get("url") or ""), "source_refs.url")
            clean_refs.append({"source_id": source_id or url, "url": url})
        if not clean_refs:
            raise KnowledgeContractError("event_card 必须包含新闻来源")
        self.source_refs = clean_refs
        self.persons = [normalize_text(value) for value in self.persons if normalize_text(value)]
        self.controversies = [normalize_text(value) for value in self.controversies if normalize_text(value)]
        if self.schema_version != SCHEMA_VERSION:
            raise KnowledgeContractError("不支持的 event_card schema_version")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "title": self.title,
            "verified_facts": list(self.verified_facts),
            "source_refs": list(self.source_refs),
            "persons": list(self.persons),
            "occurred_at": self.occurred_at,
            "controversies": list(self.controversies),
        }


@dataclass
class RetrievalPolicy:
    top_k_per_account: int = 5
    allowed_content_grades: tuple[str, ...] = ("S", "A", "B")
    allowed_statuses: tuple[str, ...] = ("APPROVED", "PUBLISHED")
    domain: str = ""
    published_after: str = ""

    def validate(self) -> "RetrievalPolicy":
        if not isinstance(self.top_k_per_account, int) or not 1 <= self.top_k_per_account <= 50:
            raise KnowledgeContractError("top_k_per_account 必须在 1 到 50 之间")
        allowed = {str(value).upper() for value in self.allowed_content_grades}
        if not allowed or "C" in allowed:
            raise KnowledgeContractError("正常选题检索不得包含 C 级内容")
        self.allowed_content_grades = tuple(sorted(allowed))
        self.allowed_statuses = tuple(sorted({str(value).upper() for value in self.allowed_statuses}))
        return self


def _topic_plan_id(event_id: str, target_account_id: str, reference_account_ids: list[str]) -> str:
    raw = "\x1f".join([event_id, target_account_id, *reference_account_ids])
    return "topic-v2-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def build_topic_plan(
    *,
    event_card: EventCard | Mapping[str, Any],
    target_account_id: str,
    reference_account_ids: list[str] | tuple[str, ...] = (),
    recommended_angle: str = "",
    author_lenses: list[Mapping[str, Any]] | None = None,
    consensus: list[str] | None = None,
    conflicts: list[str] | None = None,
    new_synthesis: list[str] | None = None,
    evidence_refs: list[Mapping[str, Any]] | None = None,
    fact_risks: list[str] | None = None,
    missing_evidence: list[str] | None = None,
    retrieval_policy: RetrievalPolicy | None = None,
    status: str = "READY",
) -> dict[str, Any]:
    card = event_card if isinstance(event_card, EventCard) else EventCard(**dict(event_card))
    card.validate()
    target = normalize_text(target_account_id)
    refs = [normalize_text(value) for value in reference_account_ids if normalize_text(value)]
    if not target:
        raise KnowledgeContractError("target_account_id 不能为空")
    if status not in TOPIC_PLAN_STATUSES:
        raise KnowledgeContractError("topic plan status 无效")
    policy = (retrieval_policy or RetrievalPolicy()).validate()
    lenses = [dict(value) for value in (author_lenses or []) if isinstance(value, Mapping)]
    for lens in lenses:
        account_id = normalize_text(lens.get("account_id"))
        if not account_id:
            raise KnowledgeContractError("author_lenses.account_id 不能为空")
        lens["account_id"] = account_id
        lens["evidence_refs"] = [dict(ref) for ref in lens.get("evidence_refs", []) if isinstance(ref, Mapping)]
    clean_evidence = [dict(ref) for ref in (evidence_refs or []) if isinstance(ref, Mapping)]
    for ref in clean_evidence:
        if not normalize_text(ref.get("account_id")) or not normalize_text(ref.get("work_id")):
            raise KnowledgeContractError("evidence_refs 必须包含 account_id 和 work_id")
        if ref.get("source_url"):
            _http_url(str(ref["source_url"]), "evidence_refs.source_url")
    final_status = status
    if not recommended_angle or not clean_evidence:
        final_status = "REVIEW_REQUIRED" if status == "READY" else status
    return {
        "schema_version": SCHEMA_VERSION,
        "topic_plan_id": _topic_plan_id(card.event_id, target, refs),
        "event_card": card.to_dict(),
        "target_account_id": target,
        "reference_account_ids": refs,
        "retrieval_policy": {
            "top_k_per_account": policy.top_k_per_account,
            "allowed_content_grades": list(policy.allowed_content_grades),
            "allowed_statuses": list(policy.allowed_statuses),
            "domain": policy.domain,
            "published_after": policy.published_after,
        },
        "recommended_angle": normalize_text(recommended_angle),
        "author_lenses": lenses,
        "consensus": [normalize_text(value) for value in (consensus or []) if normalize_text(value)],
        "conflicts": [normalize_text(value) for value in (conflicts or []) if normalize_text(value)],
        "new_synthesis": [normalize_text(value) for value in (new_synthesis or []) if normalize_text(value)],
        "evidence_refs": clean_evidence,
        "fact_risks": [normalize_text(value) for value in (fact_risks or []) if normalize_text(value)],
        "missing_evidence": [normalize_text(value) for value in (missing_evidence or []) if normalize_text(value)],
        "status": final_status,
        "created_at": utc_now(),
    }


def validate_topic_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise KnowledgeContractError("topic plan 必须是对象")
    if "approved_copy" in value:
        raise KnowledgeContractError("选题 V2 不得产出 approved_copy")
    status = str(value.get("status") or "")
    if status not in TOPIC_PLAN_STATUSES:
        raise KnowledgeContractError("topic plan status 无效")
    if not str(value.get("topic_plan_id") or "").strip():
        raise KnowledgeContractError("topic_plan_id 不能为空")
    return dict(value)


__all__ = ["EventCard", "RetrievalPolicy", "TOPIC_PLAN_STATUSES", "build_topic_plan", "validate_topic_plan"]
