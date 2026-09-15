"""独立选题治理存储。

该文件只保存选题、正文来源和状态关系，运行时路径由服务注入到源项目
之外。它不读取旧控制台文件，也不把凭据、原始供应商响应或历史产物写入
源码目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .contracts import adapt_topic_content
from .errors import NotFoundError, PersistenceError, ValidationError

SCHEMA_VERSION = "topic-writing-governance-v2"
MIN_SOURCE_TEXT_CHARS = 80
MAX_SOURCE_TEXT_CHARS = 200_000
MAX_CONTENT_ITEMS = 50
SELECTION_STATES = frozenset(
    {
        "ACCEPTED",
        "EXTRACTING",
        "CONTENT_READY",
        "WRITING",
        "REVIEW_PENDING",
        "REVIEW_BLOCKED",
        "APPROVED",
        "DIRECTOR_READY",
        "EXTRACTION_FAILED",
        "EXTRACTION_BLOCKED",
        "CONTENT_MISSING",
        "EXTRACTION_DELEGATED",
    }
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def _clean_text(value: Any, *, field: str, max_chars: int, allow_empty: bool = True) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not allow_empty and not text:
        raise ValidationError(f"{field} 不能为空")
    if len(text) > max_chars:
        raise ValidationError(f"{field} 不能超过 {max_chars} 个字符")
    return text


def normalize_source_text(value: Any, *, field: str = "正文") -> str:
    text = _clean_text(value, field=field, max_chars=MAX_SOURCE_TEXT_CHARS, allow_empty=False)
    if len(text) < MIN_SOURCE_TEXT_CHARS:
        raise ValidationError(f"{field} 至少需要 {MIN_SOURCE_TEXT_CHARS} 个字符，当前为 {len(text)} 个字符")
    return text


def _http_url(value: Any, *, field: str = "source_url", required: bool = False) -> str:
    text = _clean_text(value, field=field, max_chars=2_000)
    if not text:
        if required:
            raise ValidationError(f"{field} 必须是带域名的 HTTP(S) 地址")
        return ""
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(f"{field} 必须是带域名的 HTTP(S) 地址")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValidationError(f"{field} 不能包含凭据或 fragment")
    return text


def _safe_id(value: Any, field: str) -> str:
    text = _clean_text(value, field=field, max_chars=160, allow_empty=False)
    if "/" in text or "\\" in text or text in {".", ".."} or Path(text).name != text:
        raise ValidationError(f"{field} 不合法")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", text):
        raise ValidationError(f"{field} 只能包含字母、数字、点、下划线、冒号或连字符")
    return text


def _tags(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else ([value] if value else [])
    result: list[str] = []
    for item in values:
        text = _clean_text(item, field="tags", max_chars=80)
        if text and text not in result:
            result.append(text)
    return result[:100]


def _rank(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValidationError("rank 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("rank 必须是整数") from exc


def _content_items(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise ValidationError("content_items 必须是数组")
    result: list[dict[str, Any]] = []
    for raw in list(value)[:MAX_CONTENT_ITEMS]:
        if not isinstance(raw, Mapping):
            raise ValidationError("content_items 的每一项必须是对象")
        content_id = _clean_text(raw.get("content_id"), field="content_items.content_id", max_chars=160)
        title = _clean_text(raw.get("title"), field="content_items.title", max_chars=800)
        url = _http_url(raw.get("url") or raw.get("source_url"), field="content_items.url")
        if not content_id and not url:
            raise ValidationError("每条 content_item 至少需要 content_id 或真实 URL")
        result.append({
            "content_id": content_id or "content-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
            "provider": _clean_text(raw.get("provider"), field="content_items.provider", max_chars=80),
            "platform": _clean_text(raw.get("platform"), field="content_items.platform", max_chars=80),
            "content_type": _clean_text(raw.get("content_type"), field="content_items.content_type", max_chars=40),
            "duration_seconds": raw.get("duration_seconds"),
            "duration_source": _clean_text(raw.get("duration_source"), field="content_items.duration_source", max_chars=80),
            "content_preview": _clean_text(raw.get("content_preview"), field="content_items.content_preview", max_chars=360),
            "content_preview_source": _clean_text(raw.get("content_preview_source"), field="content_items.content_preview_source", max_chars=80),
            "title": title,
            "url": url,
            "author": _clean_text(raw.get("author"), field="content_items.author", max_chars=200),
            "published_at": _clean_text(raw.get("published_at"), field="content_items.published_at", max_chars=80),
            "metrics": dict(raw.get("metrics")) if isinstance(raw.get("metrics"), Mapping) else {},
            "native_score": raw.get("native_score"),
            "relevance_score": raw.get("relevance_score"),
            "matched_terms": _tags(raw.get("matched_terms")),
            "source_metadata": dict(raw.get("source_metadata")) if isinstance(raw.get("source_metadata"), Mapping) else {},
        })
    return result


def _append_state_history(record: dict[str, Any], state: str, timestamp: str | None = None) -> None:
    value = str(state or "").strip().upper()
    if not value:
        return
    history = record.get("state_history") if isinstance(record.get("state_history"), list) else []
    clean = [
        dict(item)
        for item in history
        if isinstance(item, Mapping) and str(item.get("state") or "").strip()
    ]
    if not clean or str(clean[-1].get("state") or "").strip().upper() != value:
        clean.append({"state": value, "at": timestamp or utc_now()})
    record["state_history"] = clean


class TopicGovernanceStore:
    """把选题、正文和交接状态保存在独立运行目录。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now(),
            "selections": {},
            "sources": {},
        }

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return self._empty()
        if not isinstance(value, Mapping):
            return self._empty()
        selections = value.get("selections") if isinstance(value.get("selections"), Mapping) else {}
        sources = value.get("sources") if isinstance(value.get("sources"), Mapping) else {}
        normalized_selections: dict[str, dict[str, Any]] = {}
        for key, item in selections.items():
            if not isinstance(item, Mapping):
                continue
            record = dict(item)
            if not isinstance(record.get("state_history"), list):
                state = str(record.get("state") or "ACCEPTED").strip().upper()
                record["state_history"] = [{
                    "state": state,
                    "at": str(record.get("updated_at") or record.get("created_at") or utc_now()),
                }]
            normalized_selections[str(key)] = record
        return {
            "schema_version": str(value.get("schema_version") or SCHEMA_VERSION),
            "updated_at": str(value.get("updated_at") or ""),
            "selections": normalized_selections,
            "sources": {str(key): dict(item) for key, item in sources.items() if isinstance(item, Mapping)},
        }

    def _write(self, value: Mapping[str, Any]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now(),
            "selections": _json_safe(value.get("selections") if isinstance(value.get("selections"), Mapping) else {}),
            "sources": _json_safe(value.get("sources") if isinstance(value.get("sources"), Mapping) else {}),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise PersistenceError("选题治理存储写入失败", code="governance_write_failed") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def save_selection(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValidationError("选题请求必须是对象")
        candidate_id = _safe_id(payload.get("candidate_id"), "candidate_id")
        title = _clean_text(payload.get("title"), field="title", max_chars=500, allow_empty=False)
        source_url = _http_url(payload.get("source_url"), required=True)
        selection_id = _safe_id(payload.get("selection_id") or f"topic-{candidate_id}", "selection_id")
        content_items = _content_items(payload.get("content_items"))
        primary_content_id = _clean_text(payload.get("primary_content_id"), field="primary_content_id", max_chars=160)
        if content_items:
            known_ids = {str(item.get("content_id") or "") for item in content_items}
            if not primary_content_id:
                raise ValidationError("建立选题包必须指定 primary_content_id")
            if primary_content_id not in known_ids:
                raise ValidationError("primary_content_id 不在已查询内容中")
            short_ids: list[str] = []
            for item in content_items:
                if str(item.get("content_type") or "").lower() != "video":
                    continue
                duration = item.get("duration_seconds")
                try:
                    if duration is not None and float(duration) <= 60:
                        short_ids.append(str(item.get("content_id") or ""))
                except (TypeError, ValueError):
                    continue
            if short_ids:
                raise ValidationError("不能选择时长不超过1分钟的视频")
        refs = _tags(payload.get("reference_content_ids"))
        known_ids = {str(item.get("content_id") or "") for item in content_items}
        refs = [item for item in dict.fromkeys(refs) if not known_ids or item in known_ids]
        if primary_content_id:
            refs = [item for item in refs if item != primary_content_id]
        source_metadata = payload.get("source_metadata")
        if not isinstance(source_metadata, Mapping):
            source_metadata = {}
        now = utc_now()
        with self._lock:
            document = self._read()
            previous = document["selections"].get(selection_id)
            previous = dict(previous) if isinstance(previous, Mapping) else {}
            previous_state = str(previous.get("state") or "ACCEPTED").strip().upper()
            if previous.get("decision") == "deleted" or previous_state not in SELECTION_STATES:
                previous_state = "ACCEPTED"
            record = {
                "selection_id": selection_id,
                "candidate_id": candidate_id,
                "decision": "accepted",
                "title": title,
                "source_url": source_url,
                "source_name": _clean_text(payload.get("source_name"), field="source_name", max_chars=200),
                "rank": _rank(payload.get("rank")),
                "intent": _clean_text(payload.get("intent"), field="intent", max_chars=1_000),
                "tags": _tags(payload.get("tags")),
                "platform": _clean_text(payload.get("platform") or "article", field="platform", max_chars=80),
                "source_metadata": dict(source_metadata),
                "primary_content_id": primary_content_id,
                "reference_content_ids": refs,
                "content_items": content_items,
                "content_bundle_status": "selected" if content_items else "not_requested",
                "state": previous_state,
                "state_history": list(previous.get("state_history") or []),
                "source_content_id": str(previous.get("source_content_id") or ""),
                "message": str(previous.get("message") or "选题已采用，等待正文提取"),
                "created_at": str(previous.get("created_at") or now),
                "updated_at": now,
                "queue_state": "ACTIVE",
            }
            _append_state_history(record, previous_state, now)
            document["selections"][selection_id] = record
            self._write(document)
            return dict(record)

    def get_selection(self, selection_id: str) -> dict[str, Any] | None:
        key = _safe_id(selection_id, "selection_id")
        with self._lock:
            value = self._read()["selections"].get(key)
        return dict(value) if isinstance(value, Mapping) else None

    def require_selection(self, selection_id: str) -> dict[str, Any]:
        value = self.get_selection(selection_id)
        if value is None:
            raise NotFoundError("未找到对应的选题")
        return value

    def list_selections(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._read()["selections"].values())
        values = [dict(item) for item in values if isinstance(item, Mapping) and item.get("decision") != "deleted"]
        values.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return values[: max(1, min(int(limit), 1_000))]

    def delete_selection(self, selection_id: str) -> dict[str, Any]:
        key = _safe_id(selection_id, "selection_id")
        with self._lock:
            document = self._read()
            record = document["selections"].get(key)
            if not isinstance(record, Mapping):
                raise NotFoundError("未找到对应的选题")
            state = str(record.get("state") or "").strip().upper()
            if state in {"EXTRACTING", "WRITING"}:
                raise ValidationError("选题正在采集或写作，暂不能删除")
            updated = dict(record)
            updated.update({"decision": "deleted", "updated_at": utc_now()})
            document["selections"][key] = updated
            self._write(document)
            return {"status": "deleted", "selection_id": key, "assets_preserved": True}

    def update_selection(
        self,
        selection_id: str,
        *,
        state: str | None = None,
        source_content_id: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        key = _safe_id(selection_id, "selection_id")
        with self._lock:
            document = self._read()
            record = document["selections"].get(key)
            if not isinstance(record, Mapping):
                raise NotFoundError("未找到对应的选题")
            updated = dict(record)
            if updated.get("decision") == "deleted":
                raise ValidationError("选题已删除，请重新采用后操作")
            if state is not None:
                normalized = str(state).strip().upper()
                if normalized not in SELECTION_STATES:
                    raise ValidationError(f"不支持的选题状态：{normalized}")
                updated["state"] = normalized
                _append_state_history(updated, normalized)
            if source_content_id is not None:
                updated["source_content_id"] = _safe_id(source_content_id, "source_content_id")
            if message is not None:
                updated["message"] = _clean_text(message, field="message", max_chars=2_000)
            updated["updated_at"] = utc_now()
            document["selections"][key] = updated
            self._write(document)
            return dict(updated)

    @staticmethod
    def _source_id(value: Any) -> str:
        return _safe_id(value, "source_content_id")

    def save_extraction_result(self, selection_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
        selection = self.require_selection(selection_id)
        if not isinstance(result, Mapping):
            raise ValidationError("采集结果必须是对象")
        collector_status = str(result.get("status") or "failed").strip().lower()
        content = _clean_text(result.get("content"), field="正文", max_chars=MAX_SOURCE_TEXT_CHARS)
        if collector_status == "ready" and len(content) < MIN_SOURCE_TEXT_CHARS:
            collector_status = "content_missing"
        status_map = {
            "ready": "CONTENT_READY",
            "content_ready": "CONTENT_READY",
            "content_missing": "CONTENT_MISSING",
            "blocked": "EXTRACTION_BLOCKED",
            "failed": "EXTRACTION_FAILED",
            "invalid_input": "EXTRACTION_FAILED",
            "delegated": "EXTRACTION_DELEGATED",
        }
        source_status = status_map.get(collector_status, "EXTRACTION_FAILED")
        final_url = str(result.get("final_url") or result.get("source_url") or selection.get("source_url") or "").strip()
        source_url = _http_url(final_url, field="source_url")
        explicit_source_id = str(result.get("source_content_id") or "").strip()
        if explicit_source_id:
            source_id = self._source_id(explicit_source_id)
        else:
            seed = "|".join((selection["selection_id"], source_url, content, source_status))
            source_id = "source-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        metadata = result.get("source_metadata")
        if not isinstance(metadata, Mapping):
            metadata = selection.get("source_metadata") if isinstance(selection.get("source_metadata"), Mapping) else {}
        now = utc_now()
        source = {
            "source_content_id": source_id,
            "selection_id": selection["selection_id"],
            "input_type": "news_article",
            "title": _clean_text(result.get("title") or selection.get("title"), field="title", max_chars=500, allow_empty=False),
            "source_url": source_url,
            "source_name": _clean_text(selection.get("source_name"), field="source_name", max_chars=200),
            "platform": _clean_text(result.get("platform") or selection.get("platform") or "article", field="platform", max_chars=80),
            "source_metadata": dict(metadata),
            "content": content if source_status == "CONTENT_READY" else "",
            "content_type": _clean_text(result.get("content_type") or "html_text", field="content_type", max_chars=80),
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest() if content else "",
            "status": source_status,
            "collector_status": collector_status,
            "transcript_status": _clean_text(result.get("transcript_status") or ("collected_html" if source_status == "CONTENT_READY" else ""), field="transcript_status", max_chars=80),
            "http_status": result.get("http_status"),
            "final_url": final_url,
            "error_code": _clean_text(result.get("error_code"), field="error_code", max_chars=120),
            "message": _clean_text(result.get("message"), field="message", max_chars=2_000),
            "queue_state": "ACTIVE",
            "provenance": [
                {
                    "source": str((metadata or {}).get("provider") or selection.get("source_name") or "topic-center"),
                    "source_url": source_url,
                    "collector_status": collector_status,
                    "evidence_level": "collected_content" if source_status == "CONTENT_READY" else "collection_attempt",
                }
            ],
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            document = self._read()
            previous = document["sources"].get(source_id)
            if isinstance(previous, Mapping):
                source["created_at"] = str(previous.get("created_at") or now)
            document["sources"][source_id] = source
            current = dict(document["selections"].get(selection["selection_id"]) or selection)
            current["state"] = source_status
            _append_state_history(current, source_status, now)
            current.update({
                "source_content_id": source_id,
                "queue_state": "ACTIVE",
                "message": source["message"] or ("正文已提取，允许进入文案创作" if source_status == "CONTENT_READY" else "正文提取未达到文案消费条件"),
                "updated_at": now,
            })
            document["selections"][selection["selection_id"]] = current
            self._write(document)
        return dict(source)

    def save_manual_source(
        self,
        text: Any,
        *,
        title: Any = "",
        source_name: Any = "",
        selection_id: Any = "",
        source_url: Any = "",
    ) -> dict[str, Any]:
        content = normalize_source_text(text, field="纯文案")
        selection_key = str(selection_id or "").strip()
        selection = self.require_selection(selection_key) if selection_key else None
        url = _http_url(source_url)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        source_id = "source-manual-" + digest[:16]
        now = utc_now()
        source = {
            "source_content_id": source_id,
            "selection_id": selection_key,
            "input_type": "manual_text",
            "title": _clean_text(title, field="title", max_chars=500) or "纯文案素材",
            "source_url": url,
            "source_name": _clean_text(source_name, field="source_name", max_chars=200),
            "platform": "manual",
            "source_metadata": {"intake": "manual_text"},
            "content": content,
            "content_type": "plain_text",
            "content_hash": digest,
            "status": "CONTENT_READY",
            "collector_status": "manual",
            "transcript_status": "manual_text",
            "http_status": None,
            "final_url": url,
            "error_code": "",
            "message": "纯文案已保存，可进入文案创作",
            "queue_state": "ACTIVE",
            "provenance": [{"source": "manual_text", "source_url": url, "evidence_level": "user_supplied_text"}],
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            document = self._read()
            previous = document["sources"].get(source_id)
            if isinstance(previous, Mapping):
                source["created_at"] = str(previous.get("created_at") or now)
            document["sources"][source_id] = source
            if selection:
                current = dict(document["selections"].get(selection_key) or selection)
                current.update({
                    "state": "CONTENT_READY",
                    "source_content_id": source_id,
                    "queue_state": "ACTIVE",
                    "message": source["message"],
                    "updated_at": now,
                })
                _append_state_history(current, "CONTENT_READY", now)
                document["selections"][selection_key] = current
            self._write(document)
        return dict(source)

    def get_source(self, source_content_id: str) -> dict[str, Any] | None:
        key = self._source_id(source_content_id)
        with self._lock:
            value = self._read()["sources"].get(key)
        return dict(value) if isinstance(value, Mapping) else None

    def get_source_for_copy(self, source_content_id: str) -> dict[str, Any]:
        source = self.get_source(source_content_id)
        if source is None:
            raise NotFoundError("未找到 source_content_id 对应的正文素材")
        if source.get("status") != "CONTENT_READY" or len(str(source.get("content") or "")) < MIN_SOURCE_TEXT_CHARS:
            raise ValidationError("正文素材尚未达到文案消费条件")
        return source

    def snapshot(self, selection_id: str) -> dict[str, Any]:
        selection = self.require_selection(selection_id)
        source_id = str(selection.get("source_content_id") or "").strip()
        source = self.get_source(source_id) if source_id else None
        return {"selection": selection, "source": source}

    def export_topic_content_manifest(self, source_content_id: str, *, project_id: str, run_id: str) -> dict[str, Any]:
        source = self.get_source_for_copy(source_content_id)
        return adapt_topic_content(source, project_id=project_id, run_id=run_id).to_dict()

    def writing_input(self, source_content_id: str, selection_id: str = "", *, project_id: str = "topic-center", run_id: str = "local") -> dict[str, Any]:
        source_key = self._source_id(source_content_id)
        source = self.get_source_for_copy(source_key)
        selected = self.require_selection(selection_id) if selection_id else None
        if selected and str(selected.get("source_content_id") or "") != source_key:
            raise ValidationError("选题与正文来源不匹配，已阻止交接")
        manifest = self.export_topic_content_manifest(source_key, project_id=project_id, run_id=run_id)
        return {
            "status": "ready",
            "selection_id": str(source.get("selection_id") or selection_id),
            "source_content_id": source_key,
            "title": manifest["title"],
            "content": manifest["content"],
            "source_url": manifest["source_url"],
            "topic_content_manifest": manifest,
        }

    def list_sources(self, *, limit: int = 100, include_archived: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._read()["sources"].values())
        values = [dict(item) for item in values if isinstance(item, Mapping)]
        if not include_archived:
            values = [item for item in values if str(item.get("queue_state") or "ACTIVE").upper() != "ARCHIVED"]
        values.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return values[: max(1, min(int(limit), 1_000))]

    def archive_source(self, source_content_id: str, *, reason: str = "user_remove_queue") -> dict[str, Any]:
        key = self._source_id(source_content_id)
        with self._lock:
            document = self._read()
            source = document["sources"].get(key)
            if not isinstance(source, Mapping):
                raise NotFoundError("未找到 source_content_id 对应的素材")
            copy_state = str(source.get("copy_state") or "").upper()
            if copy_state in {"WRITING", "REVIEW_PENDING", "REVIEW_BLOCKED", "APPROVED", "DIRECTOR_READY"}:
                raise ValidationError("该素材已进入文案或审核链路，不能移出队列")
            now = utc_now()
            updated = dict(source)
            updated.update({
                "queue_state": "ARCHIVED",
                "queue_archived_at": now,
                "queue_archive_reason": _clean_text(reason or "user_remove_queue", field="reason", max_chars=200),
                "updated_at": now,
            })
            document["sources"][key] = updated
            selection_id = str(updated.get("selection_id") or "").strip()
            selection = document["selections"].get(selection_id)
            if isinstance(selection, Mapping) and str(selection.get("source_content_id") or "") == key:
                current = dict(selection)
                current.update({"queue_state": "ARCHIVED", "message": "素材已移出文案队列，历史记录保留", "updated_at": now})
                document["selections"][selection_id] = current
            self._write(document)
            return dict(updated)


__all__ = [
    "MAX_SOURCE_TEXT_CHARS",
    "MIN_SOURCE_TEXT_CHARS",
    "SCHEMA_VERSION",
    "SELECTION_STATES",
    "TopicGovernanceStore",
    "normalize_source_text",
    "utc_now",
]
