"""历史：选题中心到采集/文案节点之间的最小数据契约。

当前选题中心暂不启用队列和自动消费链路；本模块保留标准化选题包、
队列读写函数，供后续重新接入采集节点时恢复使用。它不启动外部采集、
不调用模型，也不直接写入文案或视频生产状态。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


SCHEMA_VERSION = 1
ALLOWED_DECISIONS = frozenset({"accepted", "ignored", "later"})
ALLOWED_HTTP_SCHEMES = frozenset({"http", "https"})
_QUERY_ALIASES = (
    ("生成式人工智能", "ai"),
    ("人工智能", "ai"),
    ("人工智慧", "ai"),
    ("大语言模型", "llm"),
    ("大型语言模型", "llm"),
)
_QUERY_FILLER_RE = re.compile(r"(?:有关|相关)(?:的)?(?:信息|内容|新闻|资讯|消息)?|(?:信息|内容|新闻|资讯|消息)$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_topic_query(value: str) -> str:
    normalized = str(value or "").strip().lower()
    for source, target in _QUERY_ALIASES:
        normalized = normalized.replace(source, target)
    return _QUERY_FILLER_RE.sub(" ", normalized)


def topic_query_terms(value: str) -> set[str]:
    return set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]+", normalize_topic_query(value)))


def detect_source_platform(source_url: str) -> str:
    """识别采集路由，不代表该平台已经有可用采集器。"""
    host = urlparse(str(source_url or "").strip()).netloc.lower().removeprefix("www.")
    if host == "zhihu.com" or host.endswith(".zhihu.com"):
        return "zhihu"
    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        return "bilibili"
    if host in {"douyin.com", "v.douyin.com", "iesdouyin.com"} or host.endswith(".douyin.com"):
        return "douyin"
    return "article"


def collector_route(platform: str) -> str:
    return {
        "zhihu": "zhihu_question",
        "bilibili": "bilibili_video",
        "douyin": "douyin_video",
        "article": "article_html",
    }.get(str(platform or "article"), "article_html")


def _normalize_rank(value: Any) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("rank 必须是整数") from exc


def _validate_source_url(source_url: str) -> None:
    parsed = urlparse(str(source_url or "").strip())
    if parsed.scheme.lower() not in ALLOWED_HTTP_SCHEMES or not parsed.netloc:
        raise ValueError("source_url 必须是带域名的 HTTP(S) 地址")


def build_topic_selection_package(
    *,
    candidate_id: str,
    decision: str,
    source_url: str,
    title: str,
    source_name: str = "",
    rank: int | None = None,
    intent: str = "",
    tags: list[str] | None = None,
    source_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_id = str(candidate_id or "").strip()
    decision = str(decision or "").strip().lower()
    source_url = str(source_url or "").strip()
    title = str(title or "").strip()
    if not candidate_id or decision not in ALLOWED_DECISIONS:
        raise ValueError("缺少 candidate_id 或决策状态无效")
    if source_url:
        _validate_source_url(source_url)
    if decision == "accepted" and (not source_url or not title):
        raise ValueError("采用选题必须包含 source_url 和 title，才能交给采集节点")
    platform = detect_source_platform(source_url)
    clean_source_metadata = dict(source_metadata) if isinstance(source_metadata, Mapping) else {}
    route_override = str(clean_source_metadata.get("collector_route") or "").strip()
    collection_route = route_override if route_override in {"douyin_hot_search", "douyin_video"} else collector_route(platform)
    clean_tags: list[str] = []
    raw_tags = tags if isinstance(tags, (list, tuple)) else ([tags] if tags else [])
    for tag in raw_tags:
        value = str(tag or "").strip()
        if value and len(value) <= 80:
            clean_tags.append(value)
    timestamp = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "selection_id": f"topic-{candidate_id}",
        "candidate_id": candidate_id,
        "decision": decision,
        "source_url": source_url,
        "title": title,
        "source_name": str(source_name or "").strip(),
        "rank": _normalize_rank(rank),
        "intent": str(intent or "").strip(),
        "tags": list(dict.fromkeys(clean_tags)),
        "platform": platform,
        "source_metadata": clean_source_metadata,
        "collection": {
            "state": "queued" if decision == "accepted" else "not_requested",
            "route": collection_route,
            "result_id": "",
            "message": "等待采集节点读取" if decision == "accepted" else "未进入采集队列",
        },
        "writing": {
            "state": "blocked_collection_required" if decision == "accepted" else "not_requested",
            "input_type": "collected_source_content",
            "message": "采集正文/字幕完成后才能进入文案创作" if decision == "accepted" else "未进入文案消费",
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def load_queue(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, Mapping):
        value = {}
    items = value.get("items") if isinstance(value.get("items"), list) else []
    try:
        schema_version = int(value.get("schema_version") or SCHEMA_VERSION)
    except (TypeError, ValueError):
        schema_version = SCHEMA_VERSION
    return {"schema_version": schema_version, "updated_at": value.get("updated_at", ""), "items": items}


def upsert_queue_item(path: Path, item: dict[str, Any]) -> dict[str, Any]:
    queue = load_queue(path)
    items = [entry for entry in queue["items"] if isinstance(entry, Mapping) and entry.get("selection_id") != item.get("selection_id")]
    items.append(item)
    queue.update({"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "items": items})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return item


__all__ = [
    "SCHEMA_VERSION",
    "ALLOWED_DECISIONS",
    "build_topic_selection_package",
    "collector_route",
    "detect_source_platform",
    "load_queue",
    "normalize_topic_query",
    "topic_query_terms",
    "upsert_queue_item",
]
