"""从作品卡和本地原文生成账号级 Writing-DNA 候选版。

本模块故意不调用模型。它只把已有作品卡的结构化字段、原文的确定性统计
和可回溯的作品编号整理成五份 Obsidian 笔记，状态固定为
``PROVISIONAL_OFFLINE``，不能冒充 L3-L5 深度认知蒸馏结果。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from .contracts import KnowledgeContractError, WorkCard, normalize_text, utc_now
from .surface_analysis import analyze_texts


OFFLINE_CANDIDATE_STATUS = "PROVISIONAL_OFFLINE"
OFFLINE_CANDIDATE_METHOD = "deterministic_offline_candidate"
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z][A-Za-z0-9_-]{1,63}|\d+(?:\.\d+)?")
_STRUCTURE_FIELDS = (
    ("title", "标题/钩子"),
    ("facts", "事实/材料"),
    ("core_thesis", "核心命题"),
    ("angle", "解读角度"),
    ("reasoning_steps", "推理链"),
    ("counterpoints", "反方/限制"),
    ("conclusion_boundary", "结论边界"),
)


def _work_value(work: Mapping[str, Any] | WorkCard) -> dict[str, Any]:
    if isinstance(work, WorkCard):
        return work.to_dict()
    if not isinstance(work, Mapping):
        raise KnowledgeContractError("离线候选版只接受作品卡对象")
    return dict(work)


def _texts(value: Any) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple)) else []
    result: list[str] = []
    for item in values:
        text = normalize_text(item)
        if text and text not in result:
            result.append(text)
    return result


def _grouped_exact(values_by_work: Mapping[str, str], *, limit: int = 12) -> list[dict[str, Any]]:
    """按完全相同的作品卡字段分组，保留每组证据作品编号。"""

    groups: dict[str, list[str]] = defaultdict(list)
    for work_id, value in values_by_work.items():
        text = normalize_text(value)
        if text:
            groups[text].append(work_id)
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]
    total = max(1, len(values_by_work))
    return [
        {
            "value": value,
            "support_count": len(work_ids),
            "coverage": round(len(work_ids) / total, 4),
            "evidence_work_ids": sorted(work_ids),
            "basis": "作品卡字段完全匹配",
        }
        for value, work_ids in ordered
    ]


def _counter_with_evidence(values_by_work: Mapping[str, Sequence[str]], *, limit: int = 20) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = defaultdict(list)
    counts: Counter[str] = Counter()
    for work_id, values in values_by_work.items():
        for value in values:
            text = normalize_text(value)
            if text:
                counts[text] += 1
                if work_id not in groups[text]:
                    groups[text].append(work_id)
    ordered = sorted(counts, key=lambda item: (-counts[item], item))[:limit]
    total = max(1, len(values_by_work))
    return [
        {
            "value": value,
            "support_count": counts[value],
            "work_coverage": round(len(groups[value]) / total, 4),
            "evidence_work_ids": sorted(groups[value]),
            "basis": "作品卡字段精确计数",
        }
        for value in ordered
    ]


def _title_style(titles: Sequence[str]) -> dict[str, Any]:
    values = [str(item or "").strip() for item in titles if str(item or "").strip()]
    return {
        "sample_count": len(values),
        "average_title_characters": round(sum(len(item) for item in values) / max(1, len(values)), 2),
        "question_mark_titles": sum("？" in item or "?" in item for item in values),
        "exclamation_mark_titles": sum("！" in item or "!" in item for item in values),
        "dash_titles": sum("—" in item or "-" in item for item in values),
        "note": "标题统计只描述表面形式，不推断点击率因果。",
    }


def _structure_candidates(cards: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for card in cards:
        present = tuple(
            label
            for field, label in _STRUCTURE_FIELDS
            if (bool(normalize_text(card.get(field))) if field in {"title", "core_thesis", "angle", "conclusion_boundary"} else bool(_texts(card.get(field))))
        )
        if present:
            groups[present].append(str(card["work_id"]))
    total = max(1, len(cards))
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    return [
        {
            "template_id": f"offline-sequence-{index}",
            "pattern": " → ".join(pattern),
            "support_count": len(work_ids),
            "coverage": round(len(work_ids) / total, 4),
            "evidence_work_ids": sorted(work_ids),
            "basis": "作品卡字段是否存在的确定性统计；不是模型归纳的写作规范。",
        }
        for index, (pattern, work_ids) in enumerate(ordered[:10], start=1)
    ]


def build_offline_candidate_bundle(
    *,
    account_id: str,
    works: Sequence[Mapping[str, Any] | WorkCard],
    source_texts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """构造可写回五份账号笔记的离线候选 bundle。

    ``source_texts`` 的键必须是作品卡的 ``work_id``。缺失原文不会在这里被
    偷悄悄补齐；调用层应在真实运行前执行完整原文门禁，并把缺失编号报告给用户。
    """

    account = normalize_text(account_id)
    if not account:
        raise KnowledgeContractError("离线候选版需要 account_id")
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in works:
        card = _work_value(raw)
        work_id = normalize_text(card.get("work_id"))
        card_account = normalize_text(card.get("account_id"))
        if not work_id or card_account != account:
            raise KnowledgeContractError("作品卡的 account_id/work_id 与候选账号不一致")
        if work_id in seen:
            raise KnowledgeContractError(f"候选版发现重复 work_id：{work_id}")
        if str(card.get("knowledge_status") or "").upper() in {"ARCHIVED", "REJECTED"}:
            continue
        seen.add(work_id)
        cards.append(card)
    if not cards:
        raise KnowledgeContractError("离线候选版至少需要一篇有效作品卡")

    text_map = {str(key): str(value or "") for key, value in (source_texts or {}).items()}
    ordered_ids = sorted(seen)
    source_values = [text_map.get(work_id, "") for work_id in ordered_ids if text_map.get(work_id, "").strip()]
    surface = analyze_texts(source_values)
    missing_source_ids = [work_id for work_id in ordered_ids if not text_map.get(work_id, "").strip()]

    angle_by_work = {str(card["work_id"]): str(card.get("angle") or "") for card in cards}
    thesis_by_work = {str(card["work_id"]): str(card.get("core_thesis") or "") for card in cards}
    reasoning_by_work = {str(card["work_id"]): _texts(card.get("reasoning_steps")) for card in cards}
    evidence_by_work = {str(card["work_id"]): _texts(card.get("evidence_types")) for card in cards}
    field_presence = {
        field: sum(
            bool(normalize_text(card.get(field))) if field in {"core_thesis", "angle", "conclusion_boundary"} else bool(_texts(card.get(field)))
            for card in cards
        )
        for field, _ in _STRUCTURE_FIELDS
    }
    evidence_counts = Counter(item for values in evidence_by_work.values() for item in values)
    source_ref_count = sum(bool(normalize_text(card.get("source_ref"))) for card in cards)
    source_url_count = sum(bool(normalize_text(card.get("source_url"))) for card in cards)
    evidence_ids = ordered_ids

    language = {
        "candidate_status": OFFLINE_CANDIDATE_STATUS,
        "source_article_count": int(surface.get("article_count") or 0),
        "source_statistics": surface.get("aggregate") or {},
        "title_style": _title_style([str(card.get("title") or "") for card in cards]),
        "field_coverage": field_presence,
        "top_surface_terms": list((surface.get("aggregate") or {}).get("top_terms") or [])[:30],
        "missing_source_work_ids": missing_source_ids,
        "note": "由本地原文确定性统计和作品卡字段生成；不等同于深度语言风格判断。",
    }

    structures = _structure_candidates(cards)
    structures.append(
        {
            "template_id": "offline-reasoning-step-count",
            "pattern": "推理链步骤数的观察值",
            "average_steps": round(sum(len(values) for values in reasoning_by_work.values()) / max(1, len(cards)), 2),
            "support_count": len(cards),
            "coverage": 1.0,
            "evidence_work_ids": evidence_ids,
            "basis": "作品卡 reasoning_steps 长度统计；不能单独决定新稿结构。",
        }
    )

    cognition: list[dict[str, Any]] = []
    for item in _grouped_exact(thesis_by_work, limit=10):
        cognition.append(
            {
                "title": "核心命题候选",
                "claim": item["value"],
                "reasoning_pattern": "该命题在作品卡 core_thesis 字段中重复出现；需人工判断是否属于账号稳定认知。",
                "evidence_work_ids": item["evidence_work_ids"],
                "confidence": item["coverage"],
                "basis": item["basis"],
            }
        )
    for item in _grouped_exact(angle_by_work, limit=10):
        cognition.append(
            {
                "title": "解读角度候选",
                "claim": item["value"],
                "reasoning_pattern": "该角度在作品卡 angle 字段中重复出现；不把单篇角度直接升级为账号规则。",
                "evidence_work_ids": item["evidence_work_ids"],
                "confidence": item["coverage"],
                "basis": item["basis"],
            }
        )
    for item in _counter_with_evidence(reasoning_by_work, limit=15):
        cognition.append(
            {
                "title": "推理步骤候选",
                "claim": item["value"],
                "reasoning_pattern": "该步骤文本在 reasoning_steps 中出现；仅作为检索线索。",
                "evidence_work_ids": item["evidence_work_ids"],
                "confidence": item["work_coverage"],
                "basis": item["basis"],
            }
        )

    material = {
        "candidate_status": OFFLINE_CANDIDATE_STATUS,
        "evidence_type_counts": dict(sorted(evidence_counts.items(), key=lambda item: (-item[1], item[0]))),
        "average_facts_per_work": round(sum(len(_texts(card.get("facts"))) for card in cards) / max(1, len(cards)), 2),
        "average_counterpoints_per_work": round(sum(len(_texts(card.get("counterpoints"))) for card in cards) / max(1, len(cards)), 2),
        "source_ref_coverage": round(source_ref_count / max(1, len(cards)), 4),
        "source_url_coverage": round(source_url_count / max(1, len(cards)), 4),
        "evidence_work_ids": evidence_ids,
        "note": "材料策略只统计作品卡已有 evidence_types/facts 等字段，不补造权威性结论。",
    }
    visual = {
        "candidate_status": OFFLINE_CANDIDATE_STATUS,
        "markdown_statistics": surface.get("aggregate") or {},
        "visual_evidence": "当前输入是作品原文与作品卡，未提供视频画面/截图素材，不能推断真实视觉风格。",
        "evidence_work_ids": evidence_ids,
    }

    top_angles = [item["value"] for item in _grouped_exact(angle_by_work, limit=3)]
    top_types = [f"{key}（{value}次）" for key, value in sorted(evidence_counts.items(), key=lambda item: (-item[1], item[0]))[:5]]
    writing_dna = (
        "> ⚠️ 本文档是 `PROVISIONAL_OFFLINE` 候选版，不是最终账号级 Writing-DNA。\n\n"
        "本候选版由 %d 篇有效作品卡、%d 篇可读取原文的确定性统计生成。它只保留可追溯的字段证据，"
        "不把统计结果包装成模型推断。\n\n"
        "## 当前可复用线索\n\n"
        "- 结构：优先参考《文章结构模板.md》中的字段覆盖序列，再回看对应作品编号。\n"
        "- 角度：当前出现的候选角度包括：%s。使用前必须结合新闻事实重新判断。\n"
        "- 材料：当前 evidence_types 的高频项包括：%s。\n"
        "- 视觉：没有画面证据，不生成视觉模仿规则。\n\n"
        "## 使用边界\n\n"
        "候选笔记可用于本地检索、提示词预填和人工复核，不能直接作为账号身份或成稿约束；"
        "账号级深度蒸馏恢复后，应由内容分析模型基于同一批作品升级，并保留这些作品证据编号。\n"
    ) % (len(cards), len(source_values), "；".join(top_angles) or "暂无", "、".join(top_types) or "暂无")

    return {
        "schema_version": 1,
        "account_id": account,
        "sample_count": len(cards),
        "status": OFFLINE_CANDIDATE_STATUS,
        "distillation_method": OFFLINE_CANDIDATE_METHOD,
        "reliability_note": "仅由作品卡字段与本地原文统计生成；不能视为语义 Writing-DNA，需模型和人工审核。",
        "language_dna": language,
        "structure_templates": structures,
        "cognition_framework": cognition,
        "material_strategy": material,
        "visual_style": visual,
        "writing_dna": writing_dna,
        "evidence_work_ids": evidence_ids,
        "missing_source_work_ids": missing_source_ids,
        "distilled_at": utc_now(),
    }


def write_offline_candidate_notes(repository, bundle: Mapping[str, Any], *, overwrite: bool = False) -> list:
    """使用统一五文件命名写入候选笔记，避免产生第二套账号入口。"""

    if str(bundle.get("status") or "") != OFFLINE_CANDIDATE_STATUS:
        raise KnowledgeContractError("候选笔记写入器只接受 PROVISIONAL_OFFLINE bundle")
    from .distillation import write_account_distillation

    return write_account_distillation(repository, bundle, overwrite=overwrite)


__all__ = [
    "OFFLINE_CANDIDATE_METHOD",
    "OFFLINE_CANDIDATE_STATUS",
    "build_offline_candidate_bundle",
    "write_offline_candidate_notes",
]
