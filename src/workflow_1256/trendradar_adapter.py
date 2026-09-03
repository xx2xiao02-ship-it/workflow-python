"""TrendRadar 本地数据适配层。

TrendRadar 的职责是抓取和聚合热点，本项目只读取它已经落盘的 SQLite
快照，不直接改写 TrendRadar 数据库，也不把 Horizon 的结果冒充成
TrendRadar。适配层返回主项目现有选题候选字段，供联合治理台消费。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


TREND_RADAR_SOURCE = "TrendRadar"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# 兼容旧选题中心的信源筛选值；新联合治理台默认不传 sources，
# 传入时仍可以按 TrendRadar 平台过滤。
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
    """TrendRadar 快照不可读取或不符合预期。"""


def _clean(value: Any, max_chars: int = 500) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:max_chars]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_terms(intent: str, tags: Sequence[str]) -> list[str]:
    """从自然语言和标签提取用于本地标题筛选的稳定词项。

    这里不调用模型：TrendRadar 已经完成热点聚合，主项目只做透明的标题
    子串筛选。若一个词都未命中，会退回当前热榜，避免用户看到空白页面。
    """
    raw_values = [str(intent or "").strip(), *(str(item or "").strip() for item in tags)]
    terms: list[str] = []
    for raw in raw_values:
        if not raw:
            continue
        # 英文、数字和常见连字符词组。
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{1,}", raw):
            value = token.lower()
            if len(value) >= 2 and value not in terms:
                terms.append(value)
        # 中文连续片段：保留完整短片段及长度为 2~4 的窗口，兼容“人工
        # 智能”“职场提效”等短标题，不生成单字噪声。
        for run in re.findall(r"[\u3400-\u9fff]+", raw):
            if 2 <= len(run) <= 12 and run not in terms:
                terms.append(run)
            max_window = min(4, len(run))
            for width in range(2, max_window + 1):
                for start in range(0, len(run) - width + 1):
                    value = run[start : start + width]
                    if value not in terms:
                        terms.append(value)
    # 长词优先，避免通用短词把所有标题都判为命中。
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
            # 允许直接传 TrendRadar platform_id。
            allowed.add(key)
    return allowed


def _candidate_id(platform_id: str, news_id: Any, url: str, title: str) -> str:
    seed = f"{platform_id}|{news_id}|{url}|{title}".encode("utf-8", errors="replace")
    return "trendradar-" + hashlib.sha1(seed).hexdigest()[:20]


class TrendRadarAdapter:
    """只读 TrendRadar 输出目录。"""

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
        if not paths:
            return None
        # mtime 代表本地实际生成时间；文件名日期用于同一时间的稳定排序。
        return max(paths, key=lambda item: (item.stat().st_mtime, item.name))

    @staticmethod
    def _read_rows(db_path: Path, limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        try:
            uri = f"file:{db_path.as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=2.0)
            connection.row_factory = sqlite3.Row
        except (OSError, sqlite3.Error) as exc:
            raise TrendRadarAdapterError(f"无法只读打开 TrendRadar 数据库：{type(exc).__name__}") from exc
        try:
            cursor = connection.cursor()
            table = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='news_items'"
            ).fetchone()
            if table is None:
                raise TrendRadarAdapterError("TrendRadar 数据库缺少 news_items 表")
            sql = """
                SELECT n.id, n.title, n.platform_id,
                       COALESCE(p.name, n.platform_id) AS platform_name,
                       n.rank, n.url, n.mobile_url,
                       n.first_crawl_time, n.last_crawl_time, n.crawl_count
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
            crawl = cursor.execute(
                "SELECT crawl_time, created_at, total_items FROM crawl_records ORDER BY id DESC LIMIT 1"
            ).fetchone()
            snapshot = dict(crawl) if crawl is not None else {}
            return rows, snapshot
        except sqlite3.Error as exc:
            raise TrendRadarAdapterError(f"读取 TrendRadar 新闻快照失败：{type(exc).__name__}") from exc
        finally:
            connection.close()

    def health(self) -> dict[str, Any]:
        db_path = self._latest_db()
        base: dict[str, Any] = {
            "service": "topic_writing_governance",
            "provider": TREND_RADAR_SOURCE,
            "source": TREND_RADAR_SOURCE,
            "trendradar_root": str(self.root),
            "version": self._version(),
        }
        if db_path is None:
            return {
                **base,
                "status": "blocked",
                "message": "TrendRadar 未找到 output/news/*.db；请先运行抓取任务。",
            }
        try:
            rows, snapshot = self._read_rows(db_path, limit=1)
        except TrendRadarAdapterError as exc:
            return {**base, "status": "blocked", "database_path": str(db_path), "message": str(exc)}
        if not rows:
            return {
                **base,
                "status": "blocked",
                "database_path": str(db_path),
                "message": "TrendRadar 数据库存在，但当前没有可用新闻。",
            }
        return {
            **base,
            "status": "ready",
            "database_path": str(db_path),
            "database_date": db_path.stem,
            "latest_crawl": snapshot.get("created_at") or snapshot.get("crawl_time") or "",
            "item_count": _as_int(snapshot.get("total_items"), 0),
            "message": "TrendRadar 本地快照已连接，联合治理台将从此处读取候选。",
        }

    def candidates(
        self,
        intent: str = "",
        tags: Sequence[str] = (),
        sources: Sequence[str] = (),
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        db_path = self._latest_db()
        base: dict[str, Any] = {
            "provider": TREND_RADAR_SOURCE,
            "source": TREND_RADAR_SOURCE,
            "intent": str(intent or "").strip(),
            "tags": [str(item).strip() for item in tags if str(item).strip()],
            "sources": [str(item).strip() for item in sources if str(item).strip()],
        }
        if db_path is None:
            return {**base, "status": "blocked", "candidates": [], "message": "TrendRadar 尚无本地新闻快照。"}
        try:
            rows, snapshot = self._read_rows(db_path, limit=None)
        except TrendRadarAdapterError as exc:
            return {**base, "status": "blocked", "candidates": [], "database_path": str(db_path), "message": str(exc)}

        allowed_platforms = _source_platforms(sources)
        if allowed_platforms:
            rows = [row for row in rows if str(row.get("platform_id") or "").lower() in allowed_platforms]

        terms = _normalize_terms(str(intent or ""), tags)
        matched_rows: list[tuple[dict[str, Any], list[str]]] = []
        for row in rows:
            title = _clean(row.get("title"), 1000)
            lowered = title.lower()
            matches = [term for term in terms if term.lower() in lowered]
            if matches:
                matched_rows.append((row, matches))
        # 有命中时严格按命中结果筛选；没有命中时保留当前热榜，给出透明原因。
        used_fallback = bool(terms) and not matched_rows
        selected = matched_rows if terms and matched_rows else [(row, []) for row in rows]
        selected.sort(
            key=lambda pair: (
                -len(pair[1]),
                _as_int(pair[0].get("rank"), 9999),
                _as_int(pair[0].get("id"), 999999999),
            )
        )

        candidates: list[dict[str, Any]] = []
        for row, matches in selected[: max(1, min(int(limit), MAX_LIMIT))]:
            title = _clean(row.get("title"), 500)
            url = _clean(row.get("url"), 2000)
            if not re.match(r"^https?://[^\s]+$", url, flags=re.IGNORECASE):
                # 采用后必须能够进入正文提取，过滤掉没有真实 HTTP 来源的行。
                continue
            platform_id = _clean(row.get("platform_id"), 100)
            source_name = _clean(row.get("platform_name") or platform_id, 100)
            rank = max(0, _as_int(row.get("rank"), 0))
            score = min(0.99, max(0.01, 1.0 / max(1, rank) + min(0.25, len(matches) * 0.04)))
            reasons = [f"TrendRadar 热榜第 {rank} 名" if rank else "TrendRadar 最新热榜"]
            if matches:
                reasons.append("命中：" + "、".join(matches[:4]))
            elif used_fallback:
                reasons.append("未命中筛选词，按最新热榜展示")
            crawl_time = _clean(row.get("last_crawl_time") or row.get("first_crawl_time"), 80)
            candidates.append(
                {
                    "candidate_id": _candidate_id(platform_id, row.get("id"), url, title),
                    "title": title,
                    "url": url,
                    "source_url": url,
                    "source": source_name,
                    "source_name": source_name,
                    "source_type": "trendradar_hot",
                    "platform": platform_id,
                    "rank": rank,
                    "score": round(score, 3),
                    "recommendation": "可采用后提取真实正文，提取失败不会进入文案审核。",
                    "reasons": reasons,
                    "source_metadata": {
                        "provider": TREND_RADAR_SOURCE,
                        "platform_id": platform_id,
                        "rank": rank,
                        "crawl_time": crawl_time,
                        "database_path": str(db_path),
                        "database_date": db_path.stem,
                        "news_item_id": row.get("id"),
                        "mobile_url": _clean(row.get("mobile_url"), 2000),
                    },
                }
            )
        return {
            **base,
            "status": "ready" if candidates else "empty",
            "candidates": candidates,
            "database_path": str(db_path),
            "database_date": db_path.stem,
            "latest_crawl": snapshot.get("created_at") or snapshot.get("crawl_time") or "",
            "total": len(candidates),
            "filter_terms": terms,
            "fallback_to_latest": used_fallback,
            "message": (
                "已读取 TrendRadar 本地快照；未命中筛选词，当前展示最新热榜。"
                if used_fallback
                else "已读取 TrendRadar 本地快照，可采用后进入正文提取。"
            ),
        }


__all__ = ["TREND_RADAR_SOURCE", "TrendRadarAdapter", "TrendRadarAdapterError"]
