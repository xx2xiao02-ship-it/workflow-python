"""选题中心业务编排层。

该层只依赖已登记的 TrendRadar 只读适配器、control-plane ConfigService、
TikHubTransport、正文采集器和治理存储。它不导入旧项目控制台，也不把
未迁移的文案、编导、素材或剪辑存储暴露给选题中心。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse
from urllib.request import urlopen

from .config_service import ConfigService
from .errors import NotFoundError, PersistenceError, ValidationError
from .governance_store import TopicGovernanceStore, utc_now
from .professional_media import professional_media_contents
from .tikhub_transport import TikHubTransport, filter_short_videos
from .topic_collection import SOCIAL_PLATFORMS, collect_topic_source
from .trendradar_adapter import TrendRadarAdapter

CACHE_TTL = timedelta(minutes=20)
REFRESH_INTERVALS = (1, 6, 12)
SUPPORTED_CONTENT_PLATFORMS = ("douyin", "xiaohongshu", "weibo", "zhihu", "wechat")


def default_runtime_root() -> Path:
    configured = str(os.environ.get("TOPIC_CENTER_RUNTIME_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        return Path(local_app_data) / "TopicCenterMigration"
    return Path.home() / ".topic-center-migration"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return default
    return value


def _clean_list(value: Any, *, limit: int, max_chars: int = 200) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else ([value] if value else [])
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()[:max_chars]
        if text and text not in result:
            result.append(text)
    return result[:limit]


def _normalize_platforms(value: Any) -> list[str]:
    if value is None or value == "":
        raw = list(SUPPORTED_CONTENT_PLATFORMS)
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    values: list[str] = []
    for item in raw:
        for part in str(item or "").split(","):
            platform = part.strip().lower()
            if platform and platform not in values:
                values.append(platform)
    unknown = [item for item in values if item not in SUPPORTED_CONTENT_PLATFORMS]
    if unknown:
        raise ValidationError("platforms 包含未登记平台：" + ", ".join(unknown))
    return values or list(SUPPORTED_CONTENT_PLATFORMS)


def _normalize_limit(value: Any, *, default: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValidationError("limit 必须是整数")
    if isinstance(value, float) and not value.is_integer():
        raise ValidationError("limit 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("limit 必须是整数") from exc
    return max(1, min(number, maximum))


class TopicCenterService:
    """F1 选题中心的唯一业务入口。"""

    def __init__(
        self,
        runtime_root: Path | str | None = None,
        *,
        trendradar_root: Path | str | None = None,
        config_service: ConfigService | None = None,
        tikhub: TikHubTransport | None = None,
        tikhub_opener: Callable[..., Any] = urlopen,
        collection_opener: Callable[..., Any] = urlopen,
        preview_opener: Callable[..., Any] = urlopen,
        media_requester: Callable[[], bytes] | None = None,
    ) -> None:
        self.root = Path(runtime_root).expanduser() if runtime_root is not None else default_runtime_root()
        source_root = Path(__file__).resolve().parent.parent
        try:
            self.root.resolve().relative_to(source_root)
        except ValueError:
            pass
        else:
            raise PersistenceError("选题中心运行目录不能位于源码仓库内", code="runtime_root_inside_source")
        self.topic_root = self.root / "topic_center"
        self.control_plane_root = self.root / "control_plane"
        radar_value = str(trendradar_root or os.environ.get("TRENDRADAR_ROOT") or "").strip()
        self.trendradar_root = Path(radar_value).expanduser() if radar_value else self.root / "trendradar"
        self.config_service = config_service or ConfigService(self.control_plane_root)
        self.trendradar = TrendRadarAdapter(self.trendradar_root)
        self.tikhub = tikhub or TikHubTransport(self.config_service, opener=tikhub_opener)
        self.collection_opener = collection_opener
        self.preview_opener = preview_opener
        self.media_requester = media_requester
        self.store = TopicGovernanceStore(self.topic_root / "governance.json")
        self.cache_path = self.topic_root / "content_cache.json"
        self.schedule_path = self.topic_root / "refresh_schedule.json"
        self.accounts_path = self.topic_root / "accounts.json"
        self.decisions_path = self.topic_root / "decisions.json"
        self._lock = threading.RLock()

    def health(self) -> dict[str, Any]:
        trend = self.trendradar.health()
        tikhub = self.tikhub.health()
        configured = bool(tikhub.get("auth_configured"))
        if trend.get("status") == "ready" and configured:
            status = "partial"
            message = "TrendRadar 快照可读，TikHub 已配置但尚未完成真实供应商验证。"
        elif trend.get("status") == "ready":
            status = "partial"
            message = "TrendRadar 快照可读；TikHub 尚未配置，按需查询将被阻断。"
        else:
            status = "blocked"
            message = "TrendRadar 本地快照不可用；服务不会自动启动抓取。"
        return {
            "status": status,
            "service": "topic_center",
            "providers": {"trendradar": trend, "tikhub": tikhub},
            "storage": {
                "runtime_root": str(self.root),
                "topic_store": str(self.store.path),
                "writable_source_policy": "只写独立运行目录，不写旧项目、TrendRadar 快照或源码目录。",
            },
            "external_requests_on_health": False,
            "scheduled_refresh_running": False,
            "message": message,
        }

    def topics(self, *, intent: str = "", tags: Sequence[str] = (), sources: Sequence[str] = (), limit: int = 50) -> dict[str, Any]:
        clean_limit = _normalize_limit(limit, default=50, maximum=200)
        result = self.trendradar.candidates(intent=intent, tags=tags, sources=sources, limit=clean_limit)
        candidates: list[dict[str, Any]] = []
        for raw in result.get("candidates", []) if isinstance(result.get("candidates"), list) else []:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            item["topic_id"] = str(item.get("candidate_id") or "")
            item["keywords"] = _clean_list([item.get("title"), *tags], limit=8, max_chars=200)
            item["selection_state"] = "DISCOVERED"
            candidates.append(item)
        return {**result, "topics": candidates, "candidates": candidates, "auto_recommended": True, "generated_at": utc_now()}

    def _find_topic(self, candidate_id: str) -> dict[str, Any]:
        clean_id = str(candidate_id or "").strip()
        if not clean_id:
            raise ValidationError("candidate_id 不能为空")
        result = self.topics(limit=200)
        candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
        for item in candidates:
            if isinstance(item, Mapping) and str(item.get("candidate_id") or "") == clean_id:
                return dict(item)
        raise NotFoundError("未找到 TrendRadar 主题，请刷新后重试")

    @staticmethod
    def _cache_key(candidate_id: str, platforms: Sequence[str], limit: int, wechat_ghid: str) -> str:
        raw = "|".join((candidate_id, ",".join(sorted(set(platforms))), str(limit), wechat_ghid))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _read_cache(self) -> dict[str, Any]:
        value = _read_json(self.cache_path, {})
        entries = value.get("entries") if isinstance(value, Mapping) and isinstance(value.get("entries"), Mapping) else {}
        return {"schema_version": 1, "entries": {str(k): dict(v) for k, v in entries.items() if isinstance(v, Mapping)}}

    def _sanitize_enrich(self, result: Mapping[str, Any]) -> dict[str, Any]:
        sanitized = dict(result)
        raw = result.get("contents")
        if not isinstance(raw, list):
            return sanitized
        contents: list[dict[str, Any]] = []
        removed = 0
        for item in raw:
            if not isinstance(item, Mapping):
                removed += 1
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            parsed = urlparse(url)
            if not title or (parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc):
                removed += 1
                continue
            contents.append(dict(item))
        contents, short_removed = filter_short_videos(contents)
        sanitized["contents"] = contents
        sanitized["filtered_short_videos"] = int(result.get("filtered_short_videos") or 0) + short_removed
        if removed or short_removed:
            warning = "；".join(filter(None, [
                "缓存中有缺少可读标题或真实来源的记录，已隐藏" if removed else "",
                f"已隐藏 {short_removed} 条不超过 1 分钟的视频" if short_removed else "",
            ])) + "；历史缓存未删除。"
            sanitized["cache_warning"] = warning
            sanitized["message"] = warning
            if sanitized.get("status") == "ready" and not contents:
                sanitized["status"] = "partial"
        return sanitized

    def enrich(self, candidate_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        candidate = self._find_topic(candidate_id)
        options = dict(payload or {})
        platforms = _normalize_platforms(options.get("platforms"))
        limit = _normalize_limit(options.get("limit"), default=20, maximum=50)
        ghid = str(options.get("wechat_ghid") or "").strip()[:200]
        force = str(options.get("force") or "").strip().lower() in {"1", "true", "yes"}
        key = self._cache_key(candidate_id, platforms, limit, ghid)
        with self._lock:
            cached = self._read_cache()["entries"].get(key)
        if not force and isinstance(cached, Mapping):
            try:
                saved_at = datetime.fromisoformat(str(cached.get("saved_at") or ""))
            except ValueError:
                saved_at = datetime.min.replace(tzinfo=UTC)
            if datetime.now(UTC) - saved_at <= CACHE_TTL and isinstance(cached.get("result"), Mapping):
                return {**self._sanitize_enrich(cached["result"]), "cached": True, "cache_key": key}
        result = self.tikhub.search_related(candidate["title"], platforms=platforms, limit=limit, wechat_ghid=ghid)
        enriched = {**result, "candidate_id": candidate_id, "topic_id": candidate_id, "topic_title": candidate["title"], "queried_at": utc_now(), "cached": False, "cache_key": key}
        with self._lock:
            cache = self._read_cache()
            cache["entries"][key] = {"saved_at": utc_now(), "candidate_id": candidate_id, "result": enriched}
            _atomic_json(self.cache_path, cache)
        return self._sanitize_enrich(enriched)

    def cached_contents(self, candidate_id: str, *, platforms: Sequence[str] = SUPPORTED_CONTENT_PLATFORMS, limit: int = 20, wechat_ghid: str = "") -> dict[str, Any]:
        clean_platforms = _normalize_platforms(platforms)
        clean_limit = _normalize_limit(limit, default=20, maximum=50)
        key = self._cache_key(str(candidate_id or "").strip(), clean_platforms, clean_limit, str(wechat_ghid or "").strip())
        cached = self._read_cache()["entries"].get(key)
        if not isinstance(cached, Mapping) or not isinstance(cached.get("result"), Mapping):
            return {"status": "not_enriched", "candidate_id": candidate_id, "contents": [], "message": "请先点击查询相关内容"}
        return {**self._sanitize_enrich(cached["result"]), "cached": True, "cache_key": key}

    def preview(self, candidate_id: str) -> dict[str, Any]:
        candidate = self._find_topic(candidate_id)
        source_url = str(candidate.get("url") or candidate.get("source_url") or "").strip()
        parsed = urlparse(source_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            return {"status": "invalid_input", "candidate_id": candidate_id, "source_url": source_url, "content_preview": "", "message": "该主题没有可读取的真实来源链接"}
        platform = str(candidate.get("platform") or "").strip().lower()
        if platform == "zhihu" or platform in SOCIAL_PLATFORMS or any(platform.startswith(item + "-") for item in SOCIAL_PLATFORMS):
            return {"status": "deferred", "candidate_id": candidate_id, "source_url": source_url, "content_preview": "", "message": "该平台正文需在查询相关内容或主素材采集时获取；当前不自动计费。"}
        result = collect_topic_source({"selection_id": "preview-" + candidate_id, "source_url": source_url, "platform": platform, "title": candidate.get("title"), "source_metadata": {"preview_only": True, "provider": "TrendRadar"}}, timeout=15.0, opener=self.preview_opener, tikhub=self.tikhub)
        content = str(result.get("content") or "").strip()
        if not content:
            return {"status": result.get("status") or "content_missing", "candidate_id": candidate_id, "source_url": source_url, "content_preview": "", "content_type": result.get("content_type") or "", "message": result.get("message") or "来源页面未返回可展示正文"}
        preview = content[:360]
        return {"status": "ready", "candidate_id": candidate_id, "source_url": source_url, "title": result.get("title") or candidate.get("title"), "content_preview": preview, "content_preview_source": "source_html_preview", "content_length": len(content), "content_type": result.get("content_type") or "html_text", "message": "已读取来源页面正文预览；预览不会进入文案链路。"}

    def create_selection(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValidationError("建立选题包请求必须是对象")
        content_items = payload.get("content_items")
        if not isinstance(content_items, list) or not content_items:
            raise ValidationError("建立选题包前必须先查询并选择至少一条真实内容")
        primary_id = str(payload.get("primary_content_id") or "").strip()
        if not primary_id:
            raise ValidationError("必须指定一条 primary_content_id 作为主素材")
        primary = next((item for item in content_items if isinstance(item, Mapping) and str(item.get("content_id") or "") == primary_id), None)
        if not isinstance(primary, Mapping):
            raise ValidationError("primary_content_id 不在当前查询结果中")
        if filter_short_videos([dict(item) for item in content_items if isinstance(item, Mapping)])[1]:
            raise ValidationError("不能选择时长不超过1分钟的视频")
        merged = dict(payload)
        merged.setdefault("source_url", primary.get("url") or primary.get("source_url") or "")
        merged.setdefault("title", primary.get("title") or "")
        merged.setdefault("source_name", primary.get("platform") or primary.get("provider") or "TikHub")
        merged.setdefault("platform", primary.get("platform") or "article")
        metadata = merged.get("source_metadata") if isinstance(merged.get("source_metadata"), Mapping) else {}
        merged["source_metadata"] = {**dict(metadata), "primary_content_id": primary_id, "reference_content_ids": merged.get("reference_content_ids") or []}
        selection = self.store.save_selection(merged)
        return {"status": "accepted", "selection": selection, "governance_state": selection.get("state"), "message": "主素材已确认，下一步采集正文或字幕；采集成功后才能进入文案。"}

    def accept_governance(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        selection_payload = dict(payload)
        selection_payload["decision"] = "accepted"
        selection = self.store.save_selection(selection_payload)
        return {"status": "accepted", "selection": selection, "governance_state": selection.get("state"), "message": "选题已采用，下一步提取真实正文；尚未进入文案生成。"}

    def collect(self, selection_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        selection = self.store.require_selection(selection_id)
        self.store.update_selection(selection_id, state="EXTRACTING", message="正在提取正文或字幕，请稍候")
        options = dict(payload or {})
        try:
            raw_timeout = options.get("timeout_seconds")
            timeout_value = 20.0 if raw_timeout in (None, "") else float(raw_timeout)
            if not math.isfinite(timeout_value):
                raise ValueError
            timeout = max(5.0, min(timeout_value, 60.0))
        except (TypeError, ValueError) as exc:
            raise ValidationError("timeout_seconds 必须是数字") from exc
        request_selection = dict(selection)
        if not isinstance(request_selection.get("collection"), Mapping):
            request_selection["collection"] = {"route": "article_html"}
        try:
            result = collect_topic_source(request_selection, timeout=timeout, opener=self.collection_opener, tikhub=self.tikhub)
        except Exception as exc:
            result = {"status": "failed", "selection_id": selection_id, "source_url": selection.get("source_url") or "", "final_url": selection.get("source_url") or "", "title": selection.get("title") or "", "content": "", "error_code": "collector_exception", "message": f"正文提取异常：{type(exc).__name__}"}
        source = self.store.save_extraction_result(selection_id, result)
        current = self.store.require_selection(selection_id)
        return {"status": current.get("state"), "selection": current, "source": source, "collector_result": dict(result), "retryable": source.get("status") != "CONTENT_READY", "message": source.get("message") or current.get("message") or "正文提取完成"}

    def writing_input(self, source_content_id: str, selection_id: str = "", *, project_id: str = "topic-center", run_id: str = "local") -> dict[str, Any]:
        return self.store.writing_input(source_content_id, selection_id, project_id=project_id, run_id=run_id)

    def professional_media(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return professional_media_contents(payload, requester=self.media_requester)

    def archive_source(self, source_content_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        source = self.store.archive_source(source_content_id, reason=str((payload or {}).get("reason") or "user_remove_queue"))
        return {"status": "ARCHIVED", "source": source, "message": "已移出创作队列，历史记录仍保留。"}

    def list_accounts(self) -> dict[str, Any]:
        value = _read_json(self.accounts_path, {})
        accounts = value.get("accounts") if isinstance(value, Mapping) and isinstance(value.get("accounts"), list) else []
        return {"status": "ready" if accounts else "empty", "accounts": [dict(item) for item in accounts if isinstance(item, Mapping)], "configured": bool(accounts), "message": "已读取重点账号配置。" if accounts else "尚未配置重点账号；本批不会自动启动监控。"}

    def save_accounts(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        accounts = payload.get("accounts") if isinstance(payload, Mapping) else None
        if not isinstance(accounts, list):
            raise ValidationError("accounts 必须是数组")
        allowed = set(SUPPORTED_CONTENT_PLATFORMS)
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in accounts[:200]:
            if not isinstance(item, Mapping):
                raise ValidationError("accounts 的每一项必须是对象")
            platform = str(item.get("platform") or "").strip().lower()
            account_id = str(item.get("account_id") or item.get("uid") or item.get("sec_uid") or "").strip()[:200]
            url = str(item.get("url") or "").strip()
            if platform not in allowed or (not account_id and not url):
                raise ValidationError("重点账号必须包含已登记平台和 account_id 或 URL")
            if url:
                parsed = urlparse(url)
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                    raise ValidationError("重点账号 URL 必须是 HTTP(S) 地址")
            key = f"{platform}|{account_id or url}"
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"platform": platform, "account_id": account_id, "url": url, "name": str(item.get("name") or item.get("nickname") or "").strip()[:200], "monitor_enabled": bool(item.get("monitor_enabled", True)), "updated_at": utc_now()})
        document = {"schema_version": 1, "updated_at": utc_now(), "accounts": normalized}
        _atomic_json(self.accounts_path, document)
        return {"status": "ready" if normalized else "empty", "accounts": normalized, "saved": len(normalized), "monitor_started": False}

    @staticmethod
    def _default_schedule() -> dict[str, Any]:
        return {"version": 1, "enabled": True, "interval_hours": 1, "updated_at": "", "last_run_at": "", "last_run_status": "idle", "last_run_message": "尚未执行；本批只保存计划，不自动启动抓取。", "next_run_at": "", "refresh_started": False}

    def get_refresh_schedule(self) -> dict[str, Any]:
        schedule = self._default_schedule()
        raw = _read_json(self.schedule_path, {})
        if isinstance(raw, Mapping):
            schedule.update({str(k): v for k, v in raw.items() if str(k) in schedule})
        try:
            interval = int(schedule.get("interval_hours") or 1)
        except (TypeError, ValueError):
            interval = 1
        schedule["interval_hours"] = interval if interval in REFRESH_INTERVALS else 1
        schedule["status"] = "ready"
        return schedule

    def save_refresh_schedule(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValidationError("刷新计划请求必须是对象")
        try:
            interval = int(payload.get("interval_hours") or 0)
        except (TypeError, ValueError) as exc:
            raise ValidationError("interval_hours 必须是 1、6 或 12") from exc
        if interval not in REFRESH_INTERVALS:
            raise ValidationError("新闻库更新频率只能选择每小时、每6小时或每12小时")
        now = utc_now()
        schedule = self.get_refresh_schedule()
        schedule.update({"enabled": True, "interval_hours": interval, "updated_at": now, "last_run_status": "not_started", "last_run_message": "已保存刷新计划；本批未自动启动抓取或定时任务。", "refresh_started": False, "next_run_at": "", "status": "ready"})
        _atomic_json(self.schedule_path, schedule)
        return {**schedule, "message": "已保存刷新计划；按当前迁移边界未自动启动抓取或定时任务。"}

    def save_decision(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        candidate_id = str(payload.get("candidate_id") or "").strip()
        decision = str(payload.get("status") or "").strip().lower()
        if not candidate_id or decision not in {"accepted", "ignored", "later"}:
            raise ValidationError("需要 candidate_id，以及 accepted、ignored 或 later 状态")
        record = {"candidate_id": candidate_id, "decision": decision, "title": str(payload.get("title") or "").strip()[:500], "source_url": str(payload.get("source_url") or "").strip(), "updated_at": utc_now(), "main_chain_write": False}
        document = _read_json(self.decisions_path, {})
        if not isinstance(document, Mapping):
            document = {}
        decisions = dict(document.get("decisions") or {}) if isinstance(document.get("decisions"), Mapping) else {}
        decisions[candidate_id] = record
        _atomic_json(self.decisions_path, {"schema_version": 1, "updated_at": utc_now(), "decisions": decisions})
        return {"status": "saved", **record, "selection_id": "topic-" + candidate_id, "collection_status": "archived_" + decision, "collection_route": "not_configured", "queue_enabled": False, "message": "决策已登记；旧队列未恢复，未触发采集或文案消费。"}

    def queue_archived(self) -> dict[str, Any]:
        return {"status": "archived", "message": "选题队列功能已归档，当前批次使用 selections 治理接口；未自动恢复旧队列。", "queue_enabled": False}


__all__ = ["CACHE_TTL", "REFRESH_INTERVALS", "SUPPORTED_CONTENT_PLATFORMS", "TopicCenterService", "default_runtime_root"]
