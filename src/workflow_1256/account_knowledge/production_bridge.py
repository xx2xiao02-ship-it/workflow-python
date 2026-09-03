"""把 8768 风格包采集结果接入账号知识库。

这里是生产入口与 Obsidian 底座之间唯一的桥接层。采集结果先落成
``raw_works``、作品卡和 ``_meta``，再显式调用账号知识 provider 生成五份
Writing-DNA 笔记；任何一步失败都会保留已经写入的可追溯中间产物。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .contracts import WorkCard, build_work_id, content_sha256, normalize_text
from .distillation import build_account_distillation, write_account_distillation
from .evidence import DistillationJob, EvidenceSet, RawSource, build_evidence_set_id, utc_now as evidence_utc_now
from .model_providers import AccountKnowledgeModelProvider
from .obsidian_repository import KnowledgeConflictError, ObsidianRepository


MIN_PRODUCTION_TRANSCRIPT_CHARS = 80
PRODUCTION_PROMPT_VERSION = "account-knowledge-production-v1"
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class ProductionBridgeError(RuntimeError):
    """8768 生产入口接入账号知识库失败。"""


def _safe_account_id(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip()).strip("_-")
    if not candidate:
        return ""
    candidate = candidate[:64]
    return candidate if _ACCOUNT_ID_RE.fullmatch(candidate) else ""


def resolve_account_id(
    repository: ObsidianRepository,
    *,
    report: Mapping[str, Any],
    requested_account_id: str = "",
) -> str:
    """解析账号编号，优先复用显式编号或已有账号的来源标识。"""

    requested = _safe_account_id(requested_account_id)
    if requested:
        return requested
    creator_id = normalize_text(report.get("creator_id"))
    creator_url = normalize_text(report.get("creator_url"))
    creator_name = normalize_text(report.get("creator_name"))
    # 已有账号说明笔记可以保存人工命名；相同来源再次采集时复用该目录。
    for account_id in repository.list_account_ids():
        try:
            frontmatter, _ = repository.read_account_note(account_id, filename="account.md")
        except Exception:
            continue
        if creator_id and normalize_text(frontmatter.get("creator_id")) == creator_id:
            return account_id
        if creator_url and normalize_text(frontmatter.get("creator_url")) == creator_url:
            return account_id
        if creator_name and normalize_text(frontmatter.get("account_name")) == creator_name:
            return account_id
    # 抖音 sec_uid/B 站 mid 都是稳定的公开来源编号；没有来源编号时用主页哈希。
    seed = creator_id or creator_url or creator_name
    if not seed:
        raise ProductionBridgeError("采集报告缺少 creator_id、creator_url 和 creator_name，无法建立账号知识库")
    derived = _safe_account_id(f"creator_{seed}")
    if not derived:
        derived = "creator_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return derived[:64]


def _source_url(item: Mapping[str, Any], platform: str) -> str:
    source_url = normalize_text(item.get("source_url"))
    if source_url.startswith(("http://", "https://")):
        return source_url
    video_id = normalize_text(item.get("video_id") or item.get("bvid"))
    if video_id and platform == "douyin":
        return f"https://www.douyin.com/video/{video_id}"
    if video_id and platform == "bilibili":
        return f"https://www.bilibili.com/video/{video_id}"
    return ""


def _work_id_for_item(
    *,
    account_id: str,
    item: Mapping[str, Any],
    title: str,
    source_url: str,
    published_at: str,
    transcript: str,
) -> str:
    """按账号 + aweme_id 固化 TikHub 作品身份，旧报告继续走原规则。"""

    aweme_id = normalize_text(item.get("aweme_id"))
    if aweme_id:
        identity = "\x1f".join([str(account_id).strip(), aweme_id])
        return "work-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return build_work_id(
        account_id=account_id,
        title=title,
        source_url=source_url,
        published_at=published_at,
        content=transcript,
    )


def _raw_markdown(*, title: str, item: Mapping[str, Any], transcript: str, source_url: str, platform: str) -> str:
    # 这里不依赖 PyYAML；原始文件是给人/Obsidian查看的，作品卡仍是契约权威。
    def scalar(value: Any) -> str:
        # JSON 双引号字符串也是合法 YAML 标量，避免标题中的冒号、井号或
        # 换行破坏 Obsidian frontmatter。
        return json.dumps(str(value or ""), ensure_ascii=False)

    lines = [
        "---",
        f"title: {scalar(title)}",
        f"platform: {scalar(platform)}",
        f"source_url: {scalar(source_url)}",
        f"published_at: {scalar(item.get('published_at'))}",
        f"video_id: {scalar(item.get('video_id') or item.get('bvid'))}",
        f"aweme_id: {scalar(item.get('aweme_id'))}",
        f"transcript_status: {scalar(item.get('transcript_status'))}",
        "---",
        "",
        f"# {title}",
        "",
        transcript.strip(),
        "",
    ]
    return "\n".join(lines)


def _stored_work_transcript(body: str, title: str) -> str:
    """从已保存的作品卡正文中取回原始转写，供显式复用策略使用。"""

    content = str(body or "").strip()
    heading = f"# {str(title or '').strip()}".strip()
    if heading and content.startswith(heading):
        content = content[len(heading) :].lstrip()
    return content.strip()


def _work_input(card: WorkCard, source_content: str) -> dict[str, Any]:
    payload = card.to_dict()
    payload["source_content"] = source_content
    return payload


def sync_collection_to_obsidian(
    report: Mapping[str, Any],
    *,
    repository: ObsidianRepository,
    provider: Any | None = None,
    requested_account_id: str = "",
    progress_reporter: Callable[[str, int, str], None] | None = None,
    overwrite_pending_dna: bool = True,
    distill_assets: bool = True,
    task_id: str = "",
    evidence_set_id: str = "",
    checkpoint_state: str = "saved",
    candidate_count: int | None = None,
) -> dict[str, Any]:
    """同步一次采集结果。

    ``distill_assets=True`` 保留历史直接调用的模型链；第一阶段生产采集
    传入 ``distill_assets=False``，只写入 RawSource/WorkCard/EvidenceSet 和
    DistillationJob，绝不调用模型。
    """

    if not isinstance(report, Mapping):
        raise ProductionBridgeError("采集报告必须是对象")
    items = report.get("items")
    if not isinstance(items, list):
        raise ProductionBridgeError("采集报告缺少 items")
    account_id = resolve_account_id(repository, report=report, requested_account_id=requested_account_id)
    creator_name = normalize_text(report.get("creator_name")) or account_id
    repository.initialize_account(account_id, account_name=creator_name)
    existing_cards = {card.work_id: card for card in repository.list_work_cards(account_id)}
    work_inputs: list[dict[str, Any]] = []
    source_count = 0
    skipped_count = 0
    reused_count = 0
    conflict_reused_count = 0
    seen_item_identities: set[str] = set()

    eligible: list[tuple[Mapping[str, Any], str, str, str, str]] = []
    platform = normalize_text(report.get("platform"))
    for item in items:
        if not isinstance(item, Mapping):
            skipped_count += 1
            continue
        transcript = str(item.get("transcript") or "").strip()
        if len(transcript) < MIN_PRODUCTION_TRANSCRIPT_CHARS:
            skipped_count += 1
            continue
        title = normalize_text(item.get("title")) or f"{platform or '视频'}作品 {normalize_text(item.get('video_id') or item.get('bvid'))}"
        source_url = _source_url(item, platform)
        if not source_url:
            skipped_count += 1
            continue
        # TikHub 分页可能在边界页重复返回同一 aweme；先按稳定作品身份
        # 去重，避免同一批次重复写原文或调用作品蒸馏 provider。
        item_identity = normalize_text(item.get("aweme_id")) or source_url
        if item_identity in seen_item_identities:
            skipped_count += 1
            continue
        seen_item_identities.add(item_identity)
        published_at = normalize_text(item.get("published_at"))
        work_id = _work_id_for_item(
            account_id=account_id, item=item, title=title, source_url=source_url,
            published_at=published_at, transcript=transcript,
        )
        source_ref = f"raw_works/{work_id}.md"
        existing = existing_cards.get(work_id)
        effective_transcript = transcript
        raw_note = _raw_markdown(
            title=title, item=item, transcript=transcript, source_url=source_url, platform=platform
        )
        try:
            repository.save_raw_source(account_id, source_ref, raw_note.encode("utf-8"))
        except KnowledgeConflictError:
            # 重复采集时本地 Whisper 可能产生不同转写，但 work_id 仍然
            # 表示同一公开作品。按生产策略保留 Vault 中的旧原文，不静默
            # 覆盖；只有存在可校验的作品卡正文时才允许安全复用。
            if existing is None:
                raise
            try:
                stored_card, stored_body = repository.load_work_card(account_id, work_id)
            except Exception:
                raise
            effective_transcript = _stored_work_transcript(stored_body, stored_card.title)
            if len(effective_transcript) < MIN_PRODUCTION_TRANSCRIPT_CHARS:
                raise ProductionBridgeError(f"无法安全复用已有作品：{work_id} 原文不完整")
            if stored_card.content_hash != content_sha256(effective_transcript):
                raise ProductionBridgeError(f"无法安全复用已有作品：{work_id} 作品卡与原文不一致")
            existing_cards[work_id] = stored_card
            conflict_reused_count += 1
        eligible.append((item, effective_transcript, title, source_url, published_at))
        source_ids = report.get("knowledge_source_ids")
        source_id_values = [str(value).strip() for value in source_ids] if isinstance(source_ids, (list, tuple, set)) else []
        source_id = normalize_text(item.get("source_id")) or (source_id_values[0] if source_id_values else "")
        if not source_id:
            source_id = "source-" + hashlib.sha256((normalize_text(report.get("creator_id")) + source_url).encode("utf-8")).hexdigest()[:20]
        source_versions = report.get("knowledge_source_versions")
        source_version = "source-v1"
        if isinstance(source_versions, Mapping):
            source_version = str(source_versions.get(source_id) or "source-v1")
        raw_card = WorkCard(
            work_id=work_id,
            account_id=account_id,
            title=title,
            platform=platform,
            source_url=source_url,
            published_at=published_at,
            source_ref=source_ref,
            source_id=source_id,
            source_version=source_version,
            content_hash=content_sha256(effective_transcript),
            content_grade="B",
            knowledge_status="RAW",
        ).validate()
        if existing is None:
            repository.save_work_card(raw_card, body=f"# {title}\n\n{effective_transcript}\n")
            existing_cards[work_id] = raw_card
        # Keep an explicit source record alongside the human-readable raw note;
        # this is the durable audit link used by EvidenceSet and resume logic.
        raw_record = RawSource(
            source_id=source_id,
            account_id=account_id,
            work_id=work_id,
            source_version=source_version,
            raw_url=source_url,
            platform=platform,
            source_kind="video",
            author=creator_name,
            title=title,
            published_at=published_at,
            transcript=effective_transcript,
            content_hash=content_sha256(effective_transcript),
            vault_path=str(repository._inside_vault(repository.account_dir(account_id) / source_ref)),
        )
        try:
            repository.save_raw_source_record(raw_record)
        except KnowledgeConflictError:
            # The work_id is immutable.  A duplicate resume reuses the original
            # source record and never replaces it with a new transcription.
            existing_raw = repository.load_raw_source_record(account_id, work_id)
            if existing_raw.content_hash != raw_record.content_hash:
                raise ProductionBridgeError(f"无法安全复用已有原始来源：{work_id}")
        metadata = {
            "schema_version": 1,
            "work_id": work_id,
            "account_id": account_id,
            "title": title,
            "date": published_at,
            "author": creator_name,
            "platform": platform,
            "source_url": source_url,
            "source_ref": source_ref,
            "content_hash": raw_card.content_hash,
            "article_type": "8768 风格包采集作品，待模型蒸馏",
            "source_origin": "8768_style_collection",
            "transcript_status": normalize_text(item.get("transcript_status")),
            "video_id": normalize_text(item.get("video_id") or item.get("bvid")),
            "aweme_id": normalize_text(item.get("aweme_id")),
            "duration_seconds": item.get("duration_seconds"),
        }
        try:
            repository.save_work_metadata(account_id, work_id, metadata)
        except KnowledgeConflictError:
            # Existing metadata may contain older exporter labels or human
            # annotations.  Reuse it when the immutable content identity is
            # unchanged; never silently replace a different work.
            try:
                existing_metadata = repository.load_work_metadata(account_id, work_id)
            except Exception:
                raise
            if normalize_text(existing_metadata.get("content_hash")) != raw_card.content_hash:
                raise
        source_count += 1

    if not eligible and distill_assets:
        raise ProductionBridgeError("没有达到 80 字符门槛的完整转写，未执行模型蒸馏")

    for index, (item, transcript, title, source_url, published_at) in enumerate(eligible, start=1):
        work_id = _work_id_for_item(
            account_id=account_id, item=item, title=title, source_url=source_url,
            published_at=published_at, transcript=transcript,
        )
        existing = existing_cards[work_id]
        if existing.content_hash == content_sha256(transcript) and existing.knowledge_status in {
            "REVIEW_PENDING", "DISTILLED", "APPROVED", "PUBLISHED"
        }:
            card = existing
            reused_count += 1
        elif not distill_assets:
            card = existing
        else:
            if provider is None:
                raise ProductionBridgeError("模型 provider 未提供")
            if progress_reporter is not None:
                progress_reporter("account_work_distilling", 80 + int(index / max(1, len(eligible)) * 12), f"正在蒸馏账号作品 {index}/{len(eligible)}")
            try:
                card = provider.distill_work_card(
                    account_id=account_id,
                    title=title,
                    source_content=transcript,
                    source_url=source_url,
                    platform=normalize_text(report.get("platform")),
                    published_at=published_at,
                    source_ref=f"raw_works/{work_id}.md",
                    prompt_version=PRODUCTION_PROMPT_VERSION,
                )
            except Exception as exc:
                raise ProductionBridgeError(f"作品 {work_id} 蒸馏失败：{type(exc).__name__}") from exc
            repository.save_work_card(card, body=f"# {title}\n\n{transcript}\n", overwrite=True)
            existing_cards[work_id] = card
        work_inputs.append(_work_input(card, transcript))

    # Count all complete, non-rejected cards already in this account.  This is
    # intentionally read from Vault so a resumed task can reuse prior batches.
    eligible_work_ids: list[str] = []
    for card in repository.list_work_cards(account_id):
        if card.knowledge_status in {"ARCHIVED", "REJECTED"} or not card.content_hash:
            continue
        try:
            _stored_card, stored_body = repository.load_work_card(account_id, card.work_id)
        except Exception:
            continue
        if len(_stored_work_transcript(stored_body, card.title)) >= MIN_PRODUCTION_TRANSCRIPT_CHARS:
            eligible_work_ids.append(card.work_id)
    source_ids_for_evidence = report.get("knowledge_source_ids")
    source_ids_for_evidence = [str(value).strip() for value in source_ids_for_evidence] if isinstance(source_ids_for_evidence, (list, tuple, set)) else []
    target_valid_works = max(1, int(report.get("target_valid_works") or report.get("sample_target") or 50))
    evidence_id = str(evidence_set_id or report.get("evidence_set_id") or "").strip()
    if not evidence_id:
        evidence_id = build_evidence_set_id(account_id, source_ids=source_ids_for_evidence, seed=task_id or "account-collection")
    evidence = EvidenceSet(
        evidence_set_id=evidence_id,
        account_id=account_id,
        source_ids=source_ids_for_evidence,
        source_versions=dict(report.get("knowledge_source_versions") or {}) if isinstance(report.get("knowledge_source_versions"), Mapping) else {},
        candidate_count=max(0, int(candidate_count if candidate_count is not None else report.get("candidate_count") or report.get("raw_listed_count") or report.get("listed_count") or len(items))),
        eligible_work_count=len(eligible_work_ids),
        target_valid_works=target_valid_works,
        work_ids=eligible_work_ids,
        checkpoint_state=checkpoint_state,
        task_id=str(task_id or ""),
        updated_at=evidence_utc_now(),
    ).validate()
    repository.save_evidence_set(evidence, overwrite=True)
    if task_id:
        job_status = "READY_TO_DISTILL" if evidence.target_reached else "WAITING_FOR_EVIDENCE"
        job = DistillationJob(
            job_id=str(task_id),
            account_id=account_id,
            target_valid_works=target_valid_works,
            candidate_count=evidence.candidate_count,
            eligible_work_count=evidence.eligible_work_count,
            status=job_status,
            checkpoint=dict(report.get("checkpoint") or {}) if isinstance(report.get("checkpoint"), Mapping) else {},
            evidence_set_id=evidence.evidence_set_id,
            source_ids=source_ids_for_evidence,
            updated_at=evidence_utc_now(),
        ).validate()
        repository.save_distillation_job(job, overwrite=True)

    if not distill_assets:
        if progress_reporter is not None:
            progress_reporter("ready_to_distill" if evidence.target_reached else "waiting_for_evidence", 78 if evidence.target_reached else 72, f"Obsidian 已保存 {evidence.eligible_work_count}/{evidence.target_valid_works} 篇合格作品")
        return {
            "status": "ready_to_distill" if evidence.target_reached else "waiting_for_evidence",
            "evidence_status": evidence.status,
            "evidence_set_id": evidence.evidence_set_id,
            "account_id": account_id,
            "account_name": creator_name,
            "candidate_count": evidence.candidate_count,
            "eligible_work_count": evidence.eligible_work_count,
            "target_valid_works": evidence.target_valid_works,
            "target_reached": evidence.target_reached,
            "work_ids": evidence.work_ids,
            "raw_source_count": source_count,
            "work_card_count": len(eligible_work_ids),
            "checkpoint_state": evidence.checkpoint_state,
            "source_ids": evidence.source_ids,
            "vault_path": str(repository.vault_path),
        }

    if progress_reporter is not None:
        progress_reporter("account_distilling", 94, f"正在汇总 {len(work_inputs)} 条作品的账号级 Writing-DNA")
    try:
        account_result = provider.distill_account(
            account_id=account_id,
            works=work_inputs,
            batch_size=8,
        )
        bundle = build_account_distillation(account_id=account_id, works=work_inputs, result=account_result)
        # Preserve the upstream knowledge-source identity on every account DNA
        # note.  The report is supplied by the production style task and keeps
        # the bridge compatible with older direct callers that do not provide
        # source ids.
        raw_source_ids = report.get("knowledge_source_ids")
        if isinstance(raw_source_ids, (list, tuple, set)):
            source_ids = [str(value).strip() for value in raw_source_ids if str(value).strip()]
            if source_ids:
                bundle["knowledge_source_ids"] = list(dict.fromkeys(source_ids))[:50]
        raw_source_versions = report.get("knowledge_source_versions")
        if isinstance(raw_source_versions, Mapping):
            bundle["knowledge_source_versions"] = {
                str(key): str(value or "source-v1")
                for key, value in raw_source_versions.items()
                if str(key).strip()
            }
        context = repository.load_account_dna_context(account_id, include_pending=True)
        overwrite = overwrite_pending_dna and context.get("knowledge_status") != "APPROVED"
        paths = write_account_distillation(repository, bundle, overwrite=overwrite)
    except Exception as exc:
        raise ProductionBridgeError(f"账号级 Writing-DNA 蒸馏失败：{type(exc).__name__}") from exc
    if progress_reporter is not None:
        progress_reporter("account_assets_ready", 98, "账号作品卡和五份 Writing-DNA 已写回 Obsidian，可到知识中枢后台管理修改或启用")
    return {
        "status": "review_required",
        "account_id": account_id,
        "account_name": creator_name,
        "sample_count": len(work_inputs),
        # The collection target is a planning cap, not a hard distillation
        # gate.  Account-level distillation always receives every usable
        # WorkCard currently present in the Vault (zero usable cards still
        # fails above with an explicit error).
        "distillation_sample_count": len(work_inputs),
        "sample_target_policy": "soft_cap",
        "raw_source_count": source_count,
        "work_card_count": len(work_inputs),
        "reused_work_card_count": reused_count,
        "conflict_reused_work_count": conflict_reused_count,
        "skipped_item_count": skipped_count,
        "dna_note_files": [Path(path).name for path in paths],
        "distillation_status": bundle.get("status"),
        "knowledge_status": "REVIEW_PENDING",
        "knowledge_source_ids": list(bundle.get("knowledge_source_ids") or []),
        "knowledge_source_versions": dict(bundle.get("knowledge_source_versions") or {}) if isinstance(bundle.get("knowledge_source_versions"), Mapping) else {},
        "vault_path": str(repository.vault_path),
    }


__all__ = [
    "MIN_PRODUCTION_TRANSCRIPT_CHARS",
    "PRODUCTION_PROMPT_VERSION",
    "ProductionBridgeError",
    "resolve_account_id",
    "sync_collection_to_obsidian",
]
