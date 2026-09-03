"""逐篇执行账号作品真实蒸馏，并保留导入来源元数据。

该入口是 ``distill-work-live`` 的批处理封装：一次只处理一个作品，
模型返回经过契约校验后才覆盖同一 ``work_id`` 的作品卡。已经有
``distilled_by`` 的作品默认跳过，便于网络中断后安全续跑。

本工具会产生内容分析模型请求/费用，必须由调用者显式执行。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workflow_1256.account_knowledge import (  # noqa: E402
    AccountKnowledgeModelProvider,
    ObsidianRepository,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _api_config_path() -> Path:
    return Path(
        os.environ.get(
            "API_MANAGEMENT_CONFIG_PATH",
            str(PROJECT_ROOT / ".runtime-governance-live" / "data" / "VideoProductionConsole" / "api_management.json"),
        )
    ).expanduser()


def _metadata_after_distill(repository: ObsidianRepository, account_id: str, card) -> dict[str, Any]:
    try:
        existing = repository.load_work_metadata(account_id, card.work_id)
    except Exception:
        existing = {}
    return {
        **existing,
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


def run_batch(*, vault: Path | str, account_id: str, report_path: Path | str | None = None) -> dict[str, Any]:
    repository = ObsidianRepository(vault)
    repository.initialize_account(account_id)
    cards = repository.list_work_cards(account_id)
    provider = AccountKnowledgeModelProvider.from_runtime_config(
        api_management_config_path=_api_config_path(),
        use_api_management_runtime=True,
    )
    report: dict[str, Any] = {
        "status": "running",
        "account_id": account_id,
        "vault_path": str(repository.vault_path),
        "started_at": _now(),
        "discovered": len(cards),
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "items": [],
    }
    for ordinal, old_card in enumerate(cards, start=1):
        item: dict[str, Any] = {"ordinal": ordinal, "work_id": old_card.work_id, "title": old_card.title}
        if old_card.distilled_by:
            report["skipped"] += 1
            item.update({"status": "skipped", "reason": "已有蒸馏结果，支持断点续跑"})
            report["items"].append(item)
            continue
        try:
            source_content = repository.load_raw_source(account_id, old_card.source_ref)
            distilled = provider.distill_work_card(
                account_id=account_id,
                title=old_card.title,
                source_content=source_content,
                source_url=old_card.source_url,
                platform=old_card.platform,
                published_at=old_card.published_at,
                source_ref=old_card.source_ref,
                prompt_version="account-knowledge-v1",
            )
            if distilled.work_id != old_card.work_id:
                raise ValueError(
                    f"模型蒸馏生成的 work_id 不匹配：expected={old_card.work_id}, actual={distilled.work_id}"
                )
            repository.save_work_card(distilled, overwrite=True)
            repository.save_work_metadata(
                account_id,
                distilled.work_id,
                _metadata_after_distill(repository, account_id, distilled),
                overwrite=True,
            )
            report["processed"] += 1
            item.update({"status": "processed", "knowledge_status": distilled.knowledge_status})
        except Exception as exc:  # per-work report; no silent success
            report["failed"] += 1
            item.update({"status": "failed", "error_type": type(exc).__name__, "message": str(exc)})
        report["items"].append(item)
        print(
            json.dumps(
                {"progress": f"{ordinal}/{len(cards)}", "status": item["status"], "work_id": old_card.work_id},
                ensure_ascii=True,
            ),
            flush=True,
        )
    report["status"] = "ready" if report["failed"] == 0 else "blocked"
    report["finished_at"] = _now()
    if report_path:
        target = Path(report_path).expanduser().resolve()
    else:
        target = (PROJECT_ROOT / "outputs" / "account_knowledge_lab" / f"live_work_distillation_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(target)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量执行账号作品真实蒸馏（会产生模型请求）")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    try:
        result = run_batch(vault=args.vault, account_id=args.account_id, report_path=args.report)
        print(json.dumps({k: v for k, v in result.items() if k != "items"}, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" else 2
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
