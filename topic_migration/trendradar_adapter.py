"""Read-only adapter for an external TrendRadar SQLite snapshot."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Sequence

TREND_RADAR_SOURCE = "TrendRadar"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "douyin_hot": ("douyin",),
    "toutiao_hot": ("toutiao",),
    "baidu_hot": ("baidu",),
    "bilibili_hot": ("bilibili-hot-search",),
    "weibo_hot": ("weibo",),
    "rss": (),
    "rss_36kr": (),
    "rss_zhihu": (),
    "google_news": (),
    "web_search": (),
}


class TrendRadarAdapterError(RuntimeError):
    """TrendRadar snapshot cannot be read or does not match its contract."""


def _clean(value: Any, max_chars: int = 500) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:max_chars]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_terms(intent: str, tags: Sequence[str]) -> list[str]:
    raw_values = [str(intent or "").strip(), *(str(item or "").strip() for item in tags)]
    terms: list[str] = []
    for raw in raw_values:
        if not raw:
            continue
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]{1,}", raw):
            token = token.lower()
            if token not in terms:
                terms.append(token)
        for run in re.findall(r"[㐀-鿿]+", raw):
            if 2 <= len(run) <= 12 and run not in terms:
                terms.append(run)
            for width in range(2, min(4, len(run)) + 1):
                for start in range(0, len(run) - width + 1):
                    value = run[start : start + width]
                    if value not in terms:
                        terms.append(value)
    return sorted(terms, key=lambda item: (-len(item), item))


def _source_platforms(sources: Sequence[str]) -> set[str]:
    allowed: set[str] = set()
    for source in sources:
        key = str(source or "").strip().lower()
        if not key:
            continue
        aliases = SOURCE_ALIASES.get(key)
        if aliases:
            allowed.update(aliases)
        elif key not in {"horizon", "trendradar"}:
            allowed.add(key)
    return allowed


def _candidate_id(platform_id: str, news_id: Any, url: str, title: str) -> str:
    seed = f"{platform_id}|{news_id}|{url}|{title}".encode("utf-8", errors="replace")
    return "trendradar-" + hashlib.sha1(seed).hexdigest()[:20]


class TrendRadarAdapter:
    """Reads only the latest external output/news/*.db file."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    def _version(self) -> str:
        try:
            return self.root.joinpath("version").read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            return "unknown"

    def _latest_db(self) -> Path | None:
        directory = self.output_dir / "news"
        if not directory.is_dir():
            return None
        paths = [item for item in directory.glob("*.db") if item.is_file()]
        return max(paths, key=lambda item: (item.stat().st_mtime, item.name)) if paths else None

    @staticmethod
    def _read_rows(db_path: Path, limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        try:
            connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2.0)
            connection.row_factory = sqlite3.Row
        except (OSError, sqlite3.Error) as exc:
            raise TrendRadarAdapterError(f"无法只读打开 TrendRadar 数据库：{type(exc).__name__}") from exc
        try:
            cursor = connection.cursor()
            if cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='news_items'").fetchone() is None:
                raise TrendRadarAdapterError("TrendRadar 数据库缺少 news_items 表")
            sql = """
                SELECT n.id, n.title, n.platform_id,
                       COALESCE(p.name, n.platform_id) AS platform_name,
                       n.rank, n.url, n.mobile_url, n.first_crawl_time,
                       n.last_crawl_time, n.crawl_count
                FROM news_items n
                LEFT JOIN platforms p ON n.platform_id = p.id
                WHERE TRIM(COALESCE(n.title, '')) <> ''
                ORDER BY n.rank ASC, n.id ASC
            """
            params: tuple[Any, ...] = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (max(1, min(int(limit), MAX_LIMIT)),)
            rows = [dict(row) for row in cursor.execute(sql, params).fetchall()]
            crawl = cursor.execute("SELECT crawl_time, created_at, total_items FROM crawl_records ORDER BY id DESC LIMIT 1").fetchone()
            return rows, dict(crawl) if crawl is not None else {}
        except sqlite3.Error as exc:
            raise TrendRadarAdapterError(f"读取 TrendRadar 新闻快照失败：{type(exc).__name__}") from exc
        finally:
            connection.close()

    def health(self) -> dict[str, Any]:
        db_path = self._latest_db()
        base = {"service": "topic_center", "provider": TREND_RADAR_SOURCE, "source": TREND_RADAR_SOURCE, "trendradar_root": str(self.root), "version": self._version(), "read_only": True}
        if db_path is None:
            return {**base, "status": "blocked", "message": "TrendRadar 未找到 output/news/*.db；本服务不会自动启动抓取。"}
        try:
            rows, snapshot = self._read_rows(db_path, limit=1)
        except TrendRadarAdapterError as exc:
            return {**base, "status": "blocked", "database_path": str(db_path), "message": str(exc)}
        if not rows:
            return {**base, "status": "blocked", "database_path": str(db_path), "message": "TrendRadar 数据库存在，但当前没有可用新闻。"}
        return {**base, "status": "ready", "database_path": str(db_path), "database_date": db_path.stem, "latest_crawl": snapshot.get("created_at") or snapshot.get("crawl_time") or "", "item_count": _as_int(snapshot.get("total_items"), len(rows)), "message": "TrendRadar 本地快照已连接；当前适配器只读。"}

    def candidates(self, intent: str = "", tags: Sequence[str] = (), sources: Sequence[str] = (), limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        db_path = self._latest_db()
        base = {"provider": TREND_RADAR_SOURCE, "source": TREND_RADAR_SOURCE, "intent": str(intent or "").strip(), "tags": [str(item).strip() for item in tags if str(item).strip()], "sources": [str(item).strip() for item in sources if str(item).strip()]}
        if db_path is None:
            return {**base, "status": "blocked", "candidates": [], "message": "TrendRadar 尚无本地新闻快照。"}
        try:
            rows, snapshot = self._read_rows(db_path)
        except TrendRadarAdapterError as exc:
            return {**base, "status": "blocked", "candidates": [], "database_path": str(db_path), "message": str(exc)}
        allowed = _source_platforms(sources)
        if allowed:
            rows = [row for row in rows if str(row.get("platform_id") or "").lower() in allowed]
        terms = _normalize_terms(str(intent or ""), tags)
        matched: list[tuple[dict[str, Any], list[str]]] = []
        for row in rows:
            title = _clean(row.get("title"), 1000)
            matches = [term for term in terms if term.lower() in title.lower()]
            if matches:
                matched.append((row, matches))
        fallback = bool(terms) and not matched
        selected = matched if terms and matched else [(row, []) for row in rows]
        selected.sort(key=lambda pair: (-len(pair[1]), _as_int(pair[0].get("rank"), 9999), _as_int(pair[0].get("id"), 999999999)))
        candidates: list[dict[str, Any]] = []
        for row, matches in selected[: max(1, min(int(limit), MAX_LIMIT))]:
            title = _clean(row.get("title"), 500)
            url = _clean(row.get("url"), 2_000)
            if not re.match(r"^https?://[^\s]+$", url, re.I):
                continue
            platform_id = _clean(row.get("platform_id"), 100)
            source_name = _clean(row.get("platform_name") or platform_id, 100)
            rank = max(0, _as_int(row.get("rank"), 0))
            score = min(0.99, max(0.01, 1.0 / max(1, rank) + min(0.25, len(matches) * 0.04)))
            reasons = [f"TrendRadar 热榜第 {rank} 名" if rank else "TrendRadar 最新热榜"]
            if matches:
                reasons.append("命中：" + "、".join(matches[:4]))
            elif fallback:
                reasons.append("未命中筛选词，按最新热榜展示")
            crawl_time = _clean(row.get("last_crawl_time") or row.get("first_crawl_time"), 80)
            candidates.append({
                "candidate_id": _candidate_id(platform_id, row.get("id"), url, title),
                "topic_id": _candidate_id(platform_id, row.get("id"), url, title),
                "title": title,
                "url": url,
                "source_url": url,
                "source": source_name,
                "source_name": source_name,
                "source_type": "trendradar_hot",
                "platform": platform_id,
                "rank": rank,
                "score": round(score, 3),
                "recommendation": "可采用后提取真实正文；提取失败不会进入文案审核。",
                "reasons": reasons,
                "source_metadata": {"provider": TREND_RADAR_SOURCE, "platform_id": platform_id, "rank": rank, "crawl_time": crawl_time, "database_path": str(db_path), "database_date": db_path.stem, "news_item_id": row.get("id"), "mobile_url": _clean(row.get("mobile_url"), 2_000), "evidence_level": "external_snapshot"},
            })
        return {**base, "status": "ready" if candidates else "empty", "candidates": candidates, "database_path": str(db_path), "database_date": db_path.stem, "latest_crawl": snapshot.get("created_at") or snapshot.get("crawl_time") or "", "total": len(candidates), "filter_terms": terms, "fallback_to_latest": fallback, "message": "已读取 TrendRadar 本地快照；未命中筛选词，当前展示最新热榜。" if fallback else "已读取 TrendRadar 本地快照，可采用后进入正文提取。"}


__all__ = ["TREND_RADAR_SOURCE", "TrendRadarAdapter", "TrendRadarAdapterError"]
