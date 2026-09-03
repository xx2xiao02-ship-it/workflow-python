"""Horizon 到现有选题中心的隔离 HTTP 适配器。

该服务只负责调用 Horizon 的官方分阶段流水线并把候选转换为选题中心能消费的
最小字段。它不修改文案、编导或素材任务，也不会在缺少正文时伪造内容。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
HORIZON_ROOT = Path(
    os.environ.get("HORIZON_ROOT", str(ROOT / "tmp" / "Horizon"))
).expanduser().resolve()
HORIZON_CONFIG = Path(
    os.environ.get("HORIZON_CONFIG", str(HORIZON_ROOT / "data" / "config.json"))
).expanduser().resolve()
RUNS_ROOT = Path(
    os.environ.get("HORIZON_RUNS_ROOT", str(HORIZON_ROOT / "data" / "topic-runs"))
).expanduser().resolve()
HOST = os.environ.get("HORIZON_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("HORIZON_BRIDGE_PORT", "8791"))

if str(HORIZON_ROOT) not in sys.path:
    sys.path.insert(0, str(HORIZON_ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.mcp.service import HorizonMcpError, HorizonPipelineService  # type: ignore  # noqa: E402

try:
    from tools.douyin_hot_adapter import (  # type: ignore  # noqa: E402
        BOARD_LABELS,
        DouyinHotSourceError,
        fetch_douyin_board,
    )
except ModuleNotFoundError:  # direct ``python tools\\horizon_topic_bridge.py`` fallback
    from douyin_hot_adapter import BOARD_LABELS, DouyinHotSourceError, fetch_douyin_board  # type: ignore  # noqa: E402

try:
    from tools.mainland_hot_adapter import (  # type: ignore  # noqa: E402
        MAINLAND_HOT_SOURCE_LABELS,
        fetch_mainland_hot_sources,
    )
except ModuleNotFoundError:  # direct ``python tools\\horizon_topic_bridge.py`` fallback
    from mainland_hot_adapter import MAINLAND_HOT_SOURCE_LABELS, fetch_mainland_hot_sources  # type: ignore


SERVICE = HorizonPipelineService(runs_root=RUNS_ROOT)
PIPELINE_LOCK = threading.Lock()
QUERY_FILLERS = re.compile(
    r"(?:有关|相关)(?:的)?(?:信息|内容|新闻|资讯|消息)?|(?:信息|内容|新闻|资讯|消息)$"
)
QUERY_ALIASES = {
    "ai": ("ai", "人工智能", "智能", "模型", "deepseek", "chatgpt", "机器人", "脑机", "算法"),
    "llm": ("llm", "大语言模型", "大型语言模型", "语言模型", "deepseek", "chatgpt"),
}
QUERY_FETCH_LOCK = threading.Lock()
QUERY_RUN_CACHE: dict[str, tuple[float, str]] = {}
DOUYIN_CACHE_TTL = max(30, int(os.environ.get("DOUYIN_HOT_CACHE_TTL", "300")))
DOUYIN_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
DOUYIN_CACHE_LOCK = threading.Lock()
MAINLAND_HOT_CACHE_TTL = max(30, int(os.environ.get("MAINLAND_HOT_CACHE_TTL", "180")))
MAINLAND_HOT_CACHE: tuple[float, dict[str, Any]] | None = None
MAINLAND_HOT_CACHE_LOCK = threading.Lock()

# 选题中心对外暴露的信源键。UI 使用稳定键，显示名称只负责展示，避免把
# “36氪”“RSSHub 知乎”等具体订阅名称直接耦合到前端请求参数。
TOPIC_SOURCE_LABELS = {
    "douyin_hot": "抖音分类热榜",
    "rss": "Horizon RSS",
    "rss_36kr": "36氪",
    "rss_zhihu": "RSSHub 知乎",
    "google_news": "Google News",
    "web_search": "网络新闻检索",
    **MAINLAND_HOT_SOURCE_LABELS,
}
TOPIC_SOURCE_KEYS = frozenset(TOPIC_SOURCE_LABELS)
TOPIC_SOURCE_ALIASES = {
    "douyin": "douyin_hot",
    "抖音分类热榜": "douyin_hot",
    "抖音热榜": "douyin_hot",
    "horizon rss": "rss",
    "36氪": "rss_36kr",
    "google news 人工智能": "google_news",
    "rsshub 知乎": "rss_zhihu",
    "知乎": "rss_zhihu",
    "google news": "google_news",
    "网络检索": "web_search",
    "bing": "web_search",
    "微博热搜": "weibo_hot",
    "微博": "weibo_hot",
    "今日头条热榜": "toutiao_hot",
    "头条热榜": "toutiao_hot",
    "今日头条": "toutiao_hot",
    "百度热搜": "baidu_hot",
    "百度": "baidu_hot",
    "b站热门": "bilibili_hot",
    "b站": "bilibili_hot",
    "哔哩哔哩": "bilibili_hot",
}
GENERIC_TOPIC_TAGS = frozenset(
    {
        "知识解释",
        "商业财经",
        "普通人行动",
        "科技趋势",
        "社会观察",
        "女性成长",
        "故事评论",
        "热点快评",
        "综合热点",
    }
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def _normalize_query(value: str) -> str:
    text = str(value or "").strip().lower()
    for source, target in (
        ("生成式人工智能", "ai"),
        ("人工智能", "ai"),
        ("人工智慧", "ai"),
        ("大语言模型", "llm"),
        ("大型语言模型", "llm"),
    ):
        text = text.replace(source, target)
    return QUERY_FILLERS.sub(" ", text)


def _terms(value: str) -> set[str]:
    terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]+", _normalize_query(value)))
    expanded = set(terms)
    for key, aliases in QUERY_ALIASES.items():
        if key in terms:
            expanded.update(aliases)
    return expanded


def _filter_terms(intent: str, tags: list[str]) -> set[str]:
    """Build hard-filter terms without letting a broad UI category erase intent."""

    intent_text = str(intent or "").strip()
    terms = _terms(intent_text)
    # 有明确自然语言时，内置分类是辅助维度，不应把“综合热点”变成全量 OR。
    # 没有自然语言时，分类标签仍然可以独立筛选。
    if intent_text:
        tags = [tag for tag in tags if str(tag or "").strip() not in GENERIC_TOPIC_TAGS]
    for tag in tags:
        terms.update(_terms(tag))
    return terms


def _meaningful_terms(terms: set[str]) -> set[str]:
    """Do not let a date/number alone match an unrelated news item."""

    textual = {term for term in terms if not str(term).isdigit()}
    return textual or terms


def _candidate_id(item: Mapping[str, Any]) -> str:
    identity = str(item.get("id") or item.get("url") or item.get("title") or "")
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]


def _source_name(item: Mapping[str, Any]) -> str:
    metadata = item.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("source_name", "feed_name", "name", "category"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
    return str(item.get("source_type") or "Horizon")


def _normalize_source_selection(sources: list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize UI source keys while keeping the caller's order."""

    normalized: list[str] = []
    for raw in sources or []:
        value = str(raw or "").strip()
        if not value:
            continue
        key = TOPIC_SOURCE_ALIASES.get(value.casefold(), value.casefold())
        if key in TOPIC_SOURCE_KEYS and key not in normalized:
            normalized.append(key)
    return normalized


