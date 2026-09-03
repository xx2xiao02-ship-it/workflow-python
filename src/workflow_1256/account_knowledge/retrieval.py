"""账号隔离的知识召回与来源追踪。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .indexer import LocalKnowledgeIndex


@dataclass(frozen=True)
class EvidenceRef:
    account_id: str
    kind: str
    work_id: str = ""
    cognition_id: str = ""
    source_url: str = ""
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "kind": self.kind,
            "work_id": self.work_id,
            "cognition_id": self.cognition_id,
            "source_url": self.source_url,
            "score": self.score,
        }


class AccountKnowledgeRetriever:
    """封装 LocalKnowledgeIndex，确保每次请求只允许一个 account_id。"""

    def __init__(self, index: LocalKnowledgeIndex) -> None:
        self.index = index

    def retrieve_account(
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
        results = self.index.search(
            account_id=account_id,
            query=query,
            top_k=top_k,
            allowed_content_grades=allowed_content_grades,
            allowed_statuses=allowed_statuses,
            include_cognition=include_cognition,
            domain=domain,
            published_after=published_after,
            apply_retrieval_weight=apply_retrieval_weight,
        )
        output: list[dict] = []
        for item in results:
            record = item["record"]
            # 二次检查用于防止未来替换索引实现后出现跨账号污染。
            if record.get("account_id") != account_id:
                continue
            evidence = EvidenceRef(
                account_id=record["account_id"],
                kind=record["kind"],
                work_id=record.get("work_id", ""),
                cognition_id=record.get("cognition_id", ""),
                source_url=record.get("source_url", ""),
                score=float(item["score"]),
            )
            output.append({"score": item["score"], "record": record, "evidence_ref": evidence.to_dict()})
        return output

    def retrieve_many_accounts(
        self,
        *,
        account_ids: Iterable[str],
        query: str,
        top_k_per_account: int = 5,
        allowed_content_grades: Iterable[str] = ("S", "A", "B"),
        allowed_statuses: Iterable[str] = ("APPROVED", "PUBLISHED"),
        domain: str = "",
        published_after: str = "",
    ) -> dict[str, list[dict]]:
        """先按账号分别召回，返回字典而不是把原文混成一个集合。"""

        result: dict[str, list[dict]] = {}
        for account_id in account_ids:
            account = str(account_id or "").strip()
            if not account or account in result:
                continue
            result[account] = self.retrieve_account(
                account_id=account,
                query=query,
                top_k=top_k_per_account,
                allowed_content_grades=allowed_content_grades,
                allowed_statuses=allowed_statuses,
                domain=domain,
                published_after=published_after,
            )
        return result

    def retrieve_negative_cases(
        self,
        *,
        account_id: str,
        query: str,
        top_k: int = 5,
        published_after: str = "",
    ) -> list[dict]:
        """只检索 C 级作品的避错通道；不会混入正常观点召回。"""

        return self.retrieve_account(
            account_id=account_id,
            query=query,
            top_k=top_k,
            allowed_content_grades=("C",),
            include_cognition=False,
            published_after=published_after,
            apply_retrieval_weight=False,
        )


__all__ = ["AccountKnowledgeRetriever", "EvidenceRef"]
