"""文案生产固定执行链的运行时清单。

生产代码仍复用既有采集、账号知识和 style_package 实现；本模块负责把每个
阶段及其来源版本显式写进结果，避免调用顺序只存在于模型提示词里。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


ORCHESTRATOR_VERSION = "copywriting-orchestrator-v1"
STAGE_CHAIN = (
    "knowledge_source_auto_identification",
    "obsidian_raw_ingest",
    "human_confirmation",
    "cangjie_method_package",
    "nuwa_thinking_package",
    "writing_dna_style_package",
    "asset_human_review_lock",
    "copywriting_draft",
    "human_writing_gate",
    "human_final_review",
)


def _source_summary(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        result.append(
            {
                "source_id": str(record.get("source_id") or ""),
                "source_version": str(record.get("source_version") or "source-v1"),
                "status": str(record.get("status") or ""),
                "obsidian_path": str(record.get("obsidian_path") or ""),
            }
        )
    return result


def build_execution_manifest(
    *,
    source_records: Sequence[Mapping[str, Any]] = (),
    style_profile_record: Mapping[str, Any] | None = None,
    account_knowledge_context: Mapping[str, Any] | None = None,
    task_context: Mapping[str, Any] | None = None,
    draft_status: str = "PENDING",
    human_writing_status: str = "PENDING",
    final_review_status: str = "PENDING",
) -> dict[str, Any]:
    """构造可持久化的阶段清单；不执行模型或网络请求。"""

    sources = _source_summary(source_records)
    profile = dict(style_profile_record or {})
    account = dict(account_knowledge_context or {})
    source_ready = bool(sources) and all(item["obsidian_path"] for item in sources)
    source_confirmed = bool(sources) and all(
        item["status"] not in {"PENDING_CONFIRMATION", "FAILED"} for item in sources
    )
    style_approved = str(profile.get("review_status") or "").upper() == "APPROVED"
    account_available = bool(account.get("available")) if account else not bool(profile.get("account_id"))
    # The current workspace deliberately keeps cangjie/nuwa as separate
    # boundary adapters until their upstream runtimes are installed and
    # verified.  A source being confirmed proves the input contract only; it
    # must not be reported as a completed third-party distillation stage.
    method_status = "BOUNDARY_ONLY" if source_confirmed else ("NOT_USED" if not sources else "BLOCKED")
    thinking_status = "BOUNDARY_ONLY" if account_available else ("NOT_USED" if not profile.get("account_id") else "BLOCKED")
    stages = [
        {"stage": STAGE_CHAIN[0], "status": "COMPLETED" if sources else "NOT_USED"},
        {"stage": STAGE_CHAIN[1], "status": "COMPLETED" if source_ready else ("NOT_USED" if not sources else "BLOCKED")},
        {"stage": STAGE_CHAIN[2], "status": "COMPLETED" if source_confirmed else ("NOT_USED" if not sources else "PENDING")},
        {"stage": STAGE_CHAIN[3], "status": method_status},
        {"stage": STAGE_CHAIN[4], "status": thinking_status},
        {"stage": STAGE_CHAIN[5], "status": "READY" if style_approved else "BLOCKED"},
        {"stage": STAGE_CHAIN[6], "status": "LOCKED" if style_approved and account_available else "PENDING"},
        {"stage": STAGE_CHAIN[7], "status": str(draft_status or "PENDING").upper()},
        {"stage": STAGE_CHAIN[8], "status": str(human_writing_status or "PENDING").upper()},
        {"stage": STAGE_CHAIN[9], "status": str(final_review_status or "PENDING").upper()},
    ]
    return {
        "schema_version": "copywriting-orchestration-v1",
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "stage_order": list(STAGE_CHAIN),
        "stages": stages,
        "source_records": sources,
        "asset_versions": {
            "style_profile_id": str(profile.get("style_profile_id") or ""),
            "style_profile_version": str(
                profile.get("style_profile_version") or profile.get("approved_at") or ""
            ),
            "account_id": str(profile.get("account_id") or ""),
        },
        # Keep the user-selected task dimensions beside the stage manifest so
        # a later reviewer can reproduce the exact writing request without
        # reading model prompts or relying on browser state.
        "task_context": {
            str(key): value
            for key, value in dict(task_context or {}).items()
            if str(key) in {
                "topic",
                "target_platform",
                "audience",
                "thinking_package_id",
                "publish_format",
                "writing_mode",
            }
        },
        "human_writing_policy": "identity_safe_fallback_when_upstream_unverified",
        "adapter_policy": {
            "cangjie": "boundary_only_unverified",
            "nuwa": "boundary_only_unverified",
            "human_writing": "safe_fallback_when_upstream_unverified",
        },
    }


__all__ = ["ORCHESTRATOR_VERSION", "STAGE_CHAIN", "build_execution_manifest"]
