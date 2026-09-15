"""TikHub transport used by the migrated topic-center boundary.

The transport is deliberately unaware of files and environment variables.
It receives endpoint, timeout, retry policy, and the credential through the
registered control-plane ConfigService.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .config_service import ConfigService


class TikHubTransportError(RuntimeError):
    def __init__(self, message: str, *, code: str = "tikhub_error", http_status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


def _text(value: Any, limit: int = 500) -> str:
    if isinstance(value, Mapping):
        value = next((value.get(key) for key in ("name", "nickname", "title", "text", "value") if value.get(key) not in (None, "")), "")
    elif isinstance(value, (list, tuple)):
        value = next((item for item in value if item not in (None, "")), "")
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _number(value: Any) -> int | float | str | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return str(value).strip() or None
    return int(number) if number.is_integer() else number


def _iter_nested(value: Any, depth: int = 0) -> list[Mapping[str, Any]]:
    if depth > 4:
        return []
    result: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        result.append(value)
        for child in value.values():
            result.extend(_iter_nested(child, depth + 1))
    elif isinstance(value, list):
        for child in value[:100]:
            result.extend(_iter_nested(child, depth + 1))
    return result


def _first_nested(value: Any, keys: Sequence[str]) -> Any:
    wanted = {str(key).lower() for key in keys}
    for mapping in _iter_nested(value):
        for key, child in mapping.items():
            if str(key).lower() in wanted and child not in (None, "", [], {}):
                return child
    return ""


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in ("data", "items", "list", "results", "videos", "notes", "articles"):
            child = value.get(key)
            if isinstance(child, list):
                return [item for item in child if isinstance(item, Mapping)]
            if isinstance(child, Mapping):
                nested = _mapping_items(child)
                if nested:
                    return nested
    return []


def _raise_for_api_error(value: Any) -> None:
    if not isinstance(value, Mapping):
        return
    code = value.get("code", value.get("status_code", value.get("error_code")))
    message = value.get("message", value.get("msg", value.get("error")))
    if isinstance(code, (int, float)) and int(code) not in {0, 200}:
        raise TikHubTransportError(f"TikHub 返回错误 {int(code)}", code="upstream_rejected")
    if isinstance(code, str) and code.strip().lower() in {"error", "failed", "failure"}:
        raise TikHubTransportError("TikHub 返回错误", code="upstream_rejected")
    if isinstance(message, str) and message.strip().lower() in {"error", "failed", "failure"}:
        raise TikHubTransportError("TikHub 返回错误", code="upstream_rejected")


def _parse_duration_seconds(value: Any, *, key: str) -> float | None:
    """解析秒、毫秒、分钟和 MM:SS，未知格式保持未知而不误删。"""

    if isinstance(value, Mapping):
        value = next(
            (
                value.get(name)
                for name in (
                    "seconds",
                    "duration_seconds",
                    "milliseconds",
                    "duration_ms",
                    "duration",
                    "video_duration",
                    "value",
                    "text",
                )
                if value.get(name) not in (None, "")
            ),
            "",
        )
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (list, tuple)):
        value = next((item for item in value if item not in (None, "")), "")
    text = _text(value, 80).replace(",", "").strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [float(part.strip()) for part in parts]
        except (TypeError, ValueError):
            return None
        if len(numbers) == 2 and 0 <= numbers[0] <= 59 and 0 <= numbers[1] < 60:
            seconds = numbers[0] * 60 + numbers[1]
        elif len(numbers) == 3 and 0 <= numbers[0] and 0 <= numbers[1] < 60 and 0 <= numbers[2] < 60:
            seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
        else:
            return None
    else:
        match = re.fullmatch(
            r"\s*(-?\d+(?:\.\d+)?)\s*(ms|毫秒|s|sec|secs|秒|min|mins|分钟)?\s*",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        try:
            number = float(match.group(1))
        except (TypeError, ValueError):
            return None
        unit = (match.group(2) or "").lower()
        if unit in {"ms", "毫秒"}:
            seconds = number / 1000.0
        elif unit in {"min", "mins", "分钟"}:
            seconds = number * 60.0
        elif unit in {"s", "sec", "secs", "秒"}:
            seconds = number
        elif key.lower().endswith(("_ms", "_milliseconds")):
            seconds = number / 1000.0
        elif key.lower() in {"duration", "video_duration", "video_length"} and number >= 1000:
            # TikHub 的通用 duration 字段常以毫秒返回；明确的
            # duration_seconds 字段不走这个分支。
            seconds = number / 1000.0
        else:
            seconds = number
    if not math.isfinite(seconds) or seconds < 0 or seconds > 24 * 60 * 60:
        return None
    return round(seconds, 3)


def _extract_duration(value: Any) -> tuple[float | None, str]:
    for mapping in _iter_nested(value):
        for key in (
            "duration_seconds",
            "duration_secs",
            "duration_sec",
            "video_duration_seconds",
            "video_duration_sec",
            "duration_ms",
            "duration_milliseconds",
            "video_duration_ms",
            "video_length_ms",
            "duration",
            "video_duration",
            "video_length",
            "duration_text",
            "video_duration_text",
        ):
            for actual_key, raw_value in mapping.items():
                if str(actual_key).lower() != key:
                    continue
                parsed = _parse_duration_seconds(raw_value, key=key)
                if parsed is not None:
                    return parsed, str(actual_key)
    return None, ""


def filter_short_videos(contents: Sequence[Mapping[str, Any]], *, max_seconds: float = 60.0) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    removed = 0
    for item in contents:
        row = dict(item)
        if str(row.get("content_type") or "").lower() != "video":
            kept.append(row)
            continue
        duration = row.get("duration_seconds")
        try:
            numeric_duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            numeric_duration = None
        if numeric_duration is not None and math.isfinite(numeric_duration) and numeric_duration <= max_seconds:
            removed += 1
            continue
        kept.append(row)
    return kept, removed


def _normalize_item(item: Mapping[str, Any], *, platform: str, endpoint: str, keyword: str) -> dict[str, Any] | None:
    title = _text(_first_nested(item, ("title", "headline", "question_title", "article_title", "content_title", "name", "desc", "description", "text", "excerpt")), 800)
    url = _text(_first_nested(item, ("url", "share_url", "article_url", "note_url", "link", "aweme_url", "mobile_url", "content_url")), 2_000)
    if not re.match(r"^https?://[^\s]+$", url, re.I):
        url = ""
    item_id = _text(_first_nested(item, ("id", "aweme_id", "note_id", "article_id", "mid", "object_id", "content_id")), 160)
    if not title and not url:
        return None
    title = title or "TikHub未返回标题"
    author = _text(_first_nested(item, ("author_name", "nickname", "user_name", "screen_name", "display_name")), 200)
    metrics: dict[str, int | float | str] = {}
    metric_keys = {
        "views": ("views", "view_count", "play_count", "video_view_count"),
        "likes": ("likes", "like_count", "liked_count", "digg_count", "attitudes_count", "voteup_count"),
        "comments": ("comments", "comment_count", "comments_count"),
        "shares": ("shares", "share_count", "repost_count"),
        "collects": ("collects", "collect_count", "收藏数"),
        "reads": ("read_count", "reads", "阅读数"),
    }
    for name, keys in metric_keys.items():
        number = _number(_first_nested(item, keys))
        if number is not None:
            metrics[name] = number
    type_values = {_text(_first_nested(mapping, ("content_type", "media_type", "type")), 40).lower() for mapping in _iter_nested(item)}
    content_type = "video" if type_values.intersection({"video", "zvideo", "short_video"}) or any(key in mapping for mapping in _iter_nested(item) for key in ("video_url", "video_play_url", "aweme_id", "duration", "duration_seconds", "video_duration")) else "article"
    duration_seconds, duration_source = _extract_duration(item) if content_type == "video" else (None, "")
    preview = _text(_first_nested(item, ("content", "content_html", "article_content", "answer_content", "body")), 360) if content_type == "article" else ""
    tokens = re.findall(r"[\u3400-\u9fff]{2,}|[a-z0-9][a-z0-9+_.-]{1,}", str(keyword or "").lower())
    matched = list(dict.fromkeys(token for token in tokens if token in title.lower()))
    return {
        "content_id": f"tikhub-{platform}-{item_id}" if item_id else "",
        "provider": "TikHub",
        "platform": platform,
        "content_type": content_type,
        "duration_seconds": duration_seconds,
        "duration_source": duration_source,
        "content_preview": preview,
        "content_preview_source": "search_response" if preview else "",
        "title": title,
        "url": url,
        "author": author,
        "published_at": _text(_first_nested(item, ("published_at", "publish_time", "created_time", "create_time", "updated_at", "date", "timestamp")), 80),
        "metrics": metrics,
        "native_score": _number(_first_nested(item, ("hot_value", "hot_score", "score", "heat", "rank", "position"))),
        "relevance_score": round(len(matched) / len(tokens), 3) if tokens else 0.0,
        "matched_terms": matched,
        "source_metadata": {"provider": "TikHub", "endpoint": endpoint, "platform": platform, "item_id": item_id, "keyword": keyword},
    }


class TikHubTransport:
    ENDPOINTS = {
        "douyin": ("POST", "/api/v1/douyin/search/fetch_video_search_v2"),
        "xiaohongshu": ("GET", "/api/v1/xiaohongshu/web_v2/fetch_search_notes"),
        "weibo": ("GET", "/api/v1/weibo/web_v2/fetch_video_search"),
        "zhihu": ("GET", "/api/v1/zhihu/web/fetch_article_search_v3"),
        "wechat": ("GET", "/api/v1/wechat_mp/web/fetch_mp_article_list"),
    }
    DETAIL_ENDPOINTS = {
        "zhihu_answer": ("GET", "/api/v1/zhihu/web/fetch_answer_detail", "answer_id"),
        "zhihu_article": ("GET", "/api/v1/zhihu/web/fetch_column_article_detail", "article_id"),
        "zhihu_question": ("GET", "/api/v1/zhihu/web/fetch_question_detail", "question_id"),
    }

    def __init__(self, config_service: ConfigService, *, opener: Callable[..., Any] = urlopen, sleeper: Callable[[float], None] = time.sleep) -> None:
        self.config_service = config_service
        self._opener = opener
        self._sleeper = sleeper

    def health(self) -> dict[str, Any]:
        snapshot = self.config_service.public_snapshot()
        group = next(item for item in snapshot["groups"] if item["group_id"] == "tikhub")
        configured = group["credential_status"] == "configured"
        return {
            "provider": "TikHub",
            "service": "tikhub",
            "status": "configured_not_verified" if configured else "not_configured",
            "auth_configured": configured,
            "connection_status": group["connection_status"],
            "base_url": group["settings"]["endpoint"],
            "timeout_seconds": group["settings"]["timeout_seconds"],
            "max_retries": group["settings"]["max_retries"],
            "message": "已配置，尚未执行供应商连接验证。" if configured else "未配置 TikHub 凭据，暂不发起外部请求。",
            "paid_call": False,
        }

    def request_json(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, body: Mapping[str, Any] | Sequence[Any] | None = None) -> dict[str, Any] | list[Any]:
        cfg = self.config_service.get_tikhub_transport_config()
        if not cfg.token:
            raise TikHubTransportError("未配置 TikHub 主 API Key", code="auth_missing", http_status=401)
        url = cfg.endpoint + "/" + str(path).lstrip("/")
        if params:
            clean = {str(key): value for key, value in params.items() if value is not None and value != ""}
            if clean:
                url += "?" + urlencode(clean, doseq=True)
        raw_body: bytes | None = None
        if body is not None:
            if isinstance(body, Mapping):
                raw_body = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
            elif isinstance(body, Sequence) and not isinstance(body, (str, bytes, bytearray)):
                raw_body = json.dumps(list(body), ensure_ascii=False).encode("utf-8")
            else:
                raise TikHubTransportError("TikHub 请求体必须是对象或数组", code="invalid_request_body")
        request = Request(url, data=raw_body, method=str(method).upper(), headers={"Accept": "application/json", "User-Agent": "TopicCenterMigration/1.0", "Authorization": f"Bearer {cfg.token}", **({"Content-Type": "application/json"} if raw_body is not None else {})})
        last_error: TikHubTransportError | None = None
        for attempt in range(cfg.max_retries + 1):
            try:
                with self._opener(request, timeout=cfg.timeout_seconds) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    value = json.loads(raw) if raw else {}
                    if not isinstance(value, (Mapping, list)):
                        raise TikHubTransportError("TikHub 返回不是 JSON 对象或数组", code="invalid_json")
                    _raise_for_api_error(value)
                    return value
            except HTTPError as exc:
                status = int(exc.code or 0)
                if status in {401, 403}:
                    raise TikHubTransportError(f"TikHub HTTP {status}", code="auth_invalid" if status == 401 else "forbidden", http_status=status) from exc
                retryable = status == 429 or status >= 500
                last_error = TikHubTransportError(f"TikHub HTTP {status}", code="rate_limited" if status == 429 else "upstream_error", http_status=status, retryable=retryable)
            except (URLError, TimeoutError, OSError) as exc:
                last_error = TikHubTransportError(f"TikHub 连接失败：{type(exc).__name__}", code="network_error", retryable=True)
            except json.JSONDecodeError as exc:
                raise TikHubTransportError("TikHub 返回无法解析为 JSON", code="invalid_json") from exc
            if last_error is None or not last_error.retryable or attempt >= cfg.max_retries:
                break
            self._sleeper(min(1.5 * (attempt + 1), 4.0))
        raise last_error or TikHubTransportError("TikHub 请求失败", code="request_failed")

    def search_related(self, keyword: str, *, platforms: Sequence[str] = ("douyin", "xiaohongshu", "weibo", "zhihu", "wechat"), limit: int = 20, wechat_ghid: str = "") -> dict[str, Any]:
        clean_keyword = _text(keyword, 200)
        if not clean_keyword:
            return {"status": "invalid_input", "contents": [], "queries": [], "message": "keyword不能为空"}
        limit = max(1, min(int(limit), 50))
        selected = list(dict.fromkeys(str(item or "").strip().lower() for item in platforms if str(item or "").strip()))
        contents: list[dict[str, Any]] = []
        queries: list[dict[str, Any]] = []
        filtered_short = 0
        for platform in selected:
            endpoint_spec = self.ENDPOINTS.get(platform)
            if endpoint_spec is None:
                queries.append({"platform": platform, "status": "unsupported", "message": "平台未配置"})
                continue
            method, path = endpoint_spec
            if platform == "wechat" and not wechat_ghid:
                queries.append({"platform": platform, "status": "needs_account", "message": "公众号文章列表需要ghid"})
                continue
            if platform == "douyin":
                kwargs = {"body": {"keyword": clean_keyword, "cursor": 0, "sort_type": "0", "publish_time": "0", "filter_duration": "0", "content_type": "0", "search_id": "", "backtrace": ""}}
            elif platform == "xiaohongshu":
                kwargs = {"params": {"keywords": clean_keyword, "page": 1, "sort_type": "general", "note_type": 0}}
            elif platform == "weibo":
                kwargs = {"params": {"query": clean_keyword, "mode": "hot", "page": 1}}
            elif platform == "zhihu":
                kwargs = {"params": {"keyword": clean_keyword, "offset": 0, "limit": limit}}
            else:
                kwargs = {"params": {"ghid": wechat_ghid, "offset": 0}}
            try:
                response = self.request_json(method, path, **kwargs)
                normalized = [item for item in (_normalize_item(raw, platform=platform, endpoint=path, keyword=clean_keyword) for raw in _mapping_items(response)[:limit]) if item is not None]
                received = len(normalized)
                normalized, short_count = filter_short_videos(normalized)
                filtered_short += short_count
                contents.extend(normalized)
                queries.append({"platform": platform, "status": "ready", "endpoint": path, "received_count": received, "count": len(normalized), "filtered_short_videos": short_count})
            except TikHubTransportError as exc:
                queries.append({"platform": platform, "status": "blocked" if exc.code in {"auth_missing", "auth_invalid", "forbidden"} else "failed", "endpoint": path, "error_code": exc.code, "http_status": exc.http_status, "message": str(exc)})
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in contents:
            key = str(item.get("content_id") or item.get("url") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            deduped.append(item)
        return {"status": "ready" if deduped else "partial" if queries else "empty", "contents": deduped, "queries": queries, "filtered_short_videos": filtered_short, "message": "TikHub 已返回可展示内容。" if deduped else "TikHub 没有返回可展示内容；请检查凭据、额度、平台参数或供应商状态。"}

    def fetch_zhihu_detail(self, source_url: str) -> dict[str, Any]:
        parsed = urlparse(str(source_url or "").strip())
        match = re.search(r"/(answers|articles|questions)/(\d+)$", parsed.path.rstrip("/"), re.I)
        if not match:
            return {"status": "invalid_input", "content": "", "message": "知乎来源缺少可识别的内容ID"}
        kind, item_id = match.group(1).lower(), match.group(2)
        detail_key = {"answers": "zhihu_answer", "articles": "zhihu_article", "questions": "zhihu_question"}[kind]
        method, endpoint, parameter = self.DETAIL_ENDPOINTS[detail_key]
        response = self.request_json(method, endpoint, params={parameter: item_id})
        data = response.get("data") if isinstance(response, Mapping) else response
        candidates: list[str] = []
        def walk(node: Any, key: str = "") -> None:
            if isinstance(node, Mapping):
                for child_key, child in node.items():
                    walk(child, str(child_key).lower())
            elif isinstance(node, list):
                for child in node:
                    walk(child, key)
            elif key in {"content", "content_html", "html", "body", "answer_content", "article_content"} and isinstance(node, str):
                text = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>|<[^>]+>", " ", node, flags=re.I | re.S)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    candidates.append(text)
        walk(data)
        content = max(candidates, key=len, default="")
        title = str(data.get("title") or data.get("name") or "").strip() if isinstance(data, Mapping) else ""
        ready = len(content) >= 80
        return {"status": "ready" if ready else "content_missing", "content": content if ready else "", "title": title, "content_type": "zhihu_detail", "detail_key": detail_key, "endpoint": endpoint, "item_id": item_id, "message": "已通过 TikHub 详情接口取得正文" if ready else "知乎详情接口未返回足够正文"}


__all__ = ["TikHubTransport", "TikHubTransportError", "filter_short_videos"]
