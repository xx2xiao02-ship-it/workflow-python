"""TikHub HTTP 传输与多平台内容归一化。

本模块只负责真实 HTTP 调用和可审计的字段归一化，不把搜索摘要冒充为
正文，也不把缺少鉴权/额度的响应伪造成成功。Token 只从环境变量读取，
不会写入返回值、日志或缓存。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://api.tikhub.dev"
DEFAULT_TIMEOUT = 25.0
DEFAULT_MAX_RETRIES = 1


class TikHubTransportError(RuntimeError):
    """TikHub 请求失败，包含不泄露凭据的机器可读错误信息。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "tikhub_error",
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


@dataclass(frozen=True)
class TikHubConfig:
    base_url: str = DEFAULT_BASE_URL
    token: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES

    @classmethod
    def from_env(cls) -> "TikHubConfig":
        raw_timeout = str(os.environ.get("TIKHUB_TIMEOUT_SECONDS") or "").strip()
        raw_retries = str(os.environ.get("TIKHUB_MAX_RETRIES") or "").strip()
        try:
            timeout = max(3.0, min(float(raw_timeout or DEFAULT_TIMEOUT), 120.0))
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        try:
            # 账号采集阶段只允许一次自动重试；更高次数会把一次任务
            # 变成不可控的计费循环。调用方仍可显式设置 0 关闭重试。
            retries = max(0, min(int(raw_retries or DEFAULT_MAX_RETRIES), 1))
        except ValueError:
            retries = DEFAULT_MAX_RETRIES
        return cls(
            base_url=str(os.environ.get("TIKHUB_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
            token=str(os.environ.get("TIKHUB_API_TOKEN") or "").strip(),
            timeout_seconds=timeout,
            max_retries=retries,
        )


def _first(value: Mapping[str, Any], keys: Sequence[str], default: Any = "") -> Any:
    for key in keys:
        candidate = value.get(key)
        if candidate is not None and str(candidate).strip() != "":
            return candidate
    return default


def _text(value: Any, limit: int = 500) -> str:
    if isinstance(value, Mapping):
        value = _first(value, ("name", "nickname", "title", "text", "value"), "")
    elif isinstance(value, (list, tuple)):
        value = next((item for item in value if item is not None and str(item).strip()), "")
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _content_preview(value: Any, limit: int = 360) -> str:
    """将接口明确返回的图文正文候选压缩为展示预览；不从标题或搜索摘要臆造正文。"""
    if isinstance(value, Mapping):
        value = _first(value, ("content", "content_html", "article_content", "answer_content", "body", "text", "value"), "")
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item or "") for item in value)
    text = str(value or "")
    text = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()[:limit]


_NESTED_RESULT_KEYS = (
    "business_data",
    "object",
    "target",
    "article",
    "answer",
    "question",
    "content",
    "data",
    "aweme_info",
    "item",
    "resource",
    "result",
    "highlight",
)


def _first_nested(value: Any, keys: Sequence[str], default: Any = "", *, max_depth: int = 3) -> Any:
    """读取 TikHub/平台常见的 data[*].object 等嵌套结果字段。"""
    if not isinstance(value, Mapping):
        return default
    found = _first(value, keys, None)
    if found is not None and str(found).strip() != "":
        return found
    if max_depth <= 0:
        return default
    for key in _NESTED_RESULT_KEYS:
        nested = value.get(key)
        if isinstance(nested, Mapping):
            found = _first_nested(nested, keys, None, max_depth=max_depth - 1)
            if found is not None and str(found).strip() != "":
                return found
    return default


def _first_nested_mapping(value: Any, keys: Sequence[str], *, max_depth: int = 3) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    if max_depth <= 0:
        return None
    for key in _NESTED_RESULT_KEYS:
        nested = value.get(key)
        if isinstance(nested, Mapping):
            found = _first_nested_mapping(nested, keys, max_depth=max_depth - 1)
            if found is not None:
                return found
    return None


def _iter_nested_mappings(value: Any, *, max_depth: int = 3) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    seen: set[int] = set()

    def visit(current: Any, depth: int) -> None:
        if not isinstance(current, Mapping) or id(current) in seen:
            return
        seen.add(id(current))
        result.append(current)
        if depth >= max_depth:
            return
        for nested in current.values():
            if isinstance(nested, Mapping):
                visit(nested, depth + 1)

    visit(value, 0)
    return result


