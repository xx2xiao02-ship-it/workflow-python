"""文案创作 V2 的审核门禁契约。

该模块只组装和校验草稿；它不调用模型，不访问现有生产状态，也不允许
未经人工审核的草稿伪装成 approved_copy。
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .account_knowledge.contracts import KnowledgeContractError, normalize_text, utc_now
from .topic_knowledge_v2 import validate_topic_plan


SCHEMA_VERSION = 1
REVIEW_STATUSES = frozenset({"REVIEW_PENDING", "BLOCKED"})


def _draft_id(topic_plan_id: str, style_profile_id: str, draft_copy: str) -> str:
    raw = "\x1f".join([topic_plan_id, style_profile_id, draft_copy])
    return "draft-v2-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def build_copywriting_draft(
    *,
    topic_plan: Mapping[str, Any],
    style_profile_id: str,
    draft_copy: str,
    audience: str = "",
    platform: str = "",
    target_duration_seconds: float | None = None,
    claim_trace: list[Mapping[str, Any]] | None = None,
    knowledge_refs: list[Mapping[str, Any]] | None = None,
    style_application: list[str] | None = None,
    unsupported_claims: list[str] | None = None,
    review_flags: list[str] | None = None,
    review_status: str = "REVIEW_PENDING",
) -> dict[str, Any]:
    validated_plan = validate_topic_plan(topic_plan)
    style_id = normalize_text(style_profile_id)
    text = str(draft_copy or "").strip()
    if not style_id:
        raise KnowledgeContractError("style_profile_id 不能为空")
    if review_status not in REVIEW_STATUSES:
        raise KnowledgeContractError("V2 文案只能处于 REVIEW_PENDING 或 BLOCKED")
    if review_status == "REVIEW_PENDING" and not text:
        raise KnowledgeContractError("待审核文案不能为空")
    claims = [dict(item) for item in (claim_trace or []) if isinstance(item, Mapping)]
    refs = [dict(item) for item in (knowledge_refs or []) if isinstance(item, Mapping)]
    flags = [normalize_text(item) for item in (review_flags or []) if normalize_text(item)]
    unsupported = [normalize_text(item) for item in (unsupported_claims or []) if normalize_text(item)]
    if unsupported and "存在未追溯事实" not in flags:
        flags.append("存在未追溯事实")
    if unsupported and review_status == "REVIEW_PENDING":
        review_status = "BLOCKED"
    return {
        "schema_version": SCHEMA_VERSION,
        "draft_id": _draft_id(str(validated_plan["topic_plan_id"]), style_id, text),
        "topic_plan_id": validated_plan["topic_plan_id"],
        "target_account_id": validated_plan.get("target_account_id", ""),
        "style_profile_id": style_id,
        "draft_copy": text,
        "audience": normalize_text(audience),
        "platform": normalize_text(platform),
        "target_duration_seconds": target_duration_seconds,
        "claim_trace": claims,
        "knowledge_refs": refs,
        "style_application": [normalize_text(item) for item in (style_application or []) if normalize_text(item)],
        "unsupported_claims": unsupported,
        "review_flags": flags,
        "review_status": review_status,
        "created_at": utc_now(),
    }


def validate_copywriting_draft(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise KnowledgeContractError("文案草稿必须是对象")
    if "approved_copy" in value:
        raise KnowledgeContractError("V2 草稿不得包含 approved_copy")
    if str(value.get("review_status") or "") not in REVIEW_STATUSES:
        raise KnowledgeContractError("文案草稿审核状态无效")
    if not str(value.get("draft_id") or "").strip():
        raise KnowledgeContractError("draft_id 不能为空")
    return dict(value)


def promote_v2_draft(value: Mapping[str, Any], *, manual_approval: bool) -> dict[str, Any]:
    """显式人工批准后生成旧链路需要的交付对象，不直接写入旧仓库。"""

    draft = validate_copywriting_draft(value)
    if not manual_approval:
        raise KnowledgeContractError("必须提供 manual_approval=True 才能交付")
    if draft.get("review_status") == "BLOCKED":
        raise KnowledgeContractError("文案审核已阻断，不能交付")
    draft_copy = str(draft.get("draft_copy") or "").strip()
    if not draft_copy or draft.get("unsupported_claims") or not str(draft.get("target_account_id") or "").strip():
        raise KnowledgeContractError("文案仍有未追溯事实，不能交付")
    return {
        "handoff_type": "v2_manual_approval",
        "draft_id": draft["draft_id"],
        "topic_plan_id": draft["topic_plan_id"],
        "account_id": draft.get("target_account_id", ""),
        "style_profile_id": draft["style_profile_id"],
        "approved_copy": draft_copy,
        "approved_at": utc_now(),
    }


__all__ = [
    "REVIEW_STATUSES",
    "build_copywriting_draft",
    "promote_v2_draft",
    "validate_copywriting_draft",
]