def _candidate_source_keys(candidate: Mapping[str, Any]) -> set[str]:
    """Return all UI source keys that can legitimately claim a candidate."""

    source_type = str(candidate.get("source_type") or "").strip().casefold()
    source_name = str(
        candidate.get("source_name")
        or candidate.get("source")
        or ""
    ).strip()
    metadata = candidate.get("source_metadata")
    if isinstance(metadata, Mapping):
        source_name = " ".join(
            part
            for part in (
                source_name,
                str(metadata.get("feed_name") or ""),
                str(metadata.get("source_name") or ""),
                str(metadata.get("category") or ""),
            )
            if part
        )
    haystack = f"{source_name} {candidate.get('url') or ''}".casefold()
    keys: set[str] = set()
    if source_type == "douyin_hot":
        keys.add("douyin_hot")
    if source_type == "search":
        keys.add("web_search")
    if source_type == "rss":
        keys.add("rss")
    if source_type == "google_news":
        keys.add("google_news")
    if source_type in MAINLAND_HOT_SOURCE_LABELS:
        keys.add(source_type)
    if "36kr" in haystack or "36氪" in haystack:
        keys.add("rss_36kr")
    if "知乎" in haystack or "rsshub" in haystack:
        keys.add("rss_zhihu")
    if "google news" in haystack or "news.google.com" in haystack:
        keys.add("google_news")
    return keys


