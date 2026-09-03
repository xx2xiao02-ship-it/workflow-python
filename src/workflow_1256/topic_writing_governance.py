"""选题与文案联合治理的数据层。

该模块只负责本地、可追踪的治理对象和状态，不调用模型，也不恢复历史
选题队列。正文采集仍由 :mod:`topic_collection` 完成，文案结果仍由现有
``StylePackageStore`` 保存；这里把两者之间的对象关系固定下来。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


SCHEMA_VERSION = "topic-writing-governance-v2"
MIN_SOURCE_TEXT_CHARS = 80
MAX_SOURCE_TEXT_CHARS = 200_000

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
    }
)
SOURCE_STATES = frozenset(
    {
        "CONTENT_READY",
        "EXTRACTION_FAILED",
        "EXTRACTION_BLOCKED",
        "CONTENT_MISSING",
        "EXTRACTION_DELEGATED",
    }
)


class TopicWritingGovernanceError(ValueError):
    """治理对象不符合输入或状态契约。"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean_text(value: Any, *, field: str = "文本", max_chars: int = MAX_SOURCE_TEXT_CHARS) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > max_chars:
        raise TopicWritingGovernanceError(f"{field}不能超过 {max_chars} 个字符")
    return text


def normalize_source_text(value: Any, *, field: str = "正文") -> str:
    text = _clean_text(value, field=field)
    if len(text) < MIN_SOURCE_TEXT_CHARS:
        raise TopicWritingGovernanceError(
            f"{field}至少需要 {MIN_SOURCE_TEXT_CHARS} 个字符，当前为 {len(text)} 个字符"
        )
    return text


def _http_url(value: Any, *, required: bool = False) -> str:
    url = str(value or "").strip()
    if not url:
        if required:
            raise TopicWritingGovernanceError("source_url 必须是带域名的 HTTP(S) 地址")
        return ""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise TopicWritingGovernanceError("source_url 必须是带域名的 HTTP(S) 地址")
    return url


def _tags(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else ([value] if value else [])
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and len(text) <= 80 and text not in result:
            result.append(text)
    return result


def _content_items(value: Any, *, max_items: int = 50) -> list[dict[str, Any]]:
    """保存TikHub归一化后的内容快照，不落盘原始响应或凭据。"""
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:max_items]:
        if not isinstance(item, Mapping):
            continue
        content_id = str(item.get("content_id") or "").strip()
        title = _clean_text(item.get("title"), field="content.title", max_chars=800)
        url = _http_url(item.get("url") or item.get("source_url")) if item.get("url") or item.get("source_url") else ""
        if not content_id and not url:
            continue
        snapshot = {
            "content_id": content_id or f"content-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}",
            "provider": _clean_text(item.get("provider"), field="content.provider", max_chars=80),
            "platform": _clean_text(item.get("platform"), field="content.platform", max_chars=80),
            "content_type": _clean_text(item.get("content_type"), field="content.content_type", max_chars=40),
            "content_preview": _clean_text(item.get("content_preview"), field="content.content_preview", max_chars=360),
            "content_preview_source": _clean_text(item.get("content_preview_source"), field="content.content_preview_source", max_chars=40),
            "title": title,
            "url": url,
            "author": _clean_text(item.get("author"), field="content.author", max_chars=200),
            "published_at": _clean_text(item.get("published_at"), field="content.published_at", max_chars=80),
            "metrics": dict(item.get("metrics")) if isinstance(item.get("metrics"), Mapping) else {},
            "native_score": item.get("native_score"),
            "relevance_score": item.get("relevance_score"),
            "matched_terms": _tags(item.get("matched_terms")),
            "source_metadata": dict(item.get("source_metadata")) if isinstance(item.get("source_metadata"), Mapping) else {},
        }
        result.append(snapshot)
    return result


def _append_state_history(record: dict[str, Any], state: str, timestamp: str | None = None) -> None:
    value = str(state or "").strip().upper()
    if not value:
        return
    history = record.get("state_history") if isinstance(record.get("state_history"), list) else []
    clean_history = [
        dict(item)
        for item in history
        if isinstance(item, Mapping) and str(item.get("state") or "").strip()
    ]
    if not clean_history or str(clean_history[-1].get("state") or "").strip().upper() != value:
        clean_history.append({"state": value, "at": timestamp or utc_now()})
    record["state_history"] = clean_history


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


