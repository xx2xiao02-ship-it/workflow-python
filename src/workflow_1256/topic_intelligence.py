"""选题情报编排层：TrendRadar主题 + TikHub相关内容 + 重点账号。"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .tikhub_transport import TikHubTransport, filter_short_videos
from .trendradar_adapter import TrendRadarAdapter


CACHE_TTL = timedelta(minutes=20)
_HEX_ID_RE = re.compile(r"^[0-9a-f]{20,}$", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def _readable_content(item: Any) -> bool:
    """缓存/接口内容必须至少有可读标题，URL存在时还必须是可追溯的HTTP来源。"""
    if not isinstance(item, Mapping):
        return False
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return True
    return bool(title) and not _HEX_ID_RE.fullmatch(title)


class TopicIntelligenceService:
    """不自动调用付费接口；只有 ``enrich`` 或账号监控显式调用时才请求TikHub。"""

    def __init__(self, trendradar: TrendRadarAdapter, tikhub: TikHubTransport, cache_path: Path) -> None:
        self.trendradar = trendradar
        self.tikhub = tikhub
        self.cache_path = Path(cache_path)
        self._lock = threading.RLock()

    def _read_cache(self) -> dict[str, Any]:
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema_version": 1, "entries": {}}
        if not isinstance(value, Mapping):
            return {"schema_version": 1, "entries": {}}
        entries = value.get("entries") if isinstance(value.get("entries"), Mapping) else {}
        return {"schema_version": 1, "entries": {str(key): dict(item) for key, item in entries.items() if isinstance(item, Mapping)}}

    def _write_cache(self, document: Mapping[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name(f".{self.cache_path.name}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(json.dumps(_safe_json(document), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.cache_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _cache_key(candidate_id: str, platforms: Sequence[str], limit: int, wechat_ghid: str) -> str:
        raw = "|".join([candidate_id, ",".join(sorted(set(platforms))), str(limit), wechat_ghid])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def health(self) -> dict[str, Any]:
        trend = self.trendradar.health()
        tikhub = self.tikhub.health()
        if trend.get("status") == "ready" and tikhub.get("status") == "ready":
            status = "ready"
        elif trend.get("status") == "ready":
            status = "partial"
        else:
            status = "blocked"
        return {
            "status": status,
            "service": "topic_center",
            "providers": {"trendradar": trend, "tikhub": tikhub},
            "message": "TrendRadar与TikHub配置已就绪，可按需查询。" if status == "ready" else "部分数据能力尚未就绪，请查看providers中的具体原因。",
        }

    @staticmethod
    def _sanitize_result(result: Mapping[str, Any]) -> dict[str, Any]:
        """隐藏历史缓存中只有哈希ID或无可追溯来源的记录，不改写原始缓存。"""
        sanitized = dict(result)
        raw_contents = result.get("contents")
        if not isinstance(raw_contents, list):
            return sanitized
        readable_contents = [dict(item) for item in raw_contents if _readable_content(item)]
        contents, short_removed = filter_short_videos(readable_contents)
        removed = len(raw_contents) - len(readable_contents)
        sanitized["contents"] = contents
        try:
            already_filtered = int(sanitized.get("filtered_short_videos") or 0)
        except (TypeError, ValueError):
            already_filtered = 0
        sanitized["filtered_short_videos"] = already_filtered + short_removed
        if removed or short_removed:
            warnings: list[str] = []
            if removed:
                warnings.append("缓存中有内容缺少可读标题或来源链接，已隐藏")
            if short_removed:
                warnings.append(f"已隐藏 {short_removed} 条不超过1分钟的视频")
            warning = "；".join(warnings) + "；历史缓存未删除，请点击“查询相关内容”重新读取。"
            sanitized["cache_warning"] = warning
            sanitized["message"] = warning
            if sanitized.get("status") == "ready" and not contents:
                sanitized["status"] = "partial"
        return sanitized

    def topics(
        self,
        *,
        intent: str = "",
        tags: Sequence[str] = (),
        sources: Sequence[str] = (),
        limit: int = 50,
    ) -> dict[str, Any]:
        result = self.trendradar.candidates(intent=intent, tags=tags, sources=sources, limit=limit)
        candidates = []
        for item in result.get("candidates", []) if isinstance(result.get("candidates"), list) else []:
            if not isinstance(item, Mapping):
                continue
            candidate = dict(item)
            candidate["topic_id"] = str(candidate.get("candidate_id") or "")
            candidate["keywords"] = list(dict.fromkeys([str(item.get("title") or "").strip(), *[str(tag) for tag in tags if str(tag).strip()]]))[:8]
            candidate["selection_state"] = "DISCOVERED"
            candidates.append(candidate)
        return {**result, "topics": candidates, "candidates": candidates, "auto_recommended": True, "generated_at": utc_now()}

    def enrich(
        self,
        candidate: Mapping[str, Any],
        *,
        platforms: Sequence[str] = ("douyin", "xiaohongshu", "weibo", "zhihu", "wechat"),
        limit: int = 20,
        wechat_ghid: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        candidate_id = str(candidate.get("candidate_id") or candidate.get("topic_id") or "").strip()
        title = str(candidate.get("title") or "").strip()
        if not candidate_id or not title:
            return {"status": "invalid_input", "contents": [], "message": "主题缺少candidate_id或title"}
        key = self._cache_key(candidate_id, platforms, limit, wechat_ghid)
        with self._lock:
            cache = self._read_cache()
            cached = cache["entries"].get(key)
            if not force and isinstance(cached, Mapping):
                try:
                    saved_at = datetime.fromisoformat(str(cached.get("saved_at") or ""))
                except ValueError:
                    saved_at = datetime.min.replace(tzinfo=UTC)
                if datetime.now(UTC) - saved_at <= CACHE_TTL:
                    cached_result = cached.get("result") if isinstance(cached.get("result"), Mapping) else {}
                    return {
                        **self._sanitize_result(cached_result),
                        "cached": True,
                        "cache_key": key,
                    }
        result = self.tikhub.search_related(title, platforms=platforms, limit=limit, wechat_ghid=wechat_ghid)
        enriched = {
            **result,
            "candidate_id": candidate_id,
            "topic_id": candidate_id,
            "topic_title": title,
            "queried_at": utc_now(),
            "cached": False,
            "cache_key": key,
        }
        with self._lock:
            cache = self._read_cache()
            cache["entries"][key] = {"saved_at": utc_now(), "candidate_id": candidate_id, "result": enriched}
            self._write_cache(cache)
        return self._sanitize_result(enriched)

    def cached_enrich(
        self,
        candidate_id: str,
        *,
        platforms: Sequence[str] = ("douyin", "xiaohongshu", "weibo", "zhihu", "wechat"),
        limit: int = 20,
        wechat_ghid: str = "",
    ) -> dict[str, Any] | None:
        key = self._cache_key(str(candidate_id or "").strip(), platforms, limit, wechat_ghid)
        with self._lock:
            cached = self._read_cache()["entries"].get(key)
        if not isinstance(cached, Mapping):
            return None
        result = cached.get("result") if isinstance(cached.get("result"), Mapping) else None
        return self._sanitize_result(result) if result is not None else None

    def load_watchlist(self, path: Path) -> list[dict[str, Any]]:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = value.get("accounts") if isinstance(value, Mapping) else value
        return [dict(item) for item in entries if isinstance(item, Mapping)] if isinstance(entries, list) else []

    def accounts(self, path: Path) -> dict[str, Any]:
        accounts = self.load_watchlist(path)
        return {
            "status": "ready" if accounts else "empty",
            "accounts": accounts,
            "configured": bool(accounts),
            "message": "已读取重点账号配置。" if accounts else "尚未配置重点账号；添加账号后才能执行监控。",
        }

    def save_watchlist(self, path: Path, accounts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """保存重点账号配置；这里只写账号标识，不触发TikHub调用。"""
        allowed = {"douyin", "xiaohongshu", "weibo", "zhihu", "wechat"}
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in list(accounts)[:200]:
            if not isinstance(item, Mapping):
                continue
            platform = str(item.get("platform") or "").strip().lower()
            account_id = str(item.get("account_id") or item.get("uid") or item.get("sec_uid") or "").strip()
            url = str(item.get("url") or "").strip()
            if platform not in allowed or (not account_id and not url):
                continue
            if url:
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    continue
            key = f"{platform}|{account_id or url}"
            if key in seen:
                continue
            seen.add(key)
            normalized.append({
                "platform": platform,
                "account_id": account_id,
                "url": url,
                "name": str(item.get("name") or item.get("nickname") or "").strip()[:200],
                "monitor_enabled": bool(item.get("monitor_enabled", True)),
                "updated_at": utc_now(),
            })
        document = {"schema_version": 1, "updated_at": utc_now(), "accounts": normalized}
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return {"status": "ready" if normalized else "empty", "accounts": normalized, "saved": len(normalized)}


__all__ = ["CACHE_TTL", "TopicIntelligenceService", "utc_now"]