def _candidate_matches_sources(candidate: Mapping[str, Any], selected_sources: list[str]) -> bool:
    """Apply an explicit source selection; empty selection preserves old API behavior."""

    if not selected_sources:
        return True
    return bool(_candidate_source_keys(candidate).intersection(selected_sources))


def _analysis(item: Mapping[str, Any]) -> Mapping[str, Any]:
    processing = item.get("processing")
    if isinstance(processing, Mapping):
        analysis = processing.get("analysis")
        if isinstance(analysis, Mapping):
            return analysis
    return {}


def _to_candidate(item: Mapping[str, Any], intent: str, tags: list[str], run_id: str, stage: str) -> dict[str, Any]:
    analysis = _analysis(item)
    title = html.unescape(str(item.get("title") or "").strip())
    url = str(item.get("url") or "").strip()
    description = str(item.get("content") or "").strip()
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    category = str(metadata.get("category") or "") if isinstance(metadata, Mapping) else ""
    source = _source_name(item)
    terms = _terms(intent)
    for tag in tags:
        terms.update(_terms(tag))
    haystack = _normalize_query(" ".join((title, description[:800], source, category)))
    matched = sorted(term for term in terms if term and term in haystack)
    raw_score = analysis.get("score")
    try:
        score = round(float(raw_score) * 10) if raw_score is not None else 50
    except (TypeError, ValueError):
        score = 50
    if matched:
        score += min(25, 8 + len(matched) * 4)
    score = max(0, min(100, int(score)))
    rank = int(metadata.get("rank") or 0) if isinstance(metadata, Mapping) else 0
    reasons = []
    if raw_score is not None:
        reasons.append(f"Horizon AI 评分 {raw_score}/10")
    else:
        reasons.append("等待 Horizon AI 评分")
    if matched:
        reasons.append("匹配：" + "、".join(matched[:5]))
    if category:
        reasons.append("分类：" + category)
    return {
        "candidate_id": _candidate_id(item),
        "title": title,
        "source_type": str(item.get("source_type") or ""),
        "source": source,
        "source_name": source,
        "rank": rank,
        "url": url,
        "score": score,
        "recommendation": "推荐进入选题池" if score >= 70 else "建议人工复核",
        "reasons": reasons,
        "intent": intent,
        "tags": tags,
        "status": "new",
        "run_id": run_id,
        "stage": stage,
        "content_preview": description[:300],
        "source_metadata": dict(metadata),
    }


def _latest_stage() -> tuple[str, str, list[dict[str, Any]]]:
    runs = SERVICE.list_runs(limit=1).get("items") or []
    if not runs:
        return "", "", []
    run_id = str(runs[0].get("run_id") or "")
    stages = runs[0].get("stages") if isinstance(runs[0], Mapping) else {}
    for stage in ("filtered", "scored", "raw"):
        if isinstance(stages, Mapping) and stages.get(stage):
            payload = SERVICE.get_run_stage(run_id, stage, max_items=500)
            return run_id, stage, list(payload.get("items") or [])
    return run_id, "", []


def _query_text(intent: str, tags: list[str]) -> str:
    intent_text = str(intent or "").strip()
    query_tags = [
        str(tag).strip()
        for tag in tags
        if str(tag).strip() and (not intent_text or str(tag).strip() not in GENERIC_TOPIC_TAGS)
    ]
    text = " ".join([intent_text, *query_tags]).strip()
    text = QUERY_FILLERS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # 中文“人工智能”在公开搜索里经常被拆成“人工”；补充 AI 别名，
    # 让网络补充检索至少能命中行业文章，而不是只返回词典解释。
    if "ai" in _terms(text) and not re.search(r"\bai\b", text, flags=re.IGNORECASE):
        text = f"{text} AI"
    if "llm" in _terms(text) and not re.search(r"\bllm\b", text, flags=re.IGNORECASE):
        text = f"{text} LLM"
    return text[:120]


