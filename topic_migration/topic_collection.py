"""选题正文采集边界。

只对公开 HTML 和已登记的知乎详情 Transport 做实际读取。抖音、B站、
小红书、微博、公众号等需要额外登录态或专用采集器的路径在本批明确阻断，
不会用标题、摘要或合成文本冒充正文。
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .tikhub_transport import TikHubTransport, TikHubTransportError

MIN_SOURCE_TEXT_CHARS = 80
MAX_HTML_BYTES = 2_000_000
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "TopicCenterMigration/1.0",
}
SOCIAL_PLATFORMS = frozenset({"douyin", "bilibili", "xiaohongshu", "weibo", "wechat"})


class TopicCollectionError(RuntimeError):
    """正文采集失败，但不暴露响应内容或凭据。"""


def utc_now() -> str:
    from .contracts import utc_now as _utc_now

    return _utc_now()


def detect_source_platform(source_url: str) -> str:
    host = urlparse(str(source_url or "").strip()).netloc.lower().removeprefix("www.")
    if host == "zhihu.com" or host.endswith(".zhihu.com"):
        return "zhihu"
    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        return "bilibili"
    if host in {"douyin.com", "v.douyin.com", "iesdouyin.com"} or host.endswith(".douyin.com"):
        return "douyin"
    if host in {"xiaohongshu.com", "xhslink.com"} or host.endswith(".xiaohongshu.com"):
        return "xiaohongshu"
    if host == "weibo.com" or host.endswith(".weibo.com"):
        return "weibo"
    if host == "mp.weixin.qq.com" or host.endswith(".weixin.qq.com"):
        return "wechat"
    return "article"


class _VisibleTextParser(HTMLParser):
    _ignored = {"script", "style", "noscript", "svg", "template"}
    _article_hints = (
        "articlebody",
        "article-body",
        "article_body",
        "article-content",
        "article_content",
        "content-body",
        "content_body",
        "post-content",
        "post_content",
        "entry-content",
        "entry_content",
        "rich-text",
        "rich_text",
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.article_regions: list[list[str]] = []
        self._ignored_depth = 0
        self._title_depth = 0
        self._article_depth = 0
        self._article_parts: list[str] = []
        self._hint_depth = 0
        self._hint_parts: list[str] = []

    @classmethod
    def _is_hint(cls, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag.lower() == "article":
            return True
        classes = " ".join(str(value or "") for key, value in attrs if key.lower() in {"class", "id"}).lower()
        normalized = re.sub(r"[^a-z0-9_-]+", " ", classes)
        return any(hint in normalized for hint in cls._article_hints)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._ignored:
            self._ignored_depth += 1
        if tag == "title":
            self._title_depth += 1
        if self._article_depth:
            self._article_depth += 1
        if self._hint_depth:
            self._hint_depth += 1
        if tag == "article" and not self._article_depth:
            self._article_parts = []
            self._article_depth = 1
        elif self._is_hint(tag, attrs) and not self._hint_depth and tag != "article":
            self._hint_parts = []
            self._hint_depth = 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if self._hint_depth:
            self._hint_depth -= 1
            if not self._hint_depth and self._hint_parts:
                self.article_regions.append(self._hint_parts)
                self._hint_parts = []
        if self._article_depth:
            self._article_depth -= 1
            if not self._article_depth and self._article_parts:
                self.article_regions.append(self._article_parts)
                self._article_parts = []
        if tag in self._ignored and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not data.strip():
            return
        value = html.unescape(data).strip()
        if not value:
            return
        if self._title_depth:
            self.title_parts.append(value)
        self.text_parts.append(value)
        if self._article_depth:
            self._article_parts.append(value)
        if self._hint_depth:
            self._hint_parts.append(value)


def _clean_text(values: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(values)).strip()


def _fetch_html(source_url: str, *, timeout: float, opener: Callable[..., Any]) -> tuple[int, str, bytes]:
    request = Request(source_url, headers=DEFAULT_HEADERS)
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(MAX_HTML_BYTES + 1)
            if len(raw) > MAX_HTML_BYTES:
                raise TopicCollectionError("来源 HTML 超过 2 MB，未继续解析")
            final_url = str(getattr(response, "geturl", lambda: source_url)() or source_url)
            status = int(getattr(response, "status", 200) or 200)
            return status, final_url, raw
    except HTTPError as exc:
        raise TopicCollectionError(f"HTTP {int(exc.code)}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise TopicCollectionError(f"连接失败：{type(exc).__name__}") from exc


def _base(selection: Mapping[str, Any], platform: str) -> dict[str, Any]:
    return {
        "selection_id": str(selection.get("selection_id") or "").strip(),
        "source_url": str(selection.get("source_url") or "").strip(),
        "platform": platform,
        "title": str(selection.get("title") or "").strip(),
        "collected_at": utc_now(),
        "content": "",
        "content_type": "",
        "http_status": None,
        "final_url": str(selection.get("source_url") or "").strip(),
        "error_code": "",
        "message": "",
    }


def collect_topic_source(
    selection: Mapping[str, Any],
    *,
    timeout: float = 20.0,
    opener: Callable[..., Any] = urlopen,
    tikhub: TikHubTransport | None = None,
) -> dict[str, Any]:
    if not isinstance(selection, Mapping):
        raise TopicCollectionError("选题对象必须是对象")
    source_url = str(selection.get("source_url") or "").strip()
    platform = str(selection.get("platform") or "").strip().lower() or detect_source_platform(source_url)
    base = _base(selection, platform)
    if not base["selection_id"] or not source_url:
        return {**base, "status": "invalid_input", "error_code": "missing_selection_fields", "message": "选题包缺少 selection_id/source_url"}
    parsed = urlparse(source_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return {**base, "status": "invalid_input", "error_code": "invalid_source_url", "message": "source_url 必须是无凭据的 HTTP(S) 地址"}
    metadata = selection.get("source_metadata") if isinstance(selection.get("source_metadata"), Mapping) else {}
    route = selection.get("collection") if isinstance(selection.get("collection"), Mapping) else {}
    route_name = str(route.get("route") or metadata.get("collector_route") or "").strip()
    if route_name == "douyin_hot_search":
        return {**base, "status": "delegated", "content_type": "search_topic", "error_code": "special_collector_required", "message": "这是热点搜索入口，不是具体视频；本批不把搜索标题当正文"}
    if platform == "zhihu" and re.search(r"/(answers|articles|questions)/\d+", parsed.path, re.I):
        if tikhub is None:
            return {**base, "status": "blocked", "content_type": "zhihu_detail", "error_code": "tikhub_transport_missing", "message": "知乎详情需要已登记的 TikHub Transport"}
        try:
            detail = tikhub.fetch_zhihu_detail(source_url)
        except TikHubTransportError as exc:
            return {**base, "status": "blocked", "content_type": "zhihu_detail", "error_code": exc.code, "message": str(exc), "source_metadata": {**dict(metadata), "collector": "tikhub_zhihu_detail"}}
        content = str(detail.get("content") or "").strip()
        ready = detail.get("status") == "ready" and len(content) >= MIN_SOURCE_TEXT_CHARS
        return {
            **base,
            "status": "ready" if ready else "content_missing",
            "content_type": "zhihu_detail",
            "title": str(detail.get("title") or base["title"]),
            "content": content if ready else "",
            "error_code": "" if ready else "detail_content_missing",
            "message": str(detail.get("message") or ("已通过登记的 TikHub 知乎详情接口取得正文" if ready else "知乎详情未返回足够正文")),
            "source_metadata": {**dict(metadata), "collector": "tikhub_zhihu_detail", "endpoint": detail.get("endpoint", ""), "item_id": detail.get("item_id", "")},
        }
    if platform in SOCIAL_PLATFORMS:
        return {
            **base,
            "status": "blocked",
            "content_type": "social_content",
            "error_code": "collector_not_migrated",
            "message": f"{platform} 正文采集器尚未迁移；未用标题或搜索摘要冒充正文",
        }
    try:
        http_status, final_url, raw = _fetch_html(source_url, timeout=max(3.0, min(float(timeout), 60.0)), opener=opener)
    except TopicCollectionError as exc:
        message = str(exc)
        code = "http_blocked" if message in {"HTTP 401", "HTTP 403"} else "fetch_failed"
        return {**base, "status": "blocked" if code == "http_blocked" else "failed", "error_code": code, "message": message}
    parser = _VisibleTextParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    title = _clean_text(parser.title_parts) or base["title"]
    regions = [_clean_text(parts) for parts in parser.article_regions if _clean_text(parts)]
    provider = str(metadata.get("provider") or "")
    if provider == "ProfessionalMedia" and not regions:
        return {**base, "status": "content_missing", "content_type": "html_text", "http_status": http_status, "final_url": final_url, "title": title, "error_code": "article_body_not_found", "message": "未识别到专业媒体文章正文区域，不把导航内容当正文"}
    content = max(regions, key=len, default=_clean_text(parser.text_parts))
    if len(content) < MIN_SOURCE_TEXT_CHARS:
        return {**base, "status": "content_missing", "content_type": "html_text", "http_status": http_status, "final_url": final_url, "title": title, "error_code": "content_too_short", "message": "页面已打开，但正文少于文案消费所需的 80 个字符"}
    return {**base, "status": "ready", "content_type": "html_text", "http_status": http_status, "final_url": final_url, "title": title, "content": content, "message": "已读取公开 HTML 正文，等待 TopicContentManifest 交接", "source_metadata": dict(metadata)}


__all__ = ["MIN_SOURCE_TEXT_CHARS", "TopicCollectionError", "collect_topic_source", "detect_source_platform", "utc_now"]
