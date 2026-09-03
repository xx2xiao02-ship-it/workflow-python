"""风格包派生作品导入与候选升级。

本模块把现有风格包的作品记录接入账号知识库，但始终标记为
``style_package_derived``。它可以解除知识库建设的开发阻断，不能把派生稿
伪装成完整原始作品，也不会自动覆盖当前运行时风格包或调用模型。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import WorkCard, build_work_id, content_sha256, normalize_text
from .obsidian_repository import KnowledgeConflictError, ObsidianRepository


DERIVED_SOURCE_KIND = "style_package_derived"
DERIVED_EVIDENCE_QUALITY = "DERIVED"
UPGRADE_SCHEMA_VERSION = "style-profile-upgrade-v1"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"风格包记录不可读取：{path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"风格包记录必须是对象：{path.name}")
    return value


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            text = normalize_text(
                item.get("statement")
                or item.get("point")
                or item.get("part")
                or item.get("role")
                or item.get("name")
                or item.get("category")
            )
        else:
            text = normalize_text(item)
        if text and text not in result:
            result.append(text)
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rewrite_paths(style_root: Path, *, include_archived: bool) -> list[Path]:
    paths: list[Path] = []
    active = style_root / "rewrites"
    if active.is_dir():
        paths.extend(active.glob("rewrite_*.json"))
    if include_archived:
        archived = style_root / "archived_rewrites"
        if archived.is_dir():
            paths.extend(archived.rglob("rewrite_*.json"))
    seen: set[str] = set()
    result: list[Path] = []
    for path in sorted(paths, key=lambda item: str(item).lower()):
        record_id = path.stem
        if record_id in seen:
            continue
        seen.add(record_id)
        result.append(path)
    return result


def load_style_package_rewrites(
    style_root: Path | str,
    *,
    style_profile_id: str = "",
    include_archived: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """读取现有风格包作品记录，默认包含可恢复的历史改写记录。"""

    root = Path(style_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"风格包目录不存在：{root}")
    profile_filter = normalize_text(style_profile_id)
    records: list[dict[str, Any]] = []
    for path in _rewrite_paths(root, include_archived=include_archived):
        record = _read_json(path)
        if profile_filter and normalize_text(record.get("style_profile_id")) != profile_filter:
            continue
        record["_source_file"] = str(path.relative_to(root)).replace("\\", "/")
        records.append(record)
    records.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("rewrite_id") or "")))
    if limit is not None:
        if limit <= 0:
            return []
        records = records[-limit:]
    return records


def _source_payload(record: Mapping[str, Any]) -> tuple[str, str]:
    case_source = _mapping(record.get("case_source"))
    result = _mapping(record.get("result"))
    transcript = str(case_source.get("transcript") or "").strip()
    if len(transcript) >= 20:
        return transcript, "transcript"
    for key in ("reviewed_copy", "approved_copy", "rewritten_copy", "draft_copy"):
        value = str(result.get(key) or record.get(key) or "").strip()
        if len(value) >= 20:
            return value, "derived_copy"
    return "", ""


def _build_derived_card(*, account_id: str, record: Mapping[str, Any]) -> tuple[WorkCard, str, dict[str, Any]]:
    account = normalize_text(account_id)
    rewrite_id = normalize_text(record.get("rewrite_id"))
    style_profile_id = normalize_text(record.get("style_profile_id"))
    case_source = _mapping(record.get("case_source"))
    result = _mapping(record.get("result"))
    blueprint = _mapping(result.get("content_blueprint"))
    source_content, source_role = _source_payload(record)
    if len(source_content) < 20:
        raise ValueError(f"作品 {rewrite_id or '<unknown>'} 缺少可导入的作品提取内容")
    base_title = normalize_text(case_source.get("title") or blueprint.get("topic"))
    title = base_title or f"风格包作品 {rewrite_id or content_sha256(source_content)[:10]}"
    source_url = normalize_text(case_source.get("source_url"))
    published_at = normalize_text(case_source.get("published_at"))
    work_id = build_work_id(
        account_id=account,
        title=title,
        source_url=source_url,
        published_at=published_at,
        content=source_content,
    )
    source_ref = f"derived_works/{work_id}.txt"
    facts = _clean_list(blueprint.get("fact_units"))
    reasoning_steps = _clean_list(blueprint.get("argument_chain"))
    structure = _clean_list(blueprint.get("structure"))
    card = WorkCard(
        work_id=work_id,
        account_id=account,
        title=title,
        platform=normalize_text(case_source.get("platform")),
        source_url=source_url,
        published_at=published_at,
        source_ref=source_ref,
        content_hash=content_sha256(source_content),
        facts=facts,
        core_thesis=normalize_text(blueprint.get("core_claim") or result.get("source_summary")),
        angle=normalize_text(blueprint.get("audience_promise") or blueprint.get("topic")),
        reasoning_steps=reasoning_steps,
        evidence_types=["风格包作品提取", source_role] if source_role else ["风格包作品提取"],
        counterpoints=[],
        conclusion_boundary=normalize_text(blueprint.get("ending")),
        content_grade="B",
        performance_grade="UNRATED",
        manual_reviewed=False,
        knowledge_status="REVIEW_PENDING",
        distilled_by="style_package_rewrite",
        prompt_version=f"style-profile:{style_profile_id}" if style_profile_id else "style-package-derived-v1",
        distilled_at=normalize_text(record.get("created_at")) or _now(),
    ).validate()
    derived_copy = normalize_text(result.get("reviewed_copy") or result.get("rewritten_copy") or result.get("draft_copy"))
    body_parts = [
        "# 风格包派生作品",
        "",
        "> 此笔记来自风格包作品记录，不等同于账号原始作品；需要人工审核后才能进入正常检索。",
        "",
        "## 作品提取内容",
        "",
        source_content,
    ]
    if derived_copy:
        body_parts.extend(["", "## 改写/审核内容（派生）", "", derived_copy])
    metadata = {
        "schema_version": 1,
        "work_id": work_id,
        "account_id": account,
        "title": title,
        "date": published_at,
        "platform": card.platform,
        "source_url": source_url,
        "source_ref": source_ref,
        "content_hash": card.content_hash,
        "source_kind": DERIVED_SOURCE_KIND,
        "evidence_quality": DERIVED_EVIDENCE_QUALITY,
        "source_role": source_role,
        "style_profile_id": style_profile_id,
        "rewrite_id": rewrite_id,
        "rewrite_review_status": normalize_text(record.get("review_status")) or "PENDING_USER_REVIEW",
        "knowledge_status": card.knowledge_status,
        "article_type": "风格包派生作品，待人工/模型审核",
        "structure_pattern": structure,
        "derived_copy": derived_copy,
        "source_record_file": normalize_text(record.get("_source_file")),
        "notable": "不可作为完整原始作品证明；后续可由原始作品按 content_hash 关联升级",
    }
    return card, "\n".join(body_parts).strip() + "\n", metadata


@dataclass
class StylePackageImportItem:
    rewrite_id: str
    style_profile_id: str
    work_id: str = ""
    status: str = ""
    message: str = ""
    source_ref: str = ""


@dataclass
class StylePackageImportReport:
    account_id: str
    style_root: str
    discovered: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    items: list[StylePackageImportItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "style_root": self.style_root,
            "discovered": self.discovered,
            "imported": self.imported,
            "skipped": self.skipped,
            "failed": self.failed,
            "items": [asdict(item) for item in self.items],
        }


def import_style_package_rewrites(
    repository: ObsidianRepository,
    *,
    account_id: str,
    records: Iterable[Mapping[str, Any]],
    overwrite: bool = False,
) -> tuple[StylePackageImportReport, list[dict[str, Any]]]:
    """把派生作品写入 Vault，并返回可用于候选升级的原始记录。"""

    report = StylePackageImportReport(account_id=normalize_text(account_id), style_root="")
    normalized_records = [dict(item) for item in records]
    report.discovered = len(normalized_records)
    imported_records: list[dict[str, Any]] = []
    repository.initialize_account(report.account_id)
    for record in normalized_records:
        rewrite_id = normalize_text(record.get("rewrite_id")) or "unknown"
        item = StylePackageImportItem(
            rewrite_id=rewrite_id,
            style_profile_id=normalize_text(record.get("style_profile_id")),
        )
        report.items.append(item)
        try:
            card, body, metadata = _build_derived_card(account_id=report.account_id, record=record)
            source_path = repository.save_derived_source(
                report.account_id,
                card.source_ref,
                _source_payload(record)[0].encode("utf-8"),
                overwrite=overwrite,
            )
            card_path = repository.save_work_card(card, body=body, overwrite=overwrite)
            metadata_path = repository.save_work_metadata(
                report.account_id,
                card.work_id,
                metadata,
                overwrite=overwrite,
            )
            item.work_id = card.work_id
            item.source_ref = card.source_ref
            item.status = "imported"
            item.message = f"已写入派生源、作品卡和 _meta：{source_path.name}"
            imported_records.append({**record, "work_id": card.work_id, "work_card": card.to_dict(), "metadata": metadata})
            if card_path.is_file() and metadata_path.is_file():
                report.imported += 1
        except KnowledgeConflictError as exc:
            report.skipped += 1
            item.status = "skipped"
            item.message = str(exc)
        except Exception as exc:
            report.failed += 1
            item.status = "failed"
            item.message = f"{type(exc).__name__}: {exc}"
    return report, imported_records


def build_profile_upgrade_candidate(
    *,
    profile_record: Mapping[str, Any],
    imported_records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """生成不覆盖现行 Profile 的候选升级包。"""

    profile_id = normalize_text(profile_record.get("style_profile_id"))
    base_profile = profile_record.get("style_profile")
    if not profile_id or not isinstance(base_profile, Mapping):
        raise ValueError("风格包记录缺少 style_profile_id 或 style_profile")
    records = [dict(item) for item in imported_records]
    evidence_ids = sorted({normalize_text(item.get("work_id")) for item in records if normalize_text(item.get("work_id"))})
    rewrite_ids = sorted({normalize_text(item.get("rewrite_id")) for item in records if normalize_text(item.get("rewrite_id"))})
    habits: list[str] = []
    audit_rules: list[str] = []
    topics: list[str] = []
    structure_roles: list[str] = []
    for record in records:
        result = _mapping(record.get("result"))
        blueprint = _mapping(result.get("content_blueprint"))
        topics.extend(_clean_list([blueprint.get("topic")]))
        structure_roles.extend(
            _clean_list([item.get("part") for item in blueprint.get("structure", []) if isinstance(item, Mapping)])
            if isinstance(blueprint.get("structure"), list)
            else []
        )
        habits.extend(
            _clean_list([item.get("category") for item in result.get("expression_habits_applied", []) if isinstance(item, Mapping)])
            if isinstance(result.get("expression_habits_applied"), list)
            else []
        )
        audit_report = _mapping(result.get("audit_report"))
        audit_rules.extend(_clean_list(audit_report.get("style_rules_applied")))
    signal_library = {
        "topics": sorted(set(topics)),
        "structure_roles": sorted(set(structure_roles)),
        "expression_habits": sorted(set(habits)),
        "reviewed_style_rules": sorted(set(audit_rules)),
    }
    evidence_key = "\x1f".join([profile_id, *evidence_ids, *rewrite_ids])
    candidate_id = "upgrade_" + hashlib.sha256(evidence_key.encode("utf-8")).hexdigest()[:16]
    candidate_profile = copy.deepcopy(dict(base_profile))
    source_trace_rate = 1.0 if records and all(normalize_text(item.get("work_id")) for item in records) else 0.0
    return {
        "schema_version": UPGRADE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "status": "CANDIDATE_REVIEW_REQUIRED",
        "publish_ready": False,
        "generated_at": _now(),
        "style_profile_id": profile_id,
        "base_review_status": normalize_text(profile_record.get("review_status")),
        "base_sample_count": int(profile_record.get("sample_count") or 0),
        "base_profile": candidate_profile,
        "candidate_profile": candidate_profile,
        "evidence": {
            "source_kind": DERIVED_SOURCE_KIND,
            "evidence_quality": DERIVED_EVIDENCE_QUALITY,
            "work_ids": evidence_ids,
            "rewrite_ids": rewrite_ids,
            "record_count": len(records),
        },
        "delta": {
            "signal_library": signal_library,
            "new_rules_are_signals_only": True,
            "message": "当前为离线候选升级；需补充原始作品、人工确认并回测后，才能合并到 candidate_profile。",
        },
        "evaluation": {
            "status": "PASS" if source_trace_rate == 1.0 else "BLOCKED",
            "scope": "offline_structural_only",
            "source_trace_rate": source_trace_rate,
            "base_profile_unchanged": candidate_profile == dict(base_profile),
            "model_calls": 0,
            "semantic_holdout_score": None,
            "note": "当前只验证来源、版本和结构契约；尚未替代真实模型回测。",
        },
        "gates": {
            "raw_originals_verified": False,
            "human_review_required": True,
            "regression_required": True,
            "runtime_publish_allowed": False,
        },
    }


def write_profile_upgrade_candidate(output_root: Path | str, candidate: Mapping[str, Any], *, overwrite: bool = False) -> Path:
    candidate_id = normalize_text(candidate.get("candidate_id"))
    if not candidate_id or "/" in candidate_id or "\\" in candidate_id or ".." in candidate_id:
        raise ValueError("候选升级编号不合法")
    root = Path(output_root).expanduser().resolve() / "profile_upgrades"
    path = root / f"{candidate_id}.json"
    payload = json.dumps(dict(candidate), ensure_ascii=False, indent=2) + "\n"
    if path.exists() and not overwrite:
        existing = _read_json(path)
        if existing.get("candidate_id") == candidate_id:
            return path
        raise FileExistsError(f"候选升级产物已存在：{path.name}")
    root.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return path


def run_style_package_upgrade(
    repository: ObsidianRepository,
    *,
    account_id: str,
    style_root: Path | str,
    style_profile_id: str,
    output_root: Path | str,
    include_archived: bool = True,
    limit: int | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """执行离线支线：导入派生作品并生成候选风格包。"""

    records = load_style_package_rewrites(
        style_root,
        style_profile_id=style_profile_id,
        include_archived=include_archived,
        limit=limit,
    )
    if not records:
        raise ValueError(f"风格包 {style_profile_id} 没有可用于升级的作品记录")
    report, imported_records = import_style_package_rewrites(
        repository,
        account_id=account_id,
        records=records,
        overwrite=overwrite,
    )
    report.style_root = str(Path(style_root).expanduser().resolve())
    if not imported_records:
        raise ValueError("没有成功导入任何风格包派生作品，无法生成候选升级包")
    profile_path = Path(style_root).expanduser().resolve() / "profiles" / f"{style_profile_id}.json"
    profile_record = _read_json(profile_path)
    candidate = build_profile_upgrade_candidate(profile_record=profile_record, imported_records=imported_records)
    candidate_path = write_profile_upgrade_candidate(output_root, candidate, overwrite=overwrite)
    return {
        "status": "review_required" if report.failed == 0 else "partial_review_required",
        "import_report": report.to_dict(),
        "candidate": candidate,
        "candidate_path": str(candidate_path),
        "provider_mode": "offline_no_model_call",
    }


__all__ = [
    "DERIVED_EVIDENCE_QUALITY",
    "DERIVED_SOURCE_KIND",
    "StylePackageImportItem",
    "StylePackageImportReport",
    "UPGRADE_SCHEMA_VERSION",
    "build_profile_upgrade_candidate",
    "import_style_package_rewrites",
    "load_style_package_rewrites",
    "run_style_package_upgrade",
    "write_profile_upgrade_candidate",
]