async def _fetch_bing_query(query: str) -> list[dict[str, Any]]:
    """Use Bing's public Chinese news search as a query-time source fallback.

    This is intentionally raw-only: the user sees title, snippet and source URL;
    Horizon AI scoring remains a separate, explicitly controlled stage.
    """
    url = "https://cn.bing.com/search?q=" + quote_plus(query) + "&setlang=zh-CN"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HorizonTopicBridge/1.0"}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    seen: set[str] = set()
    for result in soup.select("li.b_algo"):
        anchor = result.select_one("h2 a")
        if anchor is None:
            continue
        title = anchor.get_text(" ", strip=True)
        href = str(anchor.get("href") or "").strip()
        if not title or not href or not href.startswith(("http://", "https://")) or href in seen:
            continue
        if "bing.com" in (urlparse(href).hostname or "").lower():
            continue
        snippet_node = result.select_one(".b_caption p") or result.select_one("p")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        identity = href or title
        seen.add(href)
        items.append(
            {
                "id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16],
                "source_type": "search",
                "title": html.unescape(title),
                "url": href,
                "content": html.unescape(snippet),
                "author": "Bing 新闻检索",
                "published_at": now,
                "profile": "tech-news",
                "metadata": {
                    "feed_name": "Bing 新闻检索",
                    "category": "用户关键词",
                    "tags": [query],
                },
            }
        )
        if len(items) >= 12:
            break
    return items


