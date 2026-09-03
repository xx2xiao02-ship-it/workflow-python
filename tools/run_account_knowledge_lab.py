"""账号知识库 V2 Lab 的命令行入口。

默认命令只操作配置的 Obsidian Vault 和本地索引，不调用模型、不访问新闻
网络、不触碰现有 8768 生产服务；名称带 ``-live`` 的命令必须显式调用，
才会进入模型请求链路。适合先用少量样本验收数据契约。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workflow_1256.account_knowledge import (
    AccountKnowledgeModelProvider,
    LabArtifactStore,
    ObsidianRepository,
    apply_api_management_model_runtime,
    apply_manual_grade,
    analyze_corpus,
    build_account_distillation,
    build_offline_candidate_bundle,
    import_corpus,
    run_style_package_upgrade,
    write_account_distillation,
    write_offline_candidate_notes,
)  # noqa: E402
from workflow_1256.style_package_store import StylePackageStore  # noqa: E402
from workflow_1256.account_knowledge.indexer import LocalKnowledgeIndex  # noqa: E402
from workflow_1256.account_knowledge.orchestrator import build_topic_plan_from_index  # noqa: E402
from workflow_1256.account_knowledge.retrieval import AccountKnowledgeRetriever  # noqa: E402
from workflow_1256.copywriting_knowledge_v2 import validate_copywriting_draft  # noqa: E402
from workflow_1256.topic_knowledge_v2 import EventCard, RetrievalPolicy, validate_topic_plan  # noqa: E402


def _repository(vault: str | None) -> ObsidianRepository:
    if vault:
        return ObsidianRepository(vault)
    return ObsidianRepository.from_env()


def _json_dump(payload) -> None:
    # Windows 默认控制台编码可能无法输出模型返回的 Unicode 字符；
    # 先切换 stdout 编码，避免结果已写回后仅因打印失败而返回假失败。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _default_index_path(repository: ObsidianRepository) -> Path:
    return repository.vault_path / "账号知识库" / "generated" / "knowledge-index.json"


def _api_management_config_path() -> Path:
    return Path(
        os.environ.get(
            "API_MANAGEMENT_CONFIG_PATH",
            str(PROJECT_ROOT / ".runtime-governance-live" / "data" / "VideoProductionConsole" / "api_management.json"),
        )
    ).expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="账号认知知识库 V2 离线实验入口")
    parser.add_argument("--vault", help="Obsidian Vault 路径；省略时读取 ACCOUNT_KNOWLEDGE_VAULT_PATH")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init-account", help="初始化账号目录")
    init_parser.add_argument("--account-id", required=True)
    init_parser.add_argument("--account-name", default="")
    init_parser.add_argument("--overwrite-account-note", action="store_true")

    import_parser = sub.add_parser("import-corpus", help="导入用户指定的 .md/.txt 完整作品语料")
    import_parser.add_argument("--account-id", required=True)
    import_parser.add_argument("--corpus", required=True)
    import_parser.add_argument("--platform", default="")
    import_parser.add_argument("--default-content-grade", default="B", choices=["S", "A", "B", "C"])
    import_parser.add_argument("--no-copy-raw", action="store_true")

    analyze_parser = sub.add_parser("analyze-corpus", help="统计 L1/L6 文本和 Markdown 特征")
    analyze_parser.add_argument("--corpus", required=True)
    analyze_parser.add_argument("--output")

    upgrade_parser = sub.add_parser(
        "upgrade-profile",
        help="离线导入风格包派生作品并生成候选风格包升级包（不调用模型）",
    )
    upgrade_parser.add_argument("--account-id", required=True)
    upgrade_parser.add_argument("--style-profile-id", required=True)
    upgrade_parser.add_argument(
        "--style-root",
        default=str(PROJECT_ROOT / "outputs" / "style_packages"),
        help="风格包根目录",
    )
    upgrade_parser.add_argument("--output-root", default="outputs/account_knowledge_lab")
    upgrade_parser.add_argument("--limit", type=int)
    upgrade_parser.add_argument("--no-archived", action="store_true", help="不读取历史归档改写记录")
    upgrade_parser.add_argument("--overwrite", action="store_true")

    distill_parser = sub.add_parser("distill-account", help="写入已得到的账号级 L1-L6 蒸馏 JSON")
    distill_parser.add_argument("--account-id", required=True)
    distill_parser.add_argument("--result-file", required=True)
    distill_parser.add_argument("--overwrite", action="store_true")

    index_parser = sub.add_parser("index", help="从 Vault 重建本地索引")
    index_parser.add_argument("--account-id", action="append", dest="account_ids")
    index_parser.add_argument("--output")

    search_parser = sub.add_parser("search", help="在本地索引中按账号检索")
    search_parser.add_argument("--account-id", required=True)
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--index", required=True)
    search_parser.add_argument("--top-k", type=int, default=5)

    negative_parser = sub.add_parser("negative-search", help="只检索 C 级作品的避错案例")
    negative_parser.add_argument("--account-id", required=True)
    negative_parser.add_argument("--query", required=True)
    negative_parser.add_argument("--index", required=True)
    negative_parser.add_argument("--top-k", type=int, default=5)
    negative_parser.add_argument("--published-after", default="")

    topic_plan_parser = sub.add_parser("topic-plan", help="从本地索引生成待审核 V2 选题方案")
    topic_plan_parser.add_argument("--event-file", required=True)
    topic_plan_parser.add_argument("--account-id", required=True)
    topic_plan_parser.add_argument("--reference-account-id", action="append", dest="reference_account_ids")
    topic_plan_parser.add_argument("--index", required=True)
    topic_plan_parser.add_argument("--top-k", type=int, default=5)
    topic_plan_parser.add_argument("--domain", default="", help="按作品/认知元数据领域过滤")
    topic_plan_parser.add_argument("--published-after", default="", help="只检索该日期之后的作品（ISO 日期）")
    topic_plan_parser.add_argument("--output")
    topic_plan_parser.add_argument("--synthesize", action="store_true", help="显式调用内容分析模型综合多账号角度")

    work_live_parser = sub.add_parser("distill-work-live", help="显式调用内容分析模型蒸馏单篇作品并写回 Vault")
    work_live_parser.add_argument("--account-id", required=True)
    work_live_parser.add_argument("--title", required=True)
    work_live_parser.add_argument("--source-file", required=True)
    work_live_parser.add_argument("--source-url", default="")
    work_live_parser.add_argument("--platform", default="")
    work_live_parser.add_argument("--published-at", default="")
    work_live_parser.add_argument("--source-ref", default="")
    work_live_parser.add_argument("--overwrite", action="store_true")

    account_live_parser = sub.add_parser("distill-account-live", help="显式调用内容分析模型蒸馏账号级 Writing-DNA")
    account_live_parser.add_argument("--account-id", required=True)
    account_live_parser.add_argument("--surface-analysis")
    account_live_parser.add_argument("--batch-size", type=int, default=8)
    account_live_parser.add_argument("--overwrite", action="store_true")

    offline_candidate_parser = sub.add_parser(
        "offline-account-candidate",
        help="从作品卡和本地原文生成五份 PROVISIONAL_OFFLINE 账号候选笔记，不调用模型",
    )
    offline_candidate_parser.add_argument("--account-id", required=True)
    offline_candidate_parser.add_argument("--overwrite", action="store_true")

    approve_dna_parser = sub.add_parser(
        "approve-account-dna",
        help="人工确认 READY_FOR_REVIEW 账号 DNA；PROVISIONAL_OFFLINE 候选版不可直接批准",
    )
    approve_dna_parser.add_argument("--account-id", required=True)
    approve_dna_parser.add_argument("--reviewer", required=True)
    approve_dna_parser.add_argument("--notes", default="")

    preflight_parser = sub.add_parser("preflight-live", help="检查真实蒸馏所需环境，不调用模型")
    preflight_parser.add_argument("--account-id", required=True)
    preflight_parser.add_argument("--min-works", type=int, default=30)

    grade_parser = sub.add_parser("grade-work", help="人工确认作品内容/传播等级并写回评级历史")
    grade_parser.add_argument("--account-id", required=True)
    grade_parser.add_argument("--work-id", required=True)
    grade_parser.add_argument("--content-grade", required=True, choices=["S", "A", "B", "C"])
    grade_parser.add_argument("--performance-grade", default="UNRATED", choices=["S", "A", "B", "C", "UNRATED"])
    grade_parser.add_argument("--reviewer", required=True)
    grade_parser.add_argument("--notes", default="")

    topic_parser = sub.add_parser("validate-topic", help="校验 topic plan JSON")
    topic_parser.add_argument("--file", required=True)
    draft_parser = sub.add_parser("validate-draft", help="校验 V2 文案草稿 JSON")
    draft_parser.add_argument("--file", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "init-account":
            repository = _repository(args.vault)
            path = repository.initialize_account(
                args.account_id,
                account_name=args.account_name,
                overwrite_account_note=args.overwrite_account_note,
            )
            _json_dump({"status": "ready", "account_id": args.account_id, "account_note": str(path)})
            return 0

        if args.command == "import-corpus":
            repository = _repository(args.vault)
            report = import_corpus(
                repository,
                account_id=args.account_id,
                corpus_path=args.corpus,
                platform=args.platform,
                default_content_grade=args.default_content_grade,
                copy_raw=not args.no_copy_raw,
            )
            _json_dump(report.to_dict())
            return 0 if report.failed == 0 else 2

        if args.command == "analyze-corpus":
            report = analyze_corpus(args.corpus)
            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _json_dump(report)
            return 0

        if args.command == "upgrade-profile":
            repository = _repository(args.vault)
            result = run_style_package_upgrade(
                repository,
                account_id=args.account_id,
                style_root=args.style_root,
                style_profile_id=args.style_profile_id,
                output_root=args.output_root,
                include_archived=not args.no_archived,
                limit=args.limit,
                overwrite=args.overwrite,
            )
            _json_dump(result)
            return 0 if result.get("status") == "review_required" else 2

        if args.command == "distill-account":
            repository = _repository(args.vault)
            result = json.loads(Path(args.result_file).read_text(encoding="utf-8"))
            works = repository.list_work_cards(args.account_id)
            bundle = build_account_distillation(account_id=args.account_id, works=works, result=result)
            paths = write_account_distillation(repository, bundle, overwrite=args.overwrite)
            _json_dump({"status": "ready", "bundle": bundle, "paths": [str(path.resolve()) for path in paths]})
            return 0

        if args.command == "index":
            repository = _repository(args.vault)
            index = LocalKnowledgeIndex()
            count = index.rebuild_from_repository(repository, account_ids=args.account_ids)
            output = Path(args.output) if args.output else _default_index_path(repository)
            index.dump(output)
            _json_dump({"status": "ready", "documents": count, "index_path": str(output.resolve())})
            return 0

        if args.command == "search":
            index = LocalKnowledgeIndex.load(Path(args.index))
            results = AccountKnowledgeRetriever(index).retrieve_account(
                account_id=args.account_id,
                query=args.query,
                top_k=args.top_k,
            )
            _json_dump({"status": "ready", "account_id": args.account_id, "results": results})
            return 0

        if args.command == "negative-search":
            index = LocalKnowledgeIndex.load(Path(args.index))
            results = AccountKnowledgeRetriever(index).retrieve_negative_cases(
                account_id=args.account_id,
                query=args.query,
                top_k=args.top_k,
                published_after=args.published_after,
            )
            _json_dump({"status": "ready", "account_id": args.account_id, "results": results, "provider_mode": "local_retrieval"})
            return 0

        if args.command == "topic-plan":
            index = LocalKnowledgeIndex.load(Path(args.index))
            event_payload = json.loads(Path(args.event_file).read_text(encoding="utf-8"))
            event_card = EventCard(**event_payload)
            plan = build_topic_plan_from_index(
                index=index,
                event_card=event_card,
                target_account_id=args.account_id,
                reference_account_ids=args.reference_account_ids or [],
                retrieval_policy=RetrievalPolicy(
                    top_k_per_account=args.top_k,
                    domain=args.domain,
                    published_after=args.published_after,
                ),
            )
            if args.synthesize:
                style_root = Path(
                    os.environ.get(
                        "ACCOUNT_KNOWLEDGE_STYLE_PACKAGE_ROOT",
                        str(PROJECT_ROOT / "outputs" / "style_packages"),
                    )
                )
                plan = AccountKnowledgeModelProvider.from_runtime_config(
                    style_profile_loader=StylePackageStore(style_root).get_profile,
                    api_management_config_path=_api_management_config_path(),
                    use_api_management_runtime=True,
                ).synthesize_topic(topic_plan=plan)
            artifact_root = args.output or os.environ.get("ACCOUNT_KNOWLEDGE_LAB_OUTPUT_PATH", "outputs/account_knowledge_lab")
            if args.output and str(args.output).lower().endswith(".json"):
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            else:
                output_path = LabArtifactStore(artifact_root).save_topic_plan(plan)
            _json_dump({"status": "ready", "topic_plan": plan, "artifact_path": str(output_path.resolve())})
            return 0

        if args.command == "distill-work-live":
            repository = _repository(args.vault)
            source_content = Path(args.source_file).read_text(encoding="utf-8")
            provider = AccountKnowledgeModelProvider.from_runtime_config(
                api_management_config_path=_api_management_config_path(),
                use_api_management_runtime=True,
            )
            repository.initialize_account(args.account_id)
            card = provider.distill_work_card(
                account_id=args.account_id,
                title=args.title,
                source_content=source_content,
                source_url=args.source_url,
                platform=args.platform,
                published_at=args.published_at,
                source_ref="",
            )
            source_ref = args.source_ref or f"raw_works/{card.work_id}{Path(args.source_file).suffix.lower() or '.txt'}"
            card.source_ref = source_ref
            card.validate()
            repository.save_raw_source(args.account_id, source_ref, source_content.encode("utf-8"), overwrite=args.overwrite)
            card_path = repository.save_work_card(card, overwrite=args.overwrite)
            # 蒸馏更新不能丢失导入器写入的 video_id/source_quality/
            # transcript_status 等来源证据；只覆盖模型相关字段。
            try:
                existing_metadata = repository.load_work_metadata(args.account_id, card.work_id)
            except Exception:
                existing_metadata = {}
            metadata = {
                **existing_metadata,
                "schema_version": 1,
                "work_id": card.work_id,
                "account_id": card.account_id,
                "title": card.title,
                "date": card.published_at,
                "platform": card.platform,
                "source_url": card.source_url,
                "source_ref": card.source_ref,
                "content_hash": card.content_hash,
                "article_type": "模型蒸馏待审核",
                "notable": "由内容分析角色模型蒸馏；内容等级和传播等级仍待人工确认",
                "distilled_by": card.distilled_by,
                "prompt_version": card.prompt_version,
                "distilled_at": card.distilled_at,
            }
            repository.save_work_metadata(
                args.account_id,
                card.work_id,
                metadata,
                overwrite=args.overwrite,
            )
            _json_dump({"status": "ready", "work_card": card.to_dict(), "work_card_path": str(card_path), "provider_mode": "runtime"})
            return 0

        if args.command == "distill-account-live":
            repository = _repository(args.vault)
            repository.initialize_account(args.account_id)
            all_cards = repository.list_work_cards(args.account_id)
            # 已归档/拒绝的历史卡不再参与账号级认知蒸馏，避免历史重复
            # 或失败版本污染当前 Writing-DNA。
            cards = [card for card in all_cards if card.knowledge_status not in {"ARCHIVED", "REJECTED"}]
            works = []
            missing_raw: list[str] = []
            for card in cards:
                value = card.to_dict()
                if not card.source_ref:
                    missing_raw.append(card.work_id)
                    continue
                try:
                    value["source_content"] = repository.load_raw_source(args.account_id, card.source_ref)
                except Exception:
                    missing_raw.append(card.work_id)
                    continue
                works.append(value)
            if missing_raw:
                raise ValueError(
                    "账号蒸馏要求每篇作品都有可读取的完整原文；缺失作品：" + ", ".join(missing_raw)
                )
            surface = None
            if args.surface_analysis:
                surface = json.loads(Path(args.surface_analysis).read_text(encoding="utf-8"))
            provider = AccountKnowledgeModelProvider.from_runtime_config(
                api_management_config_path=_api_management_config_path(),
                use_api_management_runtime=True,
            )
            result = provider.distill_account(
                account_id=args.account_id,
                works=works,
                surface_analysis=surface,
                batch_size=args.batch_size,
            )
            bundle = build_account_distillation(account_id=args.account_id, works=cards, result=result)
            paths = write_account_distillation(repository, bundle, overwrite=args.overwrite)
            _json_dump({"status": "ready", "bundle": bundle, "paths": [str(path) for path in paths], "provider_mode": "runtime"})
            return 0

        if args.command == "offline-account-candidate":
            repository = _repository(args.vault)
            repository.initialize_account(args.account_id)
            all_cards = repository.list_work_cards(args.account_id)
            cards = [card for card in all_cards if card.knowledge_status not in {"ARCHIVED", "REJECTED"}]
            if not cards:
                raise ValueError("当前账号没有可用于离线候选版的有效作品卡")
            source_texts: dict[str, str] = {}
            missing_raw: list[str] = []
            for card in cards:
                if not card.source_ref:
                    missing_raw.append(card.work_id)
                    continue
                try:
                    source_texts[card.work_id] = repository.load_raw_source(args.account_id, card.source_ref)
                except Exception:
                    missing_raw.append(card.work_id)
            if missing_raw:
                raise ValueError(
                    "离线候选版要求每篇有效作品都有可读取的本地原文；缺失作品：" + ", ".join(missing_raw)
                )
            bundle = build_offline_candidate_bundle(
                account_id=args.account_id,
                works=cards,
                source_texts=source_texts,
            )
            paths = write_offline_candidate_notes(repository, bundle, overwrite=args.overwrite)
            _json_dump(
                {
                    "status": "ready",
                    "account_id": args.account_id,
                    "sample_count": bundle["sample_count"],
                    "source_count": len(source_texts),
                    "distillation_status": bundle["status"],
                    "distillation_method": bundle["distillation_method"],
                    "knowledge_status": "REVIEW_PENDING",
                    "evidence_work_id_count": len(bundle["evidence_work_ids"]),
                    "paths": [str(path.resolve()) for path in paths],
                    "provider_mode": "offline_deterministic",
                }
            )
            return 0

        if args.command == "approve-account-dna":
            repository = _repository(args.vault)
            paths = repository.approve_account_dna(
                args.account_id,
                reviewer=args.reviewer,
                notes=args.notes,
            )
            _json_dump(
                {
                    "status": "ready",
                    "account_id": args.account_id,
                    "knowledge_status": "APPROVED",
                    "reviewer": args.reviewer,
                    "paths": [str(path.resolve()) for path in paths],
                }
            )
            return 0

        if args.command == "preflight-live":
            if args.min_works <= 0:
                raise ValueError("--min-works 必须是正整数")
            repository = _repository(args.vault)
            repository.initialize_account(args.account_id)
            all_cards = repository.list_work_cards(args.account_id)
            cards = [card for card in all_cards if card.knowledge_status not in {"ARCHIVED", "REJECTED"}]
            missing_raw: list[str] = []
            raw_count = 0
            for card in cards:
                if not card.source_ref:
                    missing_raw.append(card.work_id)
                    continue
                try:
                    repository.load_raw_source(args.account_id, card.source_ref)
                    raw_count += 1
                except Exception:
                    missing_raw.append(card.work_id)
            api_config = _api_management_config_path()
            api_status = apply_api_management_model_runtime(api_config)
            provider_status = {"ready": False, "content_transports": 0, "style_transports": 0, "review_transports": 0}
            provider_error = ""
            try:
                provider = AccountKnowledgeModelProvider.from_runtime_config(
                    api_management_config_path=api_config,
                    use_api_management_runtime=True,
                )
                try:
                    language_model_api_order = json.loads(
                        os.environ.get("API_MANAGEMENT_LANGUAGE_MODEL_ORDER", "[]")
                    )
                except json.JSONDecodeError:
                    language_model_api_order = []
                if not isinstance(language_model_api_order, list):
                    language_model_api_order = []
                model_name = lambda transport: str(getattr(getattr(transport, "config", None), "model", "") or "")
                provider_status = {
                    "ready": True,
                    "content_transports": len(provider.content_transport.transports),
                    "content_unique_credentials": len({
                        str(getattr(getattr(item, "config", None), "api_key", ""))
                        for item in provider.content_transport.transports
                        if str(getattr(getattr(item, "config", None), "api_key", ""))
                    }),
                    "story_writer_backup_configured": bool(os.environ.get("STORY_WRITER_ARK_BACKUP_API_KEY", "").strip()),
                    "language_model_api_order": language_model_api_order,
                    "director_seed21_primary_configured": bool(
                        os.environ.get("VOICE_DIRECTOR_PRIMARY_API_KEY", "").strip()
                    ),
                    "director_seed21_backup_configured": bool(
                        os.environ.get("VOICE_DIRECTOR_BACKUP_API_KEY", "").strip()
                    ),
                    "style_transports": len(provider.style_transport.transports),
                    "review_transports": len(provider.review_transport.transports) if provider.review_transport else 0,
                    "content_models": list(dict.fromkeys(model_name(item) for item in provider.content_transport.transports if model_name(item))),
                    "style_models": list(dict.fromkeys(model_name(item) for item in provider.style_transport.transports if model_name(item))),
                    "review_models": list(dict.fromkeys(model_name(item) for item in (provider.review_transport.transports if provider.review_transport else []) if model_name(item))),
                }
            except Exception as exc:
                provider_error = type(exc).__name__
            style_root = Path(
                os.environ.get(
                    "ACCOUNT_KNOWLEDGE_STYLE_PACKAGE_ROOT",
                    str(PROJECT_ROOT / "outputs" / "style_packages"),
                )
            ).expanduser()
            profiles = StylePackageStore(style_root).list_profiles()
            approved_profiles = [item for item in profiles if str(item.get("review_status") or "").upper() == "APPROVED"]
            checks = {
                "api_management_config": api_status.get("status") in {"ready", "partial"},
                "provider_auth": provider_status["ready"],
                "has_work_cards": bool(cards),
                "all_raw_sources_readable": bool(cards) and not missing_raw,
                "minimum_work_count": len(cards) >= args.min_works,
                "approved_style_profile": bool(approved_profiles),
            }
            ready = all(checks.values())
            _json_dump(
                {
                    "status": "ready" if ready else "blocked",
                    "account_id": args.account_id,
                    "vault_path": str(repository.vault_path),
                    "work_count": len(cards),
                    "archived_or_rejected_work_count": len(all_cards) - len(cards),
                    "raw_source_count": raw_count,
                    "missing_raw_source_work_ids": missing_raw,
                    "minimum_work_count": args.min_works,
                    "approved_style_profile_count": len(approved_profiles),
                    "api_management": {
                        "config_path": str(api_config),
                        "status": api_status.get("status"),
                        "model_count": api_status.get("model_count", 0),
                    },
                    "provider": provider_status,
                    "provider_error_type": provider_error,
                    "checks": checks,
                    "message": "可执行真实蒸馏" if ready else "请先补齐阻断项；当前不会自动调用模型",
                }
            )
            return 0 if ready else 2

        if args.command == "grade-work":
            repository = _repository(args.vault)
            card, body = repository.load_work_card(args.account_id, args.work_id)
            grade = apply_manual_grade(
                card,
                content_grade=args.content_grade,
                performance_grade=args.performance_grade,
                reviewer=args.reviewer,
                notes=args.notes,
            )
            card_path = repository.save_work_card(card, body=body, overwrite=True)
            grade_path = repository.save_grade_card(grade)
            _json_dump({
                "status": "ready",
                "work_card": card.to_dict(),
                "grade_card": grade.to_dict(),
                "work_card_path": str(card_path.resolve()),
                "grade_card_path": str(grade_path.resolve()),
            })
            return 0

        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if args.command == "validate-topic":
            _json_dump({"status": "valid", "topic_plan": validate_topic_plan(payload)})
        else:
            _json_dump({"status": "valid", "draft": validate_copywriting_draft(payload)})
        return 0
    except Exception as exc:
        _json_dump({"status": "blocked", "error_type": type(exc).__name__, "message": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
