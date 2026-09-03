"""把本地索引接成 V2 选题/文案实验链路的编排边界。"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from ..copywriting_knowledge_v2 import build_copywriting_draft
from ..topic_knowledge_v2 import EventCard, RetrievalPolicy, build_topic_plan
from .contracts import KnowledgeContractError, normalize_text
from .indexer import LocalKnowledgeIndex
from .retrieval import AccountKnowledgeRetriever


class CopywritingProvider(Protocol):
    def draft(self, *, topic_plan: Mapping[str, Any], style_profile_id: str, requirements: Mapping[str, Any]) -> Mapping[str, Any]:
        """返回 draft_copy、claim_trace 等结构化字段，不直接写 approved_copy。"""


class CopywritingProviderRequired(RuntimeError):
    pass


def _query_event(event: EventCard) -> str:
    return " ".join([event.title, *event.verified_facts, *event.controversies])


def _lens_from_results(account_id: str, results: list[dict]) -> dict[str, Any]:
    evidence_refs: list[dict[str, Any]] = []
    angles: list[str] = []
    reasoning_patterns: list[str] = []
    for item in results:
        record = item["record"]
        metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
        work_id = str(record.get("work_id") or "").strip()
        if work_id:
            evidence_refs.append(
                {
                    "account_id": account_id,
                    "work_id": work_id,
                    "source_url": str(record.get("source_url") or ""),
                    "score": item["score"],
                }
            )
        elif record.get("kind") == "cognition":
            for evidence_work_id in record.get("evidence_work_ids") or []:
                evidence_id = normalize_text(evidence_work_id)
                if evidence_id:
                    evidence_refs.append(
                        {
                            "account_id": account_id,
                            "work_id": evidence_id,
                            "cognition_id": record.get("cognition_id", ""),
                            "source_url": str(record.get("source_url") or ""),
                            "score": item["score"],
                        }
                    )
        for value in (metadata.get("angle"), metadata.get("core_thesis"), metadata.get("claim")):
            text = normalize_text(value)
            if text and text not in angles:
                angles.append(text)
        for value in (metadata.get("reasoning_pattern"),):
            text = normalize_text(value)
            if text and text not in reasoning_patterns:
                reasoning_patterns.append(text)
    return {
        "account_id": account_id,
        "angles": angles,
        "reasoning_patterns": reasoning_patterns,
        "evidence_refs": evidence_refs,
    }


def _overlap(left: str, right: str) -> float:
    left_terms = set(normalize_text(left))
    right_terms = set(normalize_text(right))
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / max(1, len(left_terms | right_terms))


def build_topic_plan_from_index(
    *,
    index: LocalKnowledgeIndex,
    event_card: EventCard,
    target_account_id: str,
    reference_account_ids: list[str] | tuple[str, ...] = (),
    retrieval_policy: RetrievalPolicy | None = None,
) -> dict[str, Any]:
    """分别检索账号后形成可审核 topic plan，不做跨账号原文拼接。"""

    event_card.validate()
    policy = (retrieval_policy or RetrievalPolicy()).validate()
    accounts: list[str] = []
    for value in [target_account_id, *reference_account_ids]:
        account = normalize_text(value)
        if account and account not in accounts:
            accounts.append(account)
    if not accounts:
        raise KnowledgeContractError("至少需要一个目标账号")
    retrieved = AccountKnowledgeRetriever(index).retrieve_many_accounts(
        account_ids=accounts,
        query=_query_event(event_card),
        top_k_per_account=policy.top_k_per_account,
        allowed_content_grades=policy.allowed_content_grades,
        allowed_statuses=policy.allowed_statuses,
        domain=policy.domain,
        published_after=policy.published_after,
    )
    lenses = [_lens_from_results(account, retrieved.get(account, [])) for account in accounts]
    evidence_refs: list[dict[str, Any]] = []
    for lens in lenses:
        evidence_refs.extend(lens["evidence_refs"])
    angles = [angle for lens in lenses for angle in lens["angles"]]
    consensus: list[str] = []
    conflicts: list[str] = []
    if len(angles) >= 2:
        for index, left in enumerate(angles):
            for right in angles[index + 1 :]:
                if _overlap(left, right) >= 0.35 and left not in consensus:
                    consensus.append(left)
                elif _overlap(left, right) < 0.15 and right not in conflicts:
                    conflicts.append(right)
    recommended_angle = angles[0] if angles else ""
    new_synthesis = []
    if recommended_angle and len(lenses) > 1:
        new_synthesis.append("多账号检索已完成，需人工/模型确认共识、冲突和本事件的新角度。")
    missing = []
    if not evidence_refs:
        missing.append("没有召回已审核的账号作品证据")
    if not recommended_angle:
        missing.append("没有召回可用的解读角度")
    return build_topic_plan(
        event_card=event_card,
        target_account_id=target_account_id,
        reference_account_ids=list(reference_account_ids),
        recommended_angle=recommended_angle,
        author_lenses=lenses,
        consensus=consensus,
        conflicts=conflicts,
        new_synthesis=new_synthesis,
        evidence_refs=evidence_refs,
        missing_evidence=missing,
        retrieval_policy=policy,
        status="REVIEW_REQUIRED",
    )


def run_copywriting_provider(
    provider: CopywritingProvider | None,
    *,
    topic_plan: Mapping[str, Any],
    style_profile_id: str,
    requirements: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """显式调用注入的文案 provider；未注入时保持阻断。"""

    if provider is None:
        raise CopywritingProviderRequired("未注入文案 provider；当前不会自动调用模型")
    result = provider.draft(
        topic_plan=topic_plan,
        style_profile_id=style_profile_id,
        requirements=dict(requirements or {}),
    )
    if not isinstance(result, Mapping):
        raise KnowledgeContractError("文案 provider 必须返回对象")
    return build_copywriting_draft(
        topic_plan=topic_plan,
        style_profile_id=style_profile_id,
        draft_copy=str(result.get("draft_copy") or ""),
        audience=str((requirements or {}).get("audience") or ""),
        platform=str((requirements or {}).get("platform") or ""),
        target_duration_seconds=(requirements or {}).get("target_duration_seconds"),
        claim_trace=list(result.get("claim_trace") or []),
        knowledge_refs=list(result.get("knowledge_refs") or []),
        style_application=list(result.get("style_application") or []),
        unsupported_claims=list(result.get("unsupported_claims") or []),
        review_flags=list(result.get("review_flags") or []),
        review_status=str(result.get("review_status") or "REVIEW_PENDING"),
    )


__all__ = [
    "CopywritingProvider",
    "CopywritingProviderRequired",
    "build_topic_plan_from_index",
    "run_copywriting_provider",
]