def _query_fetch_run(query: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Create a Horizon raw run for a specific query when the latest run misses it."""
    key = query.casefold().strip()
    with QUERY_FETCH_LOCK:
        cached = QUERY_RUN_CACHE.get(key)
        if cached and (datetime.now(timezone.utc).timestamp() - cached[0]) < 300:
            run_id = cached[1]
            try:
                return run_id, "raw", SERVICE.get_run_stage(run_id, "raw", max_items=500).get("items") or []
            except Exception:
                QUERY_RUN_CACHE.pop(key, None)
        try:
            items = _run_async(_fetch_bing_query(query))
        except Exception:
            items = []
        if not items:
            return "", "", []
        run_id = SERVICE.run_store.create_run()
        SERVICE.run_store.save_items(run_id, "raw", items)
        SERVICE.run_store.update_meta(
            run_id,
            {
                "horizon_path": str(HORIZON_ROOT),
                "config_path": str(HORIZON_CONFIG),
                "hours": 24,
                "source_selection": ["query_search"],
                "query": query,
                "raw_count_before_merge": len(items),
                "raw_count": len(items),
                "fetch_status": "success",
                "fetch_report": {"source": "Bing 新闻检索", "query": query, "count": len(items)},
            },
        )
        QUERY_RUN_CACHE[key] = (datetime.now(timezone.utc).timestamp(), run_id)
        return run_id, "raw", items


def _run_async(coro: Any) -> Any:
    with PIPELINE_LOCK:
        return asyncio.run(coro)


def _douyin_board_snapshot(board: str = "hot_search", *, force: bool = False, limit: int = 50) -> dict[str, Any]:
    """读取并短暂缓存抖音榜单，避免页面每次交互都重复请求外部接口。"""
    board = str(board or "hot_search").strip().lower()
    key = f"{board}:{max(1, min(int(limit), 100))}"
    now = datetime.now(timezone.utc).timestamp()
    with DOUYIN_CACHE_LOCK:
        cached = DOUYIN_CACHE.get(key)
        if not force and cached and now - cached[0] < DOUYIN_CACHE_TTL:
            return dict(cached[1])
    try:
        result = _run_async(fetch_douyin_board(board, limit=limit))
    except (DouyinHotSourceError, OSError, httpx.HTTPError, ValueError) as exc:
        return {
            "status": "source_unavailable",
            "source": "抖音热点",
            "board": board,
            "board_label": BOARD_LABELS.get(board, board),
            "items": [],
            "message": f"抖音榜单暂不可用：{type(exc).__name__}：{str(exc)[:180]}",
        }
    if not isinstance(result, Mapping):
        return {"status": "source_unavailable", "items": [], "message": "抖音榜单返回格式无效"}
    normalized = dict(result)
    with DOUYIN_CACHE_LOCK:
        DOUYIN_CACHE[key] = (now, normalized)
    return normalized


def _to_douyin_candidate(item: Mapping[str, Any], intent: str, tags: list[str]) -> dict[str, Any]:
    candidate = _to_candidate(item, intent, tags, "douyin-live", "live")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    source_score = int(metadata.get("source_score") or candidate.get("score") or 50)
    hot_value = int(metadata.get("hot_value") or 0)
    rank = int(metadata.get("rank") or candidate.get("rank") or 0)
    category = str(metadata.get("category") or "综合热点")
    board_type = str(metadata.get("board_type") or "hot_search")
    candidate.update(
        {
            "source_type": "douyin_hot",
            "score": max(0, min(100, source_score)),
            "rank": rank,
            "hot_value": hot_value,
            "category": category,
            "board_type": board_type,
            "source_metadata": {
                **dict(metadata),
                "board_type": board_type,
                "category": category,
                "hot_value": hot_value,
                "rank": rank,
                "captured_at": metadata.get("captured_at", ""),
                "collector_route": metadata.get("collector_route", "douyin_hot_search"),
            },
            "recommendation": "抖音热点，建议结合信源核实后进入选题池" if source_score >= 70 else "抖音热点，建议人工复核",
            "reasons": [
                f"{BOARD_LABELS.get(board_type, '抖音热点')}排名 {rank or '—'}",
                f"榜单热度 {hot_value:,}" if hot_value else "抖音榜单未返回热度值",
                f"分类：{category}",
            ],
        }
    )
    return candidate


def _douyin_hot_candidates(
    intent: str,
    tags: list[str],
    selected_sources: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if selected_sources and "douyin_hot" not in selected_sources:
        return [], {"status": "skipped", "items": [], "source": "抖音热点"}
    snapshot = _douyin_board_snapshot("hot_search", limit=50)
    if snapshot.get("status") not in {"ready", "empty"}:
        return [], snapshot
    wanted = _meaningful_terms(_filter_terms(intent, tags))
    output: list[dict[str, Any]] = []
    for item in snapshot.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        candidate = _to_douyin_candidate(item, intent, tags)
        if wanted:
            haystack = _normalize_query(
                " ".join(
                    (
                        candidate.get("title", ""),
                        candidate.get("source", ""),
                        candidate.get("category", ""),
                        candidate.get("content_preview", ""),
                    )
                )
            )
            if not any(term in haystack for term in wanted):
                continue
        output.append(candidate)
    return output, snapshot


def _mainland_hot_snapshot(selected_sources: list[str]) -> dict[str, Any]:
    """Read the selected mainland hot boards with a short shared cache."""

    global MAINLAND_HOT_CACHE
    requested = [key for key in selected_sources if key in MAINLAND_HOT_SOURCE_LABELS]
    if not requested:
        return {"status": "skipped", "items": [], "sources": []}
    now = datetime.now(timezone.utc).timestamp()
    with MAINLAND_HOT_CACHE_LOCK:
        cached = MAINLAND_HOT_CACHE
        if cached and now - cached[0] < MAINLAND_HOT_CACHE_TTL:
            cached_items = cached[1].get("items") or []
            cached_keys = {
                str(item.get("source_type") or "")
                for item in cached_items
                if isinstance(item, Mapping)
            }
            if set(requested).issubset(cached_keys):
                return {
                    **cached[1],
                    "items": [
                        item
                        for item in cached_items
                        if isinstance(item, Mapping) and str(item.get("source_type") or "") in requested
                    ],
                }
    try:
        snapshot = _run_async(
            fetch_mainland_hot_sources(
                requested,
                limit=50,
            )
        )
    except Exception as exc:
        return {
            "status": "source_unavailable",
            "items": [],
            "sources": [],
            "message": f"中文热点源暂不可用：{type(exc).__name__}: {str(exc)[:180]}",
        }
    normalized = dict(snapshot) if isinstance(snapshot, Mapping) else {"status": "empty", "items": []}
    with MAINLAND_HOT_CACHE_LOCK:
        MAINLAND_HOT_CACHE = (now, normalized)
    normalized["items"] = [
        item
        for item in (normalized.get("items") or [])
        if isinstance(item, Mapping) and str(item.get("source_type") or "") in requested
    ]
    return normalized


def _to_mainland_hot_candidate(item: Mapping[str, Any], intent: str, tags: list[str]) -> dict[str, Any]:
    candidate = _to_candidate(item, intent, tags, "mainland-hot-live", "live")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    source_type = str(item.get("source_type") or "")
    label = MAINLAND_HOT_SOURCE_LABELS.get(source_type, str(item.get("source") or "中文热点"))
    rank = int(metadata.get("rank") or candidate.get("rank") or 0)
    hot_value = int(metadata.get("hot_value") or 0)
    category = str(metadata.get("category") or "综合热点")
    source_score = int(metadata.get("source_score") or candidate.get("score") or 50)
    candidate.update(
        {
            "source_type": source_type,
            "source": label,
            "source_name": label,
            "rank": rank,
            "score": max(0, min(100, source_score)),
            "hot_value": hot_value,
            "category": category,
            "board_type": source_type,
            "source_metadata": {
                **dict(metadata),
                "category": category,
                "rank": rank,
                "hot_value": hot_value,
                "board_type": source_type,
            },
            "recommendation": f"{label}热点，建议结合正文信源核实后进入选题池",
            "reasons": [
                f"{label}排名 {rank or '—'}",
                f"榜单热度 {hot_value:,}" if hot_value else "榜单未返回热度值",
                f"分类：{category}",
            ],
        }
    )
    return candidate


def _mainland_hot_candidates(
    intent: str,
    tags: list[str],
    selected_sources: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = [key for key in (selected_sources or []) if key in MAINLAND_HOT_SOURCE_LABELS]
    if not selected:
        return [], {"status": "skipped", "items": [], "sources": []}
    snapshot = _mainland_hot_snapshot(selected)
    wanted = _meaningful_terms(_filter_terms(intent, tags))
    output: list[dict[str, Any]] = []
    for item in snapshot.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        candidate = _to_mainland_hot_candidate(item, intent, tags)
        if wanted:
            haystack = _normalize_query(
                " ".join(
                    (
                        candidate.get("title", ""),
                        candidate.get("source", ""),
                        candidate.get("category", ""),
                        candidate.get("content_preview", ""),
                    )
                )
            )
            if not any(term in haystack for term in wanted):
                continue
        output.append(candidate)
    return output, snapshot


def _find_candidate(candidate_id: str) -> dict[str, Any] | None:
    run_id, stage, items = _latest_stage()
    for item in items:
        candidate = _to_candidate(item, "", [], run_id, stage)
        if candidate["candidate_id"] == candidate_id:
            return candidate
    # 接受抖音热点时，仍允许从桥的短缓存恢复完整候选字段。
    with DOUYIN_CACHE_LOCK:
        snapshots = [dict(value[1]) for value in DOUYIN_CACHE.values()]
    for snapshot in snapshots:
        for item in snapshot.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            candidate = _to_douyin_candidate(item, "", [])
            if candidate["candidate_id"] == candidate_id:
                return candidate
    with MAINLAND_HOT_CACHE_LOCK:
        snapshots = [dict(MAINLAND_HOT_CACHE[1])] if MAINLAND_HOT_CACHE else []
    for snapshot in snapshots:
        for item in snapshot.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            candidate = _to_mainland_hot_candidate(item, "", [])
            if candidate["candidate_id"] == candidate_id:
                return candidate
    return None


def candidates(
    intent: str = "",
    tags: list[str] | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    intent = str(intent or "").strip()
    tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
    selected_sources = _normalize_source_selection(sources)
    run_id, stage, items = _latest_stage()
    wanted = _meaningful_terms(_filter_terms(intent, tags))
    output: list[dict[str, Any]] = []
    for item in items:
        # `SERVICE.list_runs(limit=1)` may point to the previous query-time
        # Bing run (for example the last query was “许家印”).  That run is
        # not a general news feed and must never be reused for a new intent;
        # query-time search is performed explicitly below with the current
        # text when the user has selected 网络新闻检索.
        if str(item.get("source_type") or "").strip().casefold() == "search":
            continue
        candidate = _to_candidate(item, intent, tags, run_id, stage)
        if not _candidate_matches_sources(candidate, selected_sources):
            continue
        if wanted:
            haystack = _normalize_query(
                " ".join((candidate["title"], candidate["source"], candidate.get("content_preview", "")))
            )
            if not any(term in haystack for term in wanted):
                continue
        output.append(candidate)

    douyin_output, douyin_snapshot = _douyin_hot_candidates(intent, tags, selected_sources)
    output.extend(douyin_output)
    mainland_output, mainland_snapshot = _mainland_hot_candidates(intent, tags, selected_sources)
    output.extend(mainland_output)

    search_query = _query_text(intent, tags)
    query_fallback = False
    # 明确勾选“网络新闻检索”时，Bing 是一个独立信源，必须与抖音、Horizon
    # RSS 并行查询；不能因为抖音已经命中，就跳过 Bing。此前这里的
    # ``not output`` 会让用户勾选全部信源时只看到抖音结果。
    if (
        search_query
        and len(_terms(search_query)) > 0
        and (not selected_sources or "web_search" in selected_sources)
    ):
        query_run_id, query_stage, query_items = _query_fetch_run(search_query)
        if query_items:
            query_output: list[dict[str, Any]] = []
            # Bing 已经按完整关键词完成一次检索；这里只做“至少命中用户
            # 意图词”的宽松校验，避免词典类噪声混入，也不要求标题逐字包含
            # 所有标签（例如“人工智能 + 科技趋势”）。
            fallback_terms = _meaningful_terms(_terms(intent) or _terms(" ".join(tags)))
            for item in query_items:
                candidate = _to_candidate(item, intent, tags, query_run_id, query_stage)
                if not _candidate_matches_sources(candidate, selected_sources):
                    continue
                haystack = _normalize_query(
                    " ".join((candidate["title"], candidate.get("content_preview", "")))
                )
                if fallback_terms and not any(term in haystack for term in fallback_terms):
                    continue
                query_output.append(candidate)
            if query_output:
                # 保留已有信源候选，并把当前查询得到的 Bing 候选追加进来；
                # 后面的标题去重会处理同一新闻在多个信源重复出现的情况。
                output.extend(query_output)
                query_fallback = True
                if not run_id:
                    run_id, stage = query_run_id, query_stage

    # 同一标题可能同时出现在 RSS 与抖音榜单，只保留一张候选卡，避免用户重复采用。
    deduped: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for candidate in output:
        title_key = _normalize_query(str(candidate.get("title") or "")).replace(" ", "")
        if title_key and title_key in seen_titles:
            continue
        if title_key:
            seen_titles.add(title_key)
        deduped.append(candidate)
    output = deduped
    output.sort(key=lambda item: (-int(item["score"]), int(item.get("rank") or 0), item["title"]))

    # 查询补充结果会被缓存为下一次的最新 run；即使本次没有再次发起
    # Bing 请求，也要按真实 source_type 标注，不能把搜索结果误报成 Horizon。
    if any(str(item.get("source_type") or "") == "search" for item in output):
        query_fallback = True
    sources: list[str] = []
    if any(
        str(item.get("source_type") or "") not in {"douyin_hot", "search", *MAINLAND_HOT_SOURCE_LABELS}
        for item in output
    ):
        sources.append("Horizon")
    if douyin_output:
        sources.append("抖音热榜")
    for source_key, source_label in MAINLAND_HOT_SOURCE_LABELS.items():
        if any(str(item.get("source_type") or "") == source_key for item in output):
            sources.append(source_label)
    if query_fallback:
        sources.append("Bing 新闻检索")
    if (
        not output
        and not run_id
        and douyin_snapshot.get("status") not in {"ready", "empty"}
        and mainland_snapshot.get("status") not in {"ready", "empty", "skipped"}
    ):
        return {
            "status": "source_missing",
            "message": "Horizon、抖音和中文热点信源当前都不可用，请检查隔离服务。",
            "candidates": [],
        }
    return {
        "status": "ready",
        "source": " + ".join(sources) if sources else "暂无匹配信源",
        "run_id": run_id or "douyin-live",
        "stage": stage or "live",
        "intent": intent,
        "tags": tags,
        "selected_sources": selected_sources,
        "selected_source_labels": [TOPIC_SOURCE_LABELS[key] for key in selected_sources],
        "query_fallback": query_fallback,
        "query": search_query if query_fallback else "",
        "douyin_captured_at": douyin_snapshot.get("captured_at", ""),
        "mainland_hot_captured_at": mainland_snapshot.get("captured_at", ""),
        "mainland_hot_sources": mainland_snapshot.get("sources", []),
        "candidates": output[:30],
    }


def handle_api(method: str, path: str, query: Mapping[str, list[str]], body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    if method == "GET" and path == "/api/health":
        try:
            config = SERVICE.get_effective_config(
                horizon_path=str(HORIZON_ROOT), config_path=str(HORIZON_CONFIG)
            )
            return 200, {
                "status": "ready",
                "service": "horizon_topic_bridge",
                "horizon_path": str(HORIZON_ROOT),
                "config_path": str(HORIZON_CONFIG),
                "enabled_sources": config.get("selected_sources", []),
                "ai_provider": config.get("config", {}).get("ai", {}).get("provider", ""),
                "runs_root": str(RUNS_ROOT),
            }
        except Exception as exc:  # pragma: no cover - exercised by live startup
            return 503, {"status": "blocked", "message": str(exc)}

    if method == "GET" and path == "/api/runs":
        return 200, {"status": "ready", **SERVICE.list_runs(limit=20)}

    if method == "GET" and path == "/api/douyin/hot":
        board = str((query.get("board") or ["hot_search"])[0]).strip().lower() or "hot_search"
        force = str((query.get("force") or [""])[0]).strip().lower() in {"1", "true", "yes"}
        try:
            limit = max(1, min(int((query.get("limit") or ["50"])[0]), 100))
        except (TypeError, ValueError):
            limit = 50
        result = _douyin_board_snapshot(board, force=force, limit=limit)
        status = 200 if result.get("status") in {"ready", "empty"} else 503
        return status, result

    if method == "GET" and path == "/api/candidates":
        intent = str((query.get("intent") or [""])[0]).strip()
        tags = [str(item).strip() for item in query.get("tags", [])]
        sources = [str(item).strip() for item in query.get("sources", [])]
        return 200, candidates(intent, tags, sources)

    if method == "GET" and path.startswith("/api/candidates/"):
        candidate_id = unquote(path.removeprefix("/api/candidates/").strip("/"))
        candidate = _find_candidate(candidate_id)
        return (200, {"status": "ready", "candidate": candidate}) if candidate else (404, {"status": "not_found", "message": "Horizon 未找到该候选", "candidate_id": candidate_id})

    if method == "POST" and path == "/api/pipeline/fetch":
        hours = int(body.get("hours") or 24)
        sources = body.get("sources")
        source_list = [str(item).strip() for item in sources] if isinstance(sources, list) else None
        try:
            result = _run_async(
                SERVICE.fetch_items(
                    hours=hours,
                    horizon_path=str(HORIZON_ROOT),
                    config_path=str(HORIZON_CONFIG),
                    sources=source_list,
                )
            )
            return 200, {"status": "ready", "stage": "raw", **result}
        except Exception as exc:
            return 502, {"status": "failed", "stage": "raw", "message": str(exc)}

    if method == "POST" and path == "/api/pipeline/score":
        run_id = str(body.get("run_id") or "").strip()
        if not run_id:
            return 400, {"status": "invalid_input", "message": "缺少 run_id"}
        try:
            result = _run_async(
                SERVICE.score_items(
                    run_id=run_id,
                    horizon_path=str(HORIZON_ROOT),
                    config_path=str(HORIZON_CONFIG),
                )
            )
            return 200, {"status": "ready", "stage": "scored", **result}
        except Exception as exc:
            return 502, {"status": "failed", "stage": "scored", "message": str(exc)}

    if method == "POST" and path == "/api/pipeline/run":
        try:
            result = _run_async(
                SERVICE.run_pipeline(
                    hours=int(body.get("hours") or 24),
                    languages=[str(item) for item in body.get("languages", ["zh"])],
                    threshold=float(body["threshold"]) if body.get("threshold") is not None else None,
                    horizon_path=str(HORIZON_ROOT),
                    config_path=str(HORIZON_CONFIG),
                    sources=[str(item) for item in body.get("sources", [])] or None,
                    enrich=bool(body.get("enrich", False)),
                    save_to_horizon_data=False,
                )
            )
            return 200, {"status": "ready", **result}
        except Exception as exc:
            return 502, {"status": "failed", "message": str(exc)}

    return 404, {"status": "not_found", "message": f"未找到接口：{method} {path}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "HorizonTopicBridge/1.0"

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            status, payload = handle_api("GET", parsed.path, parse_qs(parsed.query), {})
        except Exception as exc:  # pragma: no cover
            status, payload = 500, {"status": "failed", "message": str(exc)}
        self._send(status, payload)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if not isinstance(body, Mapping):
                body = {}
            status, payload = handle_api("POST", parsed.path, parse_qs(parsed.query), body)
        except (ValueError, json.JSONDecodeError) as exc:
            status, payload = 400, {"status": "invalid_json", "message": str(exc)}
        except Exception as exc:  # pragma: no cover
            status, payload = 500, {"status": "failed", "message": str(exc)}
        self._send(status, payload)

    def log_message(self, format: str, *args: Any) -> None:
        print("[horizon-bridge] " + (format % args), flush=True)


def main() -> None:
    HORIZON_ROOT.mkdir(parents=True, exist_ok=True)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Horizon topic bridge listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