class TopicWritingGovernanceStore:
    """持久化联合治理对象，使用单文件和原子替换保证刷新后可追踪。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now(),
            "selections": {},
            "sources": {},
            "copy_drafts": {},
            "approved_copies": {},
        }

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty()
        if not isinstance(value, Mapping):
            return self._empty()
        selections = value.get("selections") if isinstance(value.get("selections"), Mapping) else {}
        sources = value.get("sources") if isinstance(value.get("sources"), Mapping) else {}
        copy_drafts = value.get("copy_drafts") if isinstance(value.get("copy_drafts"), Mapping) else {}
        approved_copies = value.get("approved_copies") if isinstance(value.get("approved_copies"), Mapping) else {}
        normalized_selections: dict[str, dict[str, Any]] = {}
        for key, item in selections.items():
            if not isinstance(item, Mapping):
                continue
            record = dict(item)
            # Backfill a readable starting point for records created before
            # state_history was introduced; subsequent updates append the
            # real transitions without rewriting historical payloads.
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
            "copy_drafts": {str(key): dict(item) for key, item in copy_drafts.items() if isinstance(item, Mapping)},
            "approved_copies": {str(key): dict(item) for key, item in approved_copies.items() if isinstance(item, Mapping)},
        }

    def _write(self, value: Mapping[str, Any]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now(),
            "selections": _json_safe(value.get("selections") if isinstance(value.get("selections"), Mapping) else {}),
            "sources": _json_safe(value.get("sources") if isinstance(value.get("sources"), Mapping) else {}),
            "copy_drafts": _json_safe(value.get("copy_drafts") if isinstance(value.get("copy_drafts"), Mapping) else {}),
            "approved_copies": _json_safe(value.get("approved_copies") if isinstance(value.get("approved_copies"), Mapping) else {}),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _selection_id(value: Any) -> str:
        selection_id = str(value or "").strip()
        if not selection_id or Path(selection_id).name != selection_id or ".." in selection_id:
            raise TopicWritingGovernanceError("selection_id 不合法")
        return selection_id

    @staticmethod
    def _source_id(value: Any) -> str:
        source_id = str(value or "").strip()
        if not source_id or Path(source_id).name != source_id or ".." in source_id:
            raise TopicWritingGovernanceError("source_content_id 不合法")
        return source_id

    def save_selection(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """保存已采用选题；默认只产生 ACCEPTED，不自动采集或生成文案。"""
        candidate_id = str(payload.get("candidate_id") or "").strip()
        title = _clean_text(payload.get("title"), field="title", max_chars=500)
        source_url = _http_url(payload.get("source_url"), required=True)
        if not candidate_id or not title:
            raise TopicWritingGovernanceError("采用选题需要 candidate_id 和 title")
        content_items = _content_items(payload.get("content_items"))
        primary_content_id = str(payload.get("primary_content_id") or "").strip()
        reference_content_ids = [
            str(item).strip()
            for item in (payload.get("reference_content_ids") if isinstance(payload.get("reference_content_ids"), (list, tuple)) else [])
            if str(item).strip()
        ]
        if content_items:
            known_ids = {str(item.get("content_id") or "") for item in content_items}
            if not primary_content_id:
                raise TopicWritingGovernanceError("建立选题包必须指定 primary_content_id")
            if primary_content_id not in known_ids:
                raise TopicWritingGovernanceError("primary_content_id 不在已查询内容中")
            reference_content_ids = [item for item in dict.fromkeys(reference_content_ids) if item in known_ids and item != primary_content_id]
        selection_id = str(payload.get("selection_id") or f"topic-{candidate_id}").strip()
        self._selection_id(selection_id)
        now = utc_now()
        with self._lock:
            document = self._read()
            previous = document["selections"].get(selection_id)
            current_state = str((previous or {}).get("state") or "ACCEPTED").strip().upper()
            if current_state not in SELECTION_STATES:
                current_state = "ACCEPTED"
            record = {
                "selection_id": selection_id,
                "candidate_id": candidate_id,
                "decision": "accepted",
                "title": title,
                "source_url": source_url,
                "source_name": _clean_text(payload.get("source_name"), field="source_name", max_chars=200),
                "rank": payload.get("rank"),
                "intent": _clean_text(payload.get("intent"), field="intent", max_chars=1000),
                "tags": _tags(payload.get("tags")),
                "platform": _clean_text(payload.get("platform"), field="platform", max_chars=80) or "article",
                "source_metadata": dict(payload.get("source_metadata")) if isinstance(payload.get("source_metadata"), Mapping) else {},
                "primary_content_id": primary_content_id,
                "reference_content_ids": list(dict.fromkeys(reference_content_ids)),
                "content_items": content_items,
                "content_bundle_status": "selected" if content_items else "not_requested",
                "state": current_state,
                "state_history": list((previous or {}).get("state_history") or []),
                "source_content_id": str((previous or {}).get("source_content_id") or ""),
                "copy_rewrite_id": str((previous or {}).get("copy_rewrite_id") or ""),
                "message": str((previous or {}).get("message") or "选题已采用，等待正文提取"),
                "created_at": str((previous or {}).get("created_at") or now),
                "updated_at": now,
            }
            _append_state_history(record, current_state, now)
            document["selections"][selection_id] = record
            self._write(document)
            return dict(record)

    def get_selection(self, selection_id: str) -> dict[str, Any] | None:
        key = self._selection_id(selection_id)
        with self._lock:
            value = self._read()["selections"].get(key)
            return dict(value) if isinstance(value, Mapping) else None

    def list_selections(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._read()["selections"].values())
        values = [dict(item) for item in values if isinstance(item, Mapping)]
        values.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return values[: max(1, min(int(limit), 1000))]

    def update_selection(
        self,
        selection_id: str,
        *,
        state: str | None = None,
        source_content_id: str | None = None,
        copy_rewrite_id: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        key = self._selection_id(selection_id)
        with self._lock:
            document = self._read()
            record = document["selections"].get(key)
            if not isinstance(record, Mapping):
                raise TopicWritingGovernanceError("未找到对应的 topic_selection")
            record = dict(record)
            if state is not None:
                value = str(state).strip().upper()
                if value not in SELECTION_STATES:
                    raise TopicWritingGovernanceError(f"不支持的选题状态：{value}")
                record["state"] = value
                _append_state_history(record, value)
            if source_content_id is not None:
                record["source_content_id"] = str(source_content_id or "").strip()
            if copy_rewrite_id is not None:
                record["copy_rewrite_id"] = str(copy_rewrite_id or "").strip()
            if message is not None:
                record["message"] = str(message or "").strip()
            record["updated_at"] = utc_now()
            document["selections"][key] = record
            self._write(document)
            return dict(record)

    def save_extraction_result(self, selection_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
        selection = self.get_selection(selection_id)
        if selection is None:
            raise TopicWritingGovernanceError("未找到对应的 topic_selection")
        collector_status = str(result.get("status") or "failed").strip().lower()
        content = _clean_text(result.get("content"), field="正文")
        if collector_status == "ready" and len(content) < MIN_SOURCE_TEXT_CHARS:
            collector_status = "content_missing"
        status_by_collector = {
            "ready": "CONTENT_READY",
            "content_missing": "CONTENT_MISSING",
            "blocked": "EXTRACTION_BLOCKED",
            "failed": "EXTRACTION_FAILED",
            "invalid_input": "EXTRACTION_FAILED",
            "delegated": "EXTRACTION_DELEGATED",
        }
        source_status = status_by_collector.get(collector_status, "EXTRACTION_FAILED")
        source_id = str(result.get("source_content_id") or f"source-{uuid.uuid4().hex[:16]}").strip()
        self._source_id(source_id)
        now = utc_now()
        source = {
            "source_content_id": source_id,
            "selection_id": selection["selection_id"],
            "input_type": "news_article",
            "title": _clean_text(result.get("title") or selection.get("title"), field="title", max_chars=500),
            "source_url": _http_url(result.get("final_url") or result.get("source_url") or selection.get("source_url")),
            "source_name": str(selection.get("source_name") or "").strip(),
            "platform": str(result.get("platform") or selection.get("platform") or "article").strip(),
            "source_metadata": dict(result.get("source_metadata")) if isinstance(result.get("source_metadata"), Mapping) else dict(selection.get("source_metadata") or {}),
            "content": content if source_status == "CONTENT_READY" else "",
            "content_type": str(result.get("content_type") or "html_text").strip(),
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest() if content else "",
            "status": source_status,
            "collector_status": collector_status,
            "transcript_status": "collected_html" if source_status == "CONTENT_READY" else "",
            "http_status": result.get("http_status"),
            "final_url": str(result.get("final_url") or result.get("source_url") or selection.get("source_url") or "").strip(),
            "error_code": str(result.get("error_code") or "").strip(),
            "message": str(result.get("message") or "").strip(),
            "queue_state": "ACTIVE",
            "created_at": now,
            "updated_at": now,
        }
        selection_state = source_status
        if source_status == "CONTENT_READY":
            selection_state = "CONTENT_READY"
        with self._lock:
            document = self._read()
            document["sources"][source_id] = source
            current = dict(document["selections"].get(selection["selection_id"]) or selection)
            current["state"] = selection_state
            _append_state_history(current, selection_state, now)
            current["source_content_id"] = source_id
            current["queue_state"] = "ACTIVE"
            current["message"] = source["message"] or (
                "正文已提取，允许进入文案创作" if source_status == "CONTENT_READY" else "正文提取未达到文案消费条件"
            )
            current["updated_at"] = now
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
        selection = self.get_selection(selection_key) if selection_key else None
        if selection_key and selection is None:
            raise TopicWritingGovernanceError("未找到要绑定的 topic_selection")
        # 纯文本默认没有来源链接；如果用户提供链接，必须是真实 HTTP(S)，
        # 绝不为了填充字段生成 topic:// 或其它伪造地址。
        url = _http_url(source_url)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = utc_now()
        source_id = f"source-manual-{digest[:16]}"
        source = {
            "source_content_id": source_id,
            "selection_id": selection_key,
            "input_type": "manual_text",
            "title": _clean_text(title, field="title", max_chars=500) or "纯文案素材",
            "source_url": url,
            "source_name": _clean_text(source_name, field="source_name", max_chars=200),
            "platform": "manual",
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
                    "message": "纯文案素材已准备，允许进入文案创作",
                    "updated_at": now,
                })
                _append_state_history(current, "CONTENT_READY", now)
                document["selections"][selection_key] = current
            self._write(document)
        return dict(source)

    def save_video_source(
        self,
        case_item: Mapping[str, Any],
        *,
        source_url: Any = "",
        source_name: Any = "",
    ) -> dict[str, Any]:
        """把真实视频字幕/转写保存为可消费的治理正文。

        这个入口只接收采集器已经返回的真实转写，不接受搜索摘要或视频
        标题代替正文。source_content_id 按平台、规范链接和正文哈希稳定
        生成，重试同一视频不会在治理库里制造重复素材。
        """
        if not isinstance(case_item, Mapping):
            raise TopicWritingGovernanceError("视频采集结果不是对象")
        content = normalize_source_text(case_item.get("transcript"), field="视频字幕/转写")
        url = _http_url(source_url or case_item.get("source_url"), required=True)
        platform = _clean_text(case_item.get("platform") or "video", field="platform", max_chars=80)
        title = _clean_text(case_item.get("title"), field="title", max_chars=500) or "视频转写素材"
        name = _clean_text(source_name or case_item.get("source_name") or platform, field="source_name", max_chars=200)
        transcript_status = _clean_text(
            case_item.get("transcript_status") or "local_whisper",
            field="transcript_status",
            max_chars=40,
        )
        metadata = dict(case_item.get("source_metadata")) if isinstance(case_item.get("source_metadata"), Mapping) else {}
        for key in ("video_id", "bvid", "duration_seconds", "published_at", "subtitle_url"):
            value = case_item.get(key)
            if value not in (None, "") and key not in metadata:
                metadata[key] = value
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        video_key = "\n".join((platform, url, digest))
        source_key = hashlib.sha256(video_key.encode("utf-8")).hexdigest()
        source_id = f"source-video-{source_key[:16]}"
        now = utc_now()
        source = {
            "source_content_id": source_id,
            "selection_id": str(case_item.get("selection_id") or "").strip(),
            "input_type": "video_transcript",
            "title": title,
            "source_url": url,
            "source_name": name,
            "platform": platform,
            "source_metadata": metadata,
            "content": content,
            "content_type": "video_transcript",
            "content_hash": digest,
            "status": "CONTENT_READY",
            "collector_status": "ready",
            "transcript_status": transcript_status,
            "http_status": None,
            "final_url": url,
            "error_code": "",
            "message": "视频字幕/转写已准备，可进入文案创作",
            "queue_state": "ACTIVE",
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            document = self._read()
            previous = document["sources"].get(source_id)
            if isinstance(previous, Mapping):
                source["created_at"] = str(previous.get("created_at") or now)
            document["sources"][source_id] = source
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
            raise TopicWritingGovernanceError("未找到 source_content_id 对应的正文素材")
        if source.get("status") != "CONTENT_READY" or len(str(source.get("content") or "")) < MIN_SOURCE_TEXT_CHARS:
            raise TopicWritingGovernanceError("正文素材尚未达到文案消费条件")
        return source

    def list_sources(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._read()["sources"].values())
        values = [dict(item) for item in values if isinstance(item, Mapping)]
        values = [item for item in values if str(item.get("queue_state") or "ACTIVE").strip().upper() != "ARCHIVED"]
        values.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return values[: max(1, min(int(limit), 1000))]

    def archive_source(self, source_content_id: str, *, reason: str = "user_remove_queue") -> dict[str, Any]:
        """移出文案待处理队列，但保留素材、选题和审核历史。"""
        key = self._source_id(source_content_id)
        with self._lock:
            document = self._read()
            source = document["sources"].get(key)
            if not isinstance(source, Mapping):
                raise TopicWritingGovernanceError("未找到 source_content_id 对应的素材")
            source = dict(source)
            copy_state = str(source.get("copy_state") or "").strip().upper()
            protected_states = {"WRITING", "REVIEW_PENDING", "REVIEW_BLOCKED", "APPROVED", "DIRECTOR_READY"}
            if copy_state in protected_states:
                raise TopicWritingGovernanceError("该素材已进入文案或审核链路，不能移出队列")
            now = utc_now()
            source["queue_state"] = "ARCHIVED"
            source["queue_archived_at"] = now
            source["queue_archive_reason"] = str(reason or "user_remove_queue").strip()[:200]
            source["updated_at"] = now
            document["sources"][key] = source
            selection_id = str(source.get("selection_id") or "").strip()
            selection = document["selections"].get(selection_id)
            if isinstance(selection, Mapping) and str(selection.get("source_content_id") or "").strip() == key:
                selection = dict(selection)
                selection["queue_state"] = "ARCHIVED"
                selection["message"] = "素材已移出文案队列，历史记录保留"
                selection["updated_at"] = now
                document["selections"][selection_id] = selection
            self._write(document)
            return dict(source)

    def link_copy(
        self,
        rewrite_id: str,
        *,
        selection_id: str = "",
        source_content_id: str = "",
        state: str = "REVIEW_PENDING",
        message: str = "文案已生成，等待人工审核",
    ) -> dict[str, Any]:
        rewrite_key = str(rewrite_id or "").strip()
        if not rewrite_key:
            raise TopicWritingGovernanceError("rewrite_id 不能为空")
        state = str(state or "").strip().upper()
        if state not in SELECTION_STATES:
            raise TopicWritingGovernanceError(f"不支持的文案治理状态：{state}")
        with self._lock:
            document = self._read()
            matched: list[dict[str, Any]] = []
            matched_selection_id = str(selection_id or "").strip()
            matched_source = False
            for key, item in document["selections"].items():
                if not isinstance(item, Mapping):
                    continue
                if (selection_id and key == selection_id) or (source_content_id and item.get("source_content_id") == source_content_id) or item.get("copy_rewrite_id") == rewrite_key:
                    updated = dict(item)
                    updated["copy_rewrite_id"] = rewrite_key
                    updated["state"] = state
                    _append_state_history(updated, state)
                    updated["message"] = message
                    updated["updated_at"] = utc_now()
                    document["selections"][key] = updated
                    matched.append(updated)
                    matched_selection_id = key
            if source_content_id:
                source = document["sources"].get(source_content_id)
                if isinstance(source, Mapping):
                    matched_source = True
                    source = dict(source)
                    source["copy_rewrite_id"] = rewrite_key
                    source["copy_state"] = state
                    source["updated_at"] = utc_now()
                    document["sources"][source_content_id] = source
            existing_draft = document["copy_drafts"].get(rewrite_key)
            if not matched and not matched_source and not isinstance(existing_draft, Mapping):
                # An old case-video rewrite has no joint-governance object;
                # approval must preserve its existing store contract without
                # creating a stray governance draft.
                return {"copy_rewrite_id": rewrite_key, "state": state, "governance_linked": False}
            draft = dict(document["copy_drafts"].get(rewrite_key) or {})
            draft.update({
                "rewrite_id": rewrite_key,
                "selection_id": matched_selection_id,
                "source_content_id": str(source_content_id or draft.get("source_content_id") or ""),
                "state": state,
                "message": message,
                "updated_at": utc_now(),
            })
            draft.setdefault("created_at", draft["updated_at"])
            document["copy_drafts"][rewrite_key] = draft
            self._write(document)
            if matched:
                linked = dict(matched[0])
            else:
                linked = {"copy_rewrite_id": rewrite_key, "state": state}
            linked["governance_linked"] = bool(matched or matched_source or draft.get("selection_id") or draft.get("source_content_id"))
            return linked

    def save_approved_copy(
        self,
        rewrite_id: str,
        approved_copy: Any,
        *,
        selection_id: str = "",
        source_content_id: str = "",
        style_profile_id: str = "",
    ) -> dict[str, Any]:
        """落盘联合链的 approved_copy 快照，供编导审计读取。"""
        rewrite_key = str(rewrite_id or "").strip()
        text = _clean_text(approved_copy, field="approved_copy")
        if not text:
            raise TopicWritingGovernanceError("approved_copy 不能为空")
        with self._lock:
            document = self._read()
            draft = dict(document["copy_drafts"].get(rewrite_key) or {})
            selection_key = str(selection_id or draft.get("selection_id") or "").strip()
            source_key = str(source_content_id or draft.get("source_content_id") or "").strip()
            record = {
                "rewrite_id": rewrite_key,
                "selection_id": selection_key,
                "source_content_id": source_key,
                "style_profile_id": str(style_profile_id or "").strip(),
                "approved_copy": text,
                "approved_at": utc_now(),
                "state": "DIRECTOR_READY",
            }
            document["approved_copies"][rewrite_key] = record
            if draft:
                draft["state"] = "DIRECTOR_READY"
                draft["updated_at"] = record["approved_at"]
                document["copy_drafts"][rewrite_key] = draft
            self._write(document)
            return dict(record)

    def snapshot(self, selection_id: str) -> dict[str, Any]:
        selection = self.get_selection(selection_id)
        if selection is None:
            raise TopicWritingGovernanceError("未找到对应的 topic_selection")
        source_id = str(selection.get("source_content_id") or "").strip()
        source = self.get_source(source_id) if source_id else None
        with self._lock:
            document = self._read()
            rewrite_id = str(selection.get("copy_rewrite_id") or "").strip()
            copy_draft = dict(document["copy_drafts"].get(rewrite_id) or {}) if rewrite_id else None
            approved_copy = dict(document["approved_copies"].get(rewrite_id) or {}) if rewrite_id else None
        return {
            "selection": selection,
            "source": source,
            "copy_rewrite_id": rewrite_id,
            "copy_draft": copy_draft,
            "approved_copy": approved_copy,
        }


__all__ = [
    "MAX_SOURCE_TEXT_CHARS",
    "MIN_SOURCE_TEXT_CHARS",
    "SCHEMA_VERSION",
    "SELECTION_STATES",
    "SOURCE_STATES",
    "TopicWritingGovernanceError",
    "TopicWritingGovernanceStore",
    "normalize_source_text",
    "utc_now",
]
