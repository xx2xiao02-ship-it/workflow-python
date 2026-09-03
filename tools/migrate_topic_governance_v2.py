"""选题治理 V2 迁移预演与哈希报告。

默认只读：读取当前治理库和历史选题库，计算可比字段的哈希并写出报告，
不删除、不覆盖任何旧数据。只有报告明确无冲突时，才允许进入人工确认的
正式删除阶段。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
TOPIC_DIR = ROOT / "outputs" / "topic_center"
DEFAULT_REPORT = ROOT / "reports" / "topic-center-migration-dry-run.json"


def _load(path: Path, *, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _selection_key(item: Mapping[str, Any]) -> str:
    selection_id = str(item.get("selection_id") or "").strip()
    if selection_id:
        return selection_id
    candidate_id = str(item.get("candidate_id") or "").strip()
    return f"topic-{candidate_id}" if candidate_id else ""


def _projection(item: Mapping[str, Any]) -> dict[str, Any]:
    """比较不会因 V1/V2 状态包装变化而失真的业务字段。"""
    return {
        "candidate_id": str(item.get("candidate_id") or ""),
        "title": str(item.get("title") or ""),
        "source_url": str(item.get("source_url") or ""),
        "source_name": str(item.get("source_name") or ""),
        "rank": item.get("rank"),
        "intent": str(item.get("intent") or ""),
        "tags": list(item.get("tags") or []) if isinstance(item.get("tags"), (list, tuple)) else [],
        "platform": str(item.get("platform") or ""),
        "source_metadata": dict(item.get("source_metadata") or {}) if isinstance(item.get("source_metadata"), Mapping) else {},
    }


def _records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def build_report() -> dict[str, Any]:
    governance = _load(TOPIC_DIR / "governance.json", default={})
    archive = _load(TOPIC_DIR / "selection_archive.json", default={})
    queue = _load(TOPIC_DIR / "collection_queue.json", default={})
    decisions = _load(TOPIC_DIR / "topic_decisions.json", default={})
    results = _load(TOPIC_DIR / "collection_results.json", default={})

    current = {
        str(key): dict(value)
        for key, value in (governance.get("selections", {}) if isinstance(governance, Mapping) else {}).items()
        if isinstance(value, Mapping)
    }
    legacy_items: dict[str, dict[str, Any]] = {}
    duplicate_ids: list[str] = []
    for source_name, payload in (("selection_archive", archive), ("collection_queue", queue)):
        items = _records(payload.get("items") if isinstance(payload, Mapping) else [])
        for item in items:
            key = _selection_key(item)
            if not key:
                continue
            if key in legacy_items and _sha(_projection(legacy_items[key])) != _sha(_projection(item)):
                duplicate_ids.append(key)
            legacy_items.setdefault(key, dict(item))

    current_projection_hashes = {key: _sha(_projection(item)) for key, item in current.items()}
    legacy_projection_hashes = {key: _sha(_projection(item)) for key, item in legacy_items.items()}
    common = sorted(set(current_projection_hashes) & set(legacy_projection_hashes))
    hash_mismatches = [key for key in common if current_projection_hashes[key] != legacy_projection_hashes[key]]
    missing_in_v2 = sorted(set(legacy_projection_hashes) - set(current_projection_hashes))
    extra_in_v2 = sorted(set(current_projection_hashes) - set(legacy_projection_hashes))
    decision_ids = {str(key) for key in decisions} if isinstance(decisions, Mapping) else set()
    decision_selection_ids = {f"topic-{key}" for key in decision_ids}

    files: dict[str, Any] = {}
    for name, payload in (
        ("governance.json", governance),
        ("selection_archive.json", archive),
        ("collection_queue.json", queue),
        ("collection_results.json", results),
        ("topic_decisions.json", decisions),
    ):
        path = TOPIC_DIR / name
        files[name] = {
            "path": str(path),
            "exists": path.exists(),
            "bytes": path.stat().st_size if path.exists() else 0,
            "sha256_raw": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "",
            "record_count": (
                len(payload) if isinstance(payload, list) else
                len(payload.get("items", [])) if isinstance(payload, Mapping) and isinstance(payload.get("items"), list) else
                len(payload.get("selections", {})) if name == "governance.json" and isinstance(payload, Mapping) else
                len(payload) if isinstance(payload, Mapping) else 0
            ),
        }

    safe_to_delete = not (duplicate_ids or missing_in_v2 or hash_mismatches)
    return {
        "report_version": "topic-governance-migration-dry-run-v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "read_only",
        "safe_to_delete": safe_to_delete,
        "current_v2": {
            "schema_version": governance.get("schema_version") if isinstance(governance, Mapping) else "",
            "selection_count": len(current),
            "source_count": len(governance.get("sources", {})) if isinstance(governance, Mapping) and isinstance(governance.get("sources"), Mapping) else 0,
            "selection_ids": sorted(current),
            "projection_sha256": current_projection_hashes,
        },
        "legacy": {
            "archive_item_count": len(_records(archive.get("items") if isinstance(archive, Mapping) else [])),
            "queue_item_count": len(_records(queue.get("items") if isinstance(queue, Mapping) else [])),
            "unique_selection_count": len(legacy_items),
            "selection_ids": sorted(legacy_items),
            "projection_sha256": legacy_projection_hashes,
            "decision_candidate_count": len(decision_ids),
            "decision_selection_ids": sorted(decision_selection_ids),
            "collection_result_topic_count": len(results) if isinstance(results, Mapping) else 0,
        },
        "comparison": {
            "common_ids": common,
            "missing_in_v2": missing_in_v2,
            "extra_in_v2": extra_in_v2,
            "hash_mismatches": hash_mismatches,
            "duplicate_conflicts": sorted(set(duplicate_ids)),
        },
        "files": files,
        "deletion_gate": {
            "requires_user_confirmation": True,
            "blocked_reason": "存在未迁移历史记录或哈希冲突" if not safe_to_delete else "只读核对无冲突，但仍需用户明确确认",
        },
    }


def apply_unique_records(report: Mapping[str, Any]) -> dict[str, Any]:
    """把 V1 中仅存在于旧库的选题导入 V2；不删除旧文件。"""
    if not isinstance(report.get("comparison"), Mapping):
        raise RuntimeError("迁移报告格式无效")
    missing = [str(item) for item in report["comparison"].get("missing_in_v2", []) if str(item).strip()]
    if not missing:
        governance = _load(TOPIC_DIR / "governance.json", default={})
        existing = [
            str(key) for key, value in (governance.get("selections", {}) if isinstance(governance, Mapping) else {}).items()
            if isinstance(value, Mapping) and isinstance(value.get("migration"), Mapping)
        ]
        upgraded = str(governance.get("schema_version") or "") != "topic-writing-governance-v2" if isinstance(governance, Mapping) else False
        if upgraded and isinstance(governance, dict):
            governance["schema_version"] = "topic-writing-governance-v2"
            governance["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            governance_path = TOPIC_DIR / "governance.json"
            temporary = governance_path.with_name(f".{governance_path.name}.migration.tmp")
            temporary.write_text(json.dumps(governance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(governance_path)
        return {"imported": [], "already_imported": sorted(existing), "skipped": [], "schema_upgraded": upgraded, "message": "没有仅存在于旧库的选题"}
    governance_path = TOPIC_DIR / "governance.json"
    governance = _load(governance_path, default={})
    if not isinstance(governance, dict):
        governance = {}
    selections = governance.setdefault("selections", {})
    if not isinstance(selections, dict):
        selections = {}
        governance["selections"] = selections
    legacy_sources: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for filename in ("selection_archive.json", "collection_queue.json"):
        payload = _load(TOPIC_DIR / filename, default={})
        for item in _records(payload.get("items") if isinstance(payload, Mapping) else []):
            key = _selection_key(item)
            if key and key not in legacy_sources:
                legacy_sources[key] = (filename, item)
    imported: list[str] = []
    skipped: list[str] = []
    now = datetime.now(UTC).isoformat(timespec="seconds")
    state_map = {
        "content_missing": "CONTENT_MISSING",
        "queued": "ACCEPTED",
        "archived": "ACCEPTED",
        "archived_later": "ACCEPTED",
    }
    for key in missing:
        source = legacy_sources.get(key)
        if source is None or key in selections:
            skipped.append(key)
            continue
        filename, item = source
        collection = item.get("collection") if isinstance(item.get("collection"), Mapping) else {}
        state = state_map.get(str(collection.get("state") or "").strip().lower(), "ACCEPTED")
        created_at = str(item.get("created_at") or now)
        updated_at = str(item.get("updated_at") or created_at)
        record = {
            "selection_id": key,
            "candidate_id": str(item.get("candidate_id") or "").strip(),
            "decision": str(item.get("decision") or "accepted").strip() or "accepted",
            "title": str(item.get("title") or "").strip(),
            "source_url": str(item.get("source_url") or "").strip(),
            "source_name": str(item.get("source_name") or "").strip(),
            "rank": item.get("rank"),
            "intent": str(item.get("intent") or "").strip(),
            "tags": list(item.get("tags") or []) if isinstance(item.get("tags"), list) else [],
            "platform": str(item.get("platform") or "article").strip() or "article",
            "source_metadata": dict(item.get("source_metadata") or {}) if isinstance(item.get("source_metadata"), Mapping) else {},
            "primary_content_id": "",
            "reference_content_ids": [],
            "content_items": [],
            "content_bundle_status": "not_requested",
            "state": state,
            "state_history": [{"state": state, "at": updated_at}],
            "source_content_id": "",
            "copy_rewrite_id": "",
            "message": str((collection.get("message") if isinstance(collection, Mapping) else "") or "历史选题已迁移到 V2，等待重新采集真实正文"),
            "created_at": created_at,
            "updated_at": updated_at,
            "migration": {
                "from_schema": "topic-selection-v1",
                "from_file": filename,
                "original_record_sha256": _sha(item),
                "legacy_collection": dict(collection),
                "legacy_writing": dict(item.get("writing") or {}) if isinstance(item.get("writing"), Mapping) else {},
                "migrated_at": now,
            },
        }
        selections[key] = record
        imported.append(key)
    needs_schema_upgrade = str(governance.get("schema_version") or "") != "topic-writing-governance-v2"
    if imported or needs_schema_upgrade:
        governance["schema_version"] = "topic-writing-governance-v2"
        governance["updated_at"] = now
        temporary = governance_path.with_name(f".{governance_path.name}.migration.tmp")
        temporary.write_text(json.dumps(governance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(governance_path)
    return {
        "imported": imported,
        "already_imported": [],
        "skipped": skipped,
        "schema_upgraded": needs_schema_upgrade,
        "message": "仅导入唯一旧记录，旧库仍原样保留",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成选题治理 V2 迁移只读报告")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply-unique", action="store_true", help="导入仅存在于旧库的选题，不删除旧库")
    args = parser.parse_args()
    report = build_report()
    applied = apply_unique_records(report) if args.apply_unique else None
    if applied:
        before_ids = set(str(item) for item in report["current_v2"].get("selection_ids", []))
        imported_ids = set(str(item) for item in applied.get("imported", []))
        if not imported_ids:
            imported_ids = set(str(item) for item in applied.get("already_imported", []))
        if imported_ids:
            before_doc = _load(TOPIC_DIR / "governance.json", default={})
            before_selections = before_doc.get("selections", {}) if isinstance(before_doc, Mapping) else {}
            before_projection = {
                str(key): _sha(_projection(value))
                for key, value in before_selections.items()
                if isinstance(value, Mapping) and str(key) not in imported_ids
            }
        else:
            before_projection = {}
        report = build_report()
        report["applied"] = applied
        report["migration"] = {
            "operation": "import_unique_legacy_records",
            "before_selection_count": len(before_ids - imported_ids),
            "after_selection_count": int(report["current_v2"].get("selection_count") or 0),
            "imported_selection_ids": sorted(imported_ids),
            "before_projection_sha256": before_projection,
            "after_projection_sha256": report["current_v2"].get("projection_sha256", {}),
            "legacy_files_preserved": True,
            "deletion_performed": False,
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output = {"report": str(args.report), "safe_to_delete": report["safe_to_delete"], "comparison": report["comparison"]}
    if applied:
        output["applied"] = applied
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