def _number(value: Any) -> int | float | str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = _text(value, 80).replace(",", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _raise_for_api_error(value: Any) -> None:
    """把 HTTP 200 但业务失败的 TikHub 包装统一转成传输层错误。"""

    if not isinstance(value, Mapping):
        return
    raw_code = _first(value, ("code", "status_code", "statusCode"), None)
    try:
        code = int(raw_code) if raw_code not in (None, "") else None
    except (TypeError, ValueError):
        code = None
    success = value.get("success")
    message = _text(_first(value, ("message", "message_zh", "error", "detail"), "TikHub 业务请求失败"), 240)
    if code in {401, 403}:
        raise TikHubTransportError(
            f"TikHub HTTP {code}",
            code="auth_invalid" if code == 401 else "forbidden",
            http_status=code,
        )
    if code == 429:
        raise TikHubTransportError(message or "TikHub 请求频率受限", code="rate_limited", http_status=429, retryable=True)
    if code is not None and code >= 400:
        raise TikHubTransportError(message or f"TikHub 业务错误 {code}", code="api_error", http_status=code, retryable=code >= 500)
    if success is False:
        raise TikHubTransportError(message or "TikHub 业务请求失败", code="api_error")


SHORT_VIDEO_MAX_SECONDS = 60.0

_DURATION_KEYS = (
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
)


def _parse_duration_seconds(value: Any, *, key: str) -> float | None:
    """解析 TikHub 常见的秒、毫秒和 MM:SS 时长表示。"""
    if isinstance(value, Mapping):
        value = _first(
            value,
            ("seconds", "duration_seconds", "milliseconds", "duration_ms", "duration", "video_duration", "value", "text"),
            "",
        )
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = next((item for item in value if item is not None and str(item).strip()), "")
    text = _text(value, 80).replace(",", "").strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [float(part.strip()) for part in parts]
        except ValueError:
            numbers = []
        if len(numbers) == 2 and 0 <= numbers[0] <= 59 and 0 <= numbers[1] < 60:
            seconds = numbers[0] * 60 + numbers[1]
        elif len(numbers) == 3 and 0 <= numbers[0] and 0 <= numbers[1] < 60 and 0 <= numbers[2] < 60:
            seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
        else:
            return None
    else:
        unit_match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(ms|毫秒|s|sec|secs|秒|min|mins|分钟)?\s*", text, flags=re.IGNORECASE)
        if not unit_match:
            return None
        try:
            number = float(unit_match.group(1))
        except (TypeError, ValueError):
            return None
        unit = (unit_match.group(2) or "").lower()
        if unit in {"ms", "毫秒"}:
            seconds = number / 1000.0
        elif unit in {"min", "mins", "分钟"}:
            seconds = number * 60.0
        elif unit in {"s", "sec", "secs", "秒"}:
            seconds = number
        elif key.lower().endswith(("_ms", "_milliseconds")):
            seconds = number / 1000.0
        elif key.lower() in {"duration", "video_duration", "video_length"} and number >= 1000:
            # 抖音等接口的 duration 常以毫秒返回；超过 1000 的原始值按毫秒处理。
            seconds = number / 1000.0
        else:
            seconds = number
    if seconds < 0 or seconds > 24 * 60 * 60:
        return None
    return round(seconds, 3)


def _extract_duration_seconds(value: Any) -> tuple[float | None, str]:
    """从嵌套 TikHub 结果提取真实视频时长及其来源字段。"""
    mappings = _iter_nested_mappings(value)
    for key in _DURATION_KEYS:
        for mapping in mappings:
            for actual_key, candidate in mapping.items():
                if str(actual_key).lower() != key:
                    continue
                parsed = _parse_duration_seconds(candidate, key=key)
                if parsed is not None:
                    return parsed, str(actual_key)
    return None, ""


def filter_short_videos(contents: Sequence[Mapping[str, Any]], *, max_seconds: float = SHORT_VIDEO_MAX_SECONDS) -> tuple[list[dict[str, Any]], int]:
    """过滤已明确标注为不超过阈值的视频；未知时长保留并交给界面提示。"""
    kept: list[dict[str, Any]] = []
    removed = 0
    for item in contents:
        candidate = dict(item)
        if str(candidate.get("content_type") or "").lower() == "video":
            duration = candidate.get("duration_seconds")
            try:
                numeric_duration = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                numeric_duration = None
            if numeric_duration is not None and numeric_duration <= max_seconds:
                removed += 1
                continue
        kept.append(candidate)
    return kept, removed


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    """从不同资源的响应包中提取列表，不假设统一 data 包装。"""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        return []
    preferred = (
        "business_data",
        "items",
        "list",
        "results",
        "data_list",
        "aweme_list",
        "notes",
        "statuses",
        "articles",
        "video_list",
        "item_list",
    )
    for key in preferred:
        found = value.get(key)
        if isinstance(found, list):
            items = [item for item in found if isinstance(item, Mapping)]
            if items:
                return items
    for key in ("data", "result", "response"):
        nested = value.get(key)
        items = _mapping_items(nested)
        if items:
            return items
    # 兼容某些接口把结果按数字键包装的情况。
    for nested in value.values():
        if isinstance(nested, (Mapping, list)):
            items = _mapping_items(nested)
            if items:
                return items
    return []


def _tokens(keyword: str) -> list[str]:
    raw = str(keyword or "").strip().lower()
    result = re.findall(r"[\u3400-\u9fff]{2,}|[a-z0-9][a-z0-9+_.-]{1,}", raw)
    return list(dict.fromkeys(result))


def _canonical_url(value: Any) -> str:
    text = _text(value, 2000)
    return text if re.match(r"^https?://[^\s]+$", text, flags=re.IGNORECASE) else ""


def _normalize_item(item: Mapping[str, Any], *, platform: str, endpoint: str, keyword: str) -> dict[str, Any] | None:
    nested_mappings = _iter_nested_mappings(item)
    title = _text(
        _first_nested(
            item,
            ("title", "headline", "question_title", "article_title", "content_title", "name", "desc", "description", "text", "excerpt"),
            "",
        ),
        800,
    )
    url = _canonical_url(
        _first_nested(
            item,
            ("url", "share_url", "article_url", "note_url", "link", "aweme_url", "mobile_url", "content_url"),
            "",
        )
    )
    item_id = _text(
        _first_nested(item, ("id", "aweme_id", "note_id", "article_id", "mid", "object_id", "content_id"), ""),
        160,
    )
    # 只有 ID 的结果不可供编辑或追溯，不能把 ID 冒充成标题。
    if not title and not url:
        return None
    if not title:
        title = "TikHub未返回标题"
    author = _text(
        _first_nested(item, ("author_name", "nickname", "user_name", "screen_name", "display_name"), ""),
        200,
    )
    if not author:
        for nested_author in _iter_nested_mappings(item):
            author = _text(_first(nested_author, ("name", "nickname", "screen_name", "display_name"), ""), 200)
            if author:
                break
    metrics: dict[str, int | float | str] = {}
    metric_containers = {"statistics", "statistic", "stats", "interaction_info", "interactions", "engagement"}
    nested_metrics = [
        nested
        for nested in nested_mappings
        if any(str(key).lower() in metric_containers for key in nested.keys())
    ]
    nested_metrics.extend(
        value
        for nested in nested_mappings
        for key, value in nested.items()
        if str(key).lower() in metric_containers and isinstance(value, Mapping)
    )
    metric_keys = {
        "views": ("views", "view_count", "play_count", "video_view_count"),
        "likes": ("likes", "like_count", "liked_count", "digg_count", "attitudes_count", "voteup_count"),
        "comments": ("comments", "comment_count", "comments_count"),
        "shares": ("shares", "share_count", "share_count"),
        "collects": ("collects", "collect_count", "收藏数"),
        "reposts": ("reposts", "repost_count", "reposts_count"),
        "reads": ("read_count", "reads", "阅读数"),
    }
    for name, keys in metric_keys.items():
        number = _number(_first_nested(item, keys, None))
        if number is None:
            for nested in nested_metrics:
                number = _number(_first(nested, keys, None))
                if number is not None:
                    break
        if number is not None:
            metrics[name] = number
    published_at = _text(
        _first_nested(
            item,
            ("published_at", "publish_time", "created_time", "create_time", "updated_time", "updated_at", "ctime", "date", "timestamp"),
            "",
        ),
        80,
    )
    native_score = _number(_first_nested(item, ("hot_value", "hot_score", "score", "heat", "rank", "position"), None))
    type_values = {
        _text(_first(mapping, ("content_type", "media_type", "type"), ""), 40).lower()
        for mapping in nested_mappings
    }
    has_video_field = any(
        key in mapping
        for mapping in nested_mappings
        for key in (
            "video_url",
            "video_play_url",
            "aweme_id",
            "duration",
            "duration_seconds",
            "duration_ms",
            "video_duration",
            "video_duration_seconds",
            "video_duration_ms",
            "video_length",
            "video",
        )
    )
    content_type = "video" if has_video_field or type_values.intersection({"video", "zvideo", "short_video"}) else "article"
    duration_seconds, duration_source = _extract_duration_seconds(item) if content_type == "video" else (None, "")
    content_preview = ""
    if content_type == "article":
        content_preview = _content_preview(
            _first_nested(item, ("content", "content_html", "article_content", "answer_content", "body"), "")
        )
    tokens = _tokens(keyword)
    lowered = title.lower()
    matched = [token for token in tokens if token in lowered]
    relevance = round(len(matched) / len(tokens), 3) if tokens else 0.0
    return {
        "content_id": f"tikhub-{platform}-{item_id}" if item_id else "",
        "provider": "TikHub",
        "platform": platform,
        "content_type": content_type,
        "duration_seconds": duration_seconds,
        "duration_source": duration_source,
        "content_preview": content_preview,
        "content_preview_source": "search_response" if content_preview else "",
        "title": title,
        "url": url,
        "author": author,
        "published_at": published_at,
        "metrics": metrics,
        "native_score": native_score,
        "relevance_score": relevance,
        "matched_terms": matched,
        "source_metadata": {
            "provider": "TikHub",
            "endpoint": endpoint,
            "platform": platform,
            "item_id": item_id,
            "keyword": keyword,
        },
    }


class TikHubTransport:
    """TikHub官方HTTP API的最小、可注入测试传输层。"""

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

    def __init__(
        self,
        config: TikHubConfig | None = None,
        *,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or TikHubConfig.from_env()
        self._opener = opener
        self._sleeper = sleeper

    @classmethod
    def from_env(cls) -> "TikHubTransport":
        return cls(TikHubConfig.from_env())

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        """公开的 JSON 请求边界，供项目内适配器注入/替换。

        认证、重试和响应校验仍集中在本传输层；适配器不应自行拼接
        Authorization，也不应复制第三方 SDK。
        """

        return self._request_json(method, path, params=params, body=body)

    def health(self) -> dict[str, Any]:
        """只检查配置，不发起计费请求。"""
        configured = bool(self.config.token)
        return {
            "provider": "TikHub",
            "service": "tikhub",
            "status": "ready" if configured else "blocked",
            "auth_configured": configured,
            "base_url": self.config.base_url,
            "message": "TikHub Token 已配置，可按需查询。" if configured else "未配置 TIKHUB_API_TOKEN，暂不发起外部请求。",
            "paid_call": False,
        }

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        if not self.config.token:
            raise TikHubTransportError(
                "未配置 TIKHUB_API_TOKEN",
                code="auth_missing",
                http_status=401,
            )
        url = self.config.base_url + "/" + path.lstrip("/")
        if params:
            clean_params = {str(key): value for key, value in params.items() if value is not None and value != ""}
            if clean_params:
                url += "?" + urlencode(clean_params, doseq=True)
        raw_body = json.dumps(dict(body), ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/json",
            "User-Agent": "TopicIntelligenceCenter/1.0",
            "Authorization": f"Bearer {self.config.token}",
        }
        if raw_body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=raw_body, headers=headers, method=method.upper())
        last_error: TikHubTransportError | None = None
        max_retries = max(0, min(int(self.config.max_retries or 0), 1))
        retry_after_seconds = 0.0
        for attempt in range(max_retries + 1):
            try:
                with self._opener(request, timeout=self.config.timeout_seconds) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    value = json.loads(raw) if raw else {}
                    if not isinstance(value, (Mapping, list)):
                        raise TikHubTransportError("TikHub 返回不是 JSON 对象或数组", code="invalid_json")
                    _raise_for_api_error(value)
                    return value
            except HTTPError as exc:
                status = int(exc.code or 0)
                if status in {401, 403}:
                    code = "auth_invalid" if status == 401 else "forbidden"
                    raise TikHubTransportError(
                        f"TikHub HTTP {status}", code=code, http_status=status
                    ) from exc
                retryable = status == 429 or status >= 500
                last_error = TikHubTransportError(
                    f"TikHub HTTP {status}",
                    code="rate_limited" if status == 429 else "upstream_error",
                    http_status=status,
                    retryable=retryable,
                )
                # 只读取服务端建议的等待时间，不把响应正文或 Token 写入
                # 日志/快照。异常对象没有稳定的 headers 接口时回退到短等待。
                retry_after_seconds = 0.0
                if retryable:
                    try:
                        raw_retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                        retry_after_seconds = max(0.0, min(float(str(raw_retry_after).strip()), 30.0))
                    except (TypeError, ValueError):
                        retry_after_seconds = 0.0
            except (URLError, TimeoutError, OSError) as exc:
                last_error = TikHubTransportError(
                    f"TikHub 连接失败：{type(exc).__name__}",
                    code="network_error",
                    retryable=True,
                )
            except json.JSONDecodeError as exc:
                raise TikHubTransportError("TikHub 返回无法解析为 JSON", code="invalid_json") from exc
            if last_error is None or not last_error.retryable or attempt >= max_retries:
                break
            self._sleeper(retry_after_seconds or min(1.5 * (attempt + 1), 4.0))
        if last_error is not None:
            raise last_error
        raise TikHubTransportError("TikHub 请求失败", code="request_failed")

    def search_related(
        self,
        keyword: str,
        *,
        platforms: Sequence[str] = ("douyin", "xiaohongshu", "weibo", "zhihu", "wechat"),
        limit: int = 20,
        wechat_ghid: str = "",
    ) -> dict[str, Any]:
        clean_keyword = _text(keyword, 200)
        if not clean_keyword:
            return {"status": "invalid_input", "contents": [], "queries": [], "message": "keyword不能为空"}
        limit = max(1, min(int(limit), 50))
        selected = list(dict.fromkeys(str(item or "").strip().lower() for item in platforms if str(item or "").strip()))
        contents: list[dict[str, Any]] = []
        queries: list[dict[str, Any]] = []
        filtered_short_videos = 0
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
                request_kwargs = {
                    "body": {
                        "keyword": clean_keyword,
                        "cursor": 0,
                        # TikHub 官方示例将这些枚举参数作为字符串传递；
                        # cursor 保持数字类型。部分网关对数字枚举返回 422。
                        "sort_type": "0",
                        "publish_time": "0",
                        "filter_duration": "0",
                        "content_type": "0",
                        "search_id": "",
                        "backtrace": "",
                    }
                }
            elif platform == "xiaohongshu":
                request_kwargs = {"params": {"keywords": clean_keyword, "page": 1, "sort_type": "general", "note_type": 0}}
            elif platform == "weibo":
                request_kwargs = {"params": {"query": clean_keyword, "mode": "hot", "page": 1}}
            elif platform == "zhihu":
                request_kwargs = {"params": {"keyword": clean_keyword, "offset": 0, "limit": limit}}
            else:
                request_kwargs = {"params": {"ghid": wechat_ghid, "offset": 0}}
            try:
                response = self._request_json(method, path, **request_kwargs)
                raw_items = _mapping_items(response)
                normalized = [
                    item
                    for item in (
                        _normalize_item(raw, platform=platform, endpoint=path, keyword=clean_keyword)
                        for raw in raw_items[:limit]
                    )
                    if item is not None
                ]
                received_count = len(normalized)
                normalized, short_count = filter_short_videos(normalized)
                filtered_short_videos += short_count
                contents.extend(normalized)
                queries.append({
                    "platform": platform,
                    "status": "ready",
                    "endpoint": path,
                    "received_count": received_count,
                    "count": len(normalized),
                    "filtered_short_videos": short_count,
                })
            except TikHubTransportError as exc:
                queries.append({
                    "platform": platform,
                    "status": "blocked" if exc.code in {"auth_missing", "auth_invalid", "forbidden"} else "failed",
                    "endpoint": path,
                    "error_code": exc.code,
                    "http_status": exc.http_status,
                    "message": str(exc),
                })
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in contents:
            identity = str(item.get("url") or item.get("content_id") or "").strip()
            if not identity or identity in seen:
                continue
            seen.add(identity)
            deduped.append(item)
        ready_count = sum(1 for query in queries if query.get("status") == "ready")
        failed_count = sum(1 for query in queries if query.get("status") in {"blocked", "failed"})
        status = "ready" if deduped else ("partial" if ready_count else "blocked")
        return {
            "status": status,
            "keyword": clean_keyword,
            "contents": deduped,
            "queries": queries,
            "ready_platforms": ready_count,
            "failed_platforms": failed_count,
            "filtered_short_videos": filtered_short_videos,
            "message": (
                f"已从TikHub读取 {len(deduped)} 条相关内容。"
                + (f" 已过滤 {filtered_short_videos} 条不超过1分钟的视频。" if filtered_short_videos else "")
                if deduped
                else (
                    f"TikHub返回的内容均不满足时长要求，已过滤 {filtered_short_videos} 条不超过1分钟的视频。"
                    if filtered_short_videos
                    else "TikHub没有返回可展示的相关内容；请检查Token、额度、平台参数或稍后重试。"
                )
            ),
        }

    def fetch_zhihu_detail(self, source_url: str) -> dict[str, Any]:
        """按搜索结果的真实知乎 URL 获取正文详情；不会把搜索摘要当正文。"""
        parsed = urlparse(str(source_url or "").strip())
        path = parsed.path.rstrip("/")
        match = re.search(r"/(answers|articles|questions)/(\d+)$", path, re.I)
        if not match:
            return {"status": "invalid_input", "content": "", "message": "知乎来源缺少可识别的内容ID"}
        kind, item_id = match.group(1).lower(), match.group(2)
        detail_key = {"answers": "zhihu_answer", "articles": "zhihu_article", "questions": "zhihu_question"}[kind]
        method, endpoint, parameter = self.DETAIL_ENDPOINTS[detail_key]
        response = self._request_json(method, endpoint, params={parameter: item_id})
        data = response.get("data") if isinstance(response, Mapping) else response
        if not isinstance(data, Mapping):
            return {"status": "content_missing", "content": "", "message": "知乎详情接口未返回正文数据", "endpoint": endpoint}

        def text_value(value: Any) -> str:
            if not isinstance(value, str):
                return ""
            # TikHub 返回的知乎正文通常为 HTML；只将正文节点转成纯文本。
            value = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
            value = re.sub(r"<[^>]+>", " ", value)
            return re.sub(r"\s+", " ", value).strip()

        candidates: list[tuple[str, str]] = []
        def walk(node: Any, key: str = "") -> None:
            if isinstance(node, Mapping):
                for child_key, child in node.items():
                    walk(child, str(child_key).lower())
            elif isinstance(node, list):
                for child in node:
                    walk(child, key)
            elif key in {"content", "content_html", "html", "body", "answer_content", "article_content"}:
                value = text_value(node)
                if value:
                    candidates.append((key, value))

        walk(data)
        content = max((value for _key, value in candidates), key=len, default="")
        title = ""
        for key in ("title", "name"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                title = re.sub(r"\s+", " ", value).strip()
                break
        return {
            "status": "ready" if len(content) >= 80 else "content_missing",
            "content": content if len(content) >= 80 else "",
            "title": title,
            "content_type": "zhihu_detail",
            "detail_key": detail_key,
            "endpoint": endpoint,
            "item_id": item_id,
            "message": "已通过TikHub知乎详情接口取得正文" if len(content) >= 80 else "知乎详情接口未返回足够正文",
        }


__all__ = ["DEFAULT_BASE_URL", "TikHubConfig", "TikHubTransport", "TikHubTransportError"]
