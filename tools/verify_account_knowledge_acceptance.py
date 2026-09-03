"""账号知识库 V2 的离线验收报告工具。

读取本地索引、topic plan、草稿和可选的人工期望召回样本，检查来源追溯、
账号隔离、C 级过滤和人工审核门禁。该工具不读取密钥、不调用模型、不修改
Obsidian Vault；真实模型/人工验收仍需单独执行。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workflow_1256.account_knowledge.indexer import LocalKnowledgeIndex  # noqa: E402
from workflow_1256.account_knowledge.retrieval import AccountKnowledgeRetriever  # noqa: E402
from workflow_1256.copywriting_knowledge_v2 import validate_copywriting_draft  # noqa: E402
from workflow_1256.topic_knowledge_v2 import validate_topic_plan  # noqa: E402


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON 必须是对象：{path}")
    return dict(value)


def _check_topic_plan(topic_plan: Mapping[str, Any]) -> dict[str, Any]:
    plan = validate_topic_plan(topic_plan)
    refs = list(plan.get("evidence_refs") or [])
    valid_refs = [
        ref
        for ref in refs
        if isinstance(ref, Mapping)
        and str(ref.get("account_id") or "").strip()
        and str(ref.get("work_id") or "").strip()
        and str(ref.get("source_url") or "").strip()
    ]
    lenses = [item for item in (plan.get("author_lenses") or []) if isinstance(item, Mapping)]
    accounts = [str(item.get("account_id") or "").strip() for item in lenses]
    return {
        "status": plan.get("status"),
        "evidence_count": len(refs),
        "traceable_evidence_count": len(valid_refs),
        "evidence_trace_rate": round(len(valid_refs) / len(refs), 4) if refs else 0.0,
        "author_lens_count": len(lenses),
        "author_account_isolation": len(accounts) == len(set(accounts)),
        "not_blocked": plan.get("status") != "BLOCKED",
    }


def _check_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    value = validate_copywriting_draft(draft)
    claims = [item for item in (value.get("claim_trace") or []) if isinstance(item, Mapping)]
    traceable = 0
    untraceable_claims: list[str] = []
    for claim in claims:
        source_type = str(claim.get("source_type") or "").strip().lower()
        if source_type == "news":
            ok = bool(claim.get("source_refs") or claim.get("source_url"))
        elif source_type == "knowledge":
            ok = bool(claim.get("knowledge_refs"))
        else:
            ok = bool(claim.get("source_refs") or claim.get("knowledge_refs"))
        if ok:
            traceable += 1
        else:
            untraceable_claims.append(str(claim.get("claim") or "未命名 claim"))
    refs = [item for item in (value.get("knowledge_refs") or []) if isinstance(item, Mapping)]
    valid_refs = [item for item in refs if str(item.get("account_id") or "").strip() and str(item.get("work_id") or "").strip()]
    return {
        "review_status": value.get("review_status"),
        "claim_count": len(claims),
        "traceable_claim_count": traceable,
        "claim_trace_rate": round(traceable / len(claims), 4) if claims else 0.0,
        "untraceable_claims": untraceable_claims,
        "knowledge_ref_count": len(refs),
        "valid_knowledge_ref_count": len(valid_refs),
        "no_approved_copy": "approved_copy" not in value,
        "review_gate": value.get("review_status") in {"REVIEW_PENDING", "BLOCKED"},
    }


def _check_retrieval(index: LocalKnowledgeIndex, expected: Mapping[str, Any] | None) -> dict[str, Any]:
    queries = list(expected.get("queries") or []) if isinstance(expected, Mapping) else []
    normal_c_hits = 0
    cases: list[dict[str, Any]] = []
    retriever = AccountKnowledgeRetriever(index)
    for item in queries:
        if not isinstance(item, Mapping):
            continue
        account_id = str(item.get("account_id") or "").strip()
        query = str(item.get("query") or "").strip()
        expected_ids = {str(value).strip() for value in (item.get("expected_work_ids") or []) if str(value).strip()}
        results = retriever.retrieve_account(account_id=account_id, query=query, top_k=5)
        work_ids = {str(result["record"].get("work_id") or "") for result in results}
        c_hits = [result for result in results if str(result["record"].get("content_grade") or "").upper() == "C"]
        normal_c_hits += len(c_hits)
        cases.append({
            "account_id": account_id,
            "query": query,
            "expected_work_ids": sorted(expected_ids),
            "top5_work_ids": sorted(work_ids),
            "hit": bool(expected_ids & work_ids) if expected_ids else True,
            "c_grade_hits": len(c_hits),
        })
    hits = sum(1 for case in cases if case["hit"])
    return {
        "query_count": len(cases),
        "hit_count": hits,
        "hit_rate": round(hits / len(cases), 4) if cases else None,
        "normal_retrieval_c_grade_hits": normal_c_hits,
        "cases": cases,
    }


def build_report(*, index: LocalKnowledgeIndex, topic_plan: Mapping[str, Any] | None = None, draft: Mapping[str, Any] | None = None, expected: Mapping[str, Any] | None = None, min_recall: float = 0.7) -> dict[str, Any]:
    checks: dict[str, bool] = {
        "index_has_records": bool(index.records),
    }
    metrics: dict[str, Any] = {"index_record_count": len(index.records)}
    if topic_plan is not None:
        topic = _check_topic_plan(topic_plan)
        metrics["topic_plan"] = topic
        checks.update({
            "topic_not_blocked": topic["not_blocked"],
            "topic_evidence_traceable": topic["evidence_count"] == topic["traceable_evidence_count"],
            "topic_account_isolation": topic["author_account_isolation"],
        })
    if draft is not None:
        draft_metrics = _check_draft(draft)
        metrics["draft"] = draft_metrics
        checks.update({
            "draft_claims_traceable": draft_metrics["claim_count"] == draft_metrics["traceable_claim_count"],
            "draft_no_approved_copy": draft_metrics["no_approved_copy"],
            "draft_review_gate": draft_metrics["review_gate"],
        })
    retrieval = _check_retrieval(index, expected)
    metrics["retrieval"] = retrieval
    checks["normal_retrieval_c_grade_zero"] = retrieval["normal_retrieval_c_grade_hits"] == 0
    if retrieval["hit_rate"] is not None:
        checks["retrieval_minimum_recall"] = retrieval["hit_rate"] >= min_recall
    return {
        "status": "ready" if all(checks.values()) else "blocked",
        "mode": "offline_acceptance_report",
        "checks": checks,
        "metrics": metrics,
        "message": "离线验收条件通过；仍需真实模型、人工审核和 approved_copy 交付" if all(checks.values()) else "离线验收未通过，请根据 checks 修正数据或门禁",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="账号知识库 V2 离线验收报告")
    parser.add_argument("--index", required=True)
    parser.add_argument("--topic-plan")
    parser.add_argument("--draft")
    parser.add_argument("--expected")
    parser.add_argument("--min-recall", type=float, default=0.7)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        index = LocalKnowledgeIndex.load(Path(args.index))
        report = build_report(
            index=index,
            topic_plan=_load(Path(args.topic_plan)) if args.topic_plan else None,
            draft=_load(Path(args.draft)) if args.draft else None,
            expected=_load(Path(args.expected)) if args.expected else None,
            min_recall=args.min_recall,
        )
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if report["status"] == "ready" else 2
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
