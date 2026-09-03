"""作品蒸馏的供应商边界与结果校验。

真正的模型调用必须由上层显式注入 provider；本模块默认只把已经得到的
结构化结果转成知识卡，不会偷偷调用网络或产生付费请求。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from .contracts import CognitionCard, KnowledgeContractError, WorkCard, build_work_id, content_sha256, normalize_text, utc_now


class DistillationProvider(Protocol):
    def distill(self, *, account_id: str, title: str, source_content: str, source_url: str = "") -> Mapping[str, Any]:
        """返回可被 build_work_card_from_distillation 校验的结构化结果。"""


class AccountDistillationProvider(Protocol):
    def distill_account(self, *, account_id: str, works: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        """返回 L1-L6 账号级蒸馏结果，必须自行携带作品证据编号。"""


class DistillationProviderRequired(RuntimeError):
    pass


def build_account_distillation(
    *, account_id: str, works: Sequence[Mapping[str, Any] | WorkCard], result: Mapping[str, Any]
) -> dict[str, Any]:
    """校验账号级 Writing DNA，样本不足时只标记为格式/流程演示。"""

    account = normalize_text(account_id)
    if not account or not isinstance(result, Mapping):
        raise KnowledgeContractError("账号蒸馏需要 account_id 和对象结果")
    work_ids: set[str] = set()
    for work in works:
        value = work.to_dict() if isinstance(work, WorkCard) else dict(work)
        work_id = normalize_text(value.get("work_id"))
        if work_id:
            work_ids.add(work_id)
    if not work_ids:
        raise KnowledgeContractError("账号蒸馏至少需要一篇带 work_id 的作品")
    cognition = result.get("cognition_framework")
    if cognition is not None and not isinstance(cognition, list):
        raise KnowledgeContractError("cognition_framework 必须是数组")
    for rule in cognition or []:
        if not isinstance(rule, Mapping) or not rule.get("evidence_work_ids"):
            raise KnowledgeContractError("每条认知规则必须记录 evidence_work_ids")
        unknown = set(str(item) for item in rule.get("evidence_work_ids") or []) - work_ids
        if unknown:
            raise KnowledgeContractError("认知规则引用了不在本账号语料中的作品")
    sample_count = len(work_ids)
    status = "READY_FOR_REVIEW" if sample_count >= 30 else "FORMAT_ONLY_REVIEW_REQUIRED"
    return {
        "schema_version": 1,
        "account_id": account,
        "sample_count": sample_count,
        "status": status,
        "reliability_note": "至少30篇完整作品后才可评估为可靠风格蒸馏；当前结果仍需人工审核。",
        "language_dna": dict(result.get("language_dna") or {}),
        "structure_templates": list(result.get("structure_templates") or []),
        "cognition_framework": [dict(item) for item in (cognition or [])],
        "material_strategy": dict(result.get("material_strategy") or {}),
        "visual_style": dict(result.get("visual_style") or {}),
        "writing_dna": str(result.get("writing_dna") or "").strip(),
        "evidence_work_ids": sorted(work_ids),
        "distilled_at": utc_now(),
    }


def write_account_distillation(repository, bundle: Mapping[str, Any], *, overwrite: bool = False) -> list:
    """将账号级蒸馏结果拆成写作 DNA 技能要求的五份 Markdown。"""

    account_id = normalize_text(bundle.get("account_id"))
    if not account_id or not bundle.get("schema_version"):
        raise KnowledgeContractError("账号蒸馏 bundle 缺少必要字段")
    sample_count = int(bundle.get("sample_count") or 0)
    status = str(bundle.get("status") or "FORMAT_ONLY_REVIEW_REQUIRED")
    evidence = list(bundle.get("evidence_work_ids") or [])
    common = {
        "schema_version": 1,
        "account_id": account_id,
        "sample_count": sample_count,
        "knowledge_status": "REVIEW_PENDING",
        "distillation_status": status,
        "evidence_work_ids": evidence,
    }
    source_ids = [str(value).strip() for value in (bundle.get("knowledge_source_ids") or []) if str(value).strip()]
    if source_ids:
        common["knowledge_source_ids"] = list(dict.fromkeys(source_ids))[:50]
    source_versions = bundle.get("knowledge_source_versions")
    if isinstance(source_versions, Mapping):
        common["knowledge_source_versions"] = {
            str(key): str(value or "source-v1")
            for key, value in source_versions.items()
            if str(key).strip()
        }
    # 候选版/真实版共用五个文件名，但把生成方式和可靠性边界写进
    # frontmatter，便于 Obsidian 检索时区分“离线候选”与“模型蒸馏”。
    if bundle.get("distillation_method"):
        common["distillation_method"] = str(bundle.get("distillation_method"))
    if bundle.get("reliability_note"):
        common["reliability_note"] = str(bundle.get("reliability_note"))
    language = bundle.get("language_dna") if isinstance(bundle.get("language_dna"), Mapping) else {}
    structures = bundle.get("structure_templates") if isinstance(bundle.get("structure_templates"), list) else []
    cognition = bundle.get("cognition_framework") if isinstance(bundle.get("cognition_framework"), list) else []
    material = bundle.get("material_strategy") if isinstance(bundle.get("material_strategy"), Mapping) else {}
    visual = bundle.get("visual_style") if isinstance(bundle.get("visual_style"), Mapping) else {}
    writing_dna = str(bundle.get("writing_dna") or "").strip()
    documents = {
        "语言DNA.md": ("L1", language, "# 语言 DNA\n\n" + _section_lines(language)),
        "文章结构模板.md": ("L2", {"templates": structures}, "# 文章结构模板\n\n" + _section_lines({"templates": structures})),
        "写作视角与认知框架.md": (
            "L3-L5",
            {"cognition_framework": cognition, "material_strategy": material},
            "# 写作视角与认知框架\n\n" + _section_lines({"cognition_framework": cognition, "material_strategy": material}),
        ),
        "视觉风格指南.md": ("L6", visual, "# 视觉风格指南\n\n" + _section_lines(visual)),
        "Writing-DNA.md": (
            "L1-L6",
            {"summary": "账号级可复用规则", "writing_dna": writing_dna},
            "# Writing DNA\n\n" + (writing_dna or "账号级整合文档尚未由 provider 提供，当前仅保留结构。"),
        ),
    }
    paths = []
    for filename, (layer, payload, body) in documents.items():
        frontmatter = {**common, "layer": layer, "content": payload}
        paths.append(repository.write_account_note(account_id, filename=filename, frontmatter=frontmatter, body=body, overwrite=overwrite))
    return paths


def _section_lines(value: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for key, item in value.items():
        lines.append(f"## {key}")
        lines.append("")
        lines.append(str(item))
        lines.append("")
    return "\n".join(lines).strip()


def distill_account_with_provider(
    provider: AccountDistillationProvider | None,
    *,
    account_id: str,
    works: Sequence[Mapping[str, Any] | WorkCard],
) -> dict[str, Any]:
    if provider is None:
        raise DistillationProviderRequired("未注入账号蒸馏 provider；当前不会自动调用模型")
    result = provider.distill_account(account_id=account_id, works=[work.to_dict() if isinstance(work, WorkCard) else dict(work) for work in works])
    return build_account_distillation(account_id=account_id, works=works, result=result)


def build_work_card_from_distillation(
    *,
    account_id: str,
    title: str,
    source_content: str,
    source_url: str = "",
    platform: str = "",
    published_at: str = "",
    source_ref: str = "",
    result: Mapping[str, Any],
    distilled_by: str = "",
    prompt_version: str = "",
) -> WorkCard:
    text = str(source_content or "").strip()
    if len(text) < 20:
        raise KnowledgeContractError("作品原文过短，不能进入蒸馏")
    if not isinstance(result, Mapping):
        raise KnowledgeContractError("蒸馏结果必须是对象")
    card = WorkCard(
        work_id=build_work_id(
            account_id=account_id,
            title=title,
            source_url=source_url,
            published_at=published_at,
            content=text,
        ),
        account_id=account_id,
        title=title,
        platform=platform,
        source_url=source_url,
        published_at=published_at,
        source_ref=source_ref,
        content_hash=content_sha256(text),
        facts=list(result.get("facts") or []),
        core_thesis=str(result.get("core_thesis") or ""),
        angle=str(result.get("angle") or ""),
        reasoning_steps=list(result.get("reasoning_steps") or []),
        evidence_types=list(result.get("evidence_types") or []),
        counterpoints=list(result.get("counterpoints") or []),
        conclusion_boundary=str(result.get("conclusion_boundary") or ""),
        content_grade=str(result.get("content_grade") or "B").upper(),
        performance_grade=str(result.get("performance_grade") or "UNRATED").upper(),
        manual_reviewed=False,
        knowledge_status="REVIEW_PENDING",
        distilled_by=normalize_text(distilled_by),
        prompt_version=normalize_text(prompt_version),
        distilled_at=utc_now(),
    )
    return card.validate()


def build_cognition_card_from_distillation(
    *,
    cognition_id: str,
    account_id: str,
    result: Mapping[str, Any],
    evidence_work_ids: list[str] | None = None,
) -> CognitionCard:
    if not isinstance(result, Mapping):
        raise KnowledgeContractError("账号认知蒸馏结果必须是对象")
    evidence = list(evidence_work_ids or result.get("evidence_work_ids") or [])
    if not evidence:
        raise KnowledgeContractError("账号级认知必须包含 evidence_work_ids")
    card = CognitionCard(
        cognition_id=cognition_id,
        account_id=account_id,
        title=str(result.get("title") or ""),
        claim=str(result.get("claim") or ""),
        reasoning_pattern=str(result.get("reasoning_pattern") or ""),
        suitable_events=list(result.get("suitable_events") or []),
        evidence_work_ids=evidence,
        confidence=float(result.get("confidence") or 0.0),
        knowledge_status="REVIEW_PENDING",
    )
    return card.validate()


def distill_with_provider(provider: DistillationProvider | None, **kwargs: Any) -> WorkCard:
    if provider is None:
        raise DistillationProviderRequired("未注入蒸馏 provider；当前不会自动调用模型")
    result = provider.distill(
        account_id=kwargs["account_id"],
        title=kwargs["title"],
        source_content=kwargs["source_content"],
        source_url=kwargs.get("source_url", ""),
    )
    return build_work_card_from_distillation(result=result, **kwargs)


__all__ = [
    "AccountDistillationProvider",
    "DistillationProvider",
    "DistillationProviderRequired",
    "build_account_distillation",
    "build_cognition_card_from_distillation",
    "build_work_card_from_distillation",
    "distill_account_with_provider",
    "distill_with_provider",
    "write_account_distillation",
]
