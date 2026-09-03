"""选题采集节点：消费标准选题包并返回可审计的内容结果。

当前只对文章/知乎问题走公开 HTML 读取；抖音/B 站仍交给已有视频采集器，
不在这里伪造字幕。任何 401/403、风控或正文不足都会返回 blocked/content_missing，
不会生成假内容。
"""
from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from .bilibili_style_collector import BilibiliCollectorError, collect_case_video as collect_bilibili_case_video
from .douyin_style_collector import DouyinCollectorError, collect_case_video as collect_douyin_case_video
from .tikhub_transport import TikHubTransport, TikHubTransportError


DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TopicCollectionLab/1.0",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def detect_source_platform(source_url: str) -> str:
    """识别公开来源平台；平台识别不代表采集一定成功。"""
    host = urlparse(str(source_url or "").strip()).netloc.lower().removeprefix("www.")
    if host == "zhihu.com" or host.endswith(".zhihu.com"):
        return "zhihu"
    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        return "bilibili"
    if host in {"douyin.com", "v.douyin.com", "iesdouyin.com"} or host.endswith(".douyin.com"):
        return "douyin"
    return "article"


class TopicCollectionError(RuntimeError):
    """采集连接、响应或内容契约错误。"""


class _VisibleTextParser(HTMLParser):
    _ignored = {"script", "style", "noscript", "svg", "template"}
    _article_region_hints = (
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
        self.article_region_hints: list[bool] = []
        self._article_depth = 0
        self._article_parts: list[str] = []
        self._hint_depth = 0
        self._ignore_depth = 0
        self._in_title = False

    @classmethod
    def _is_article_region(cls, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag.lower() == "article":
            return True
        classes = " ".join(str(value or "") for key, value in attrs if key.lower() == "class").lower()
        normalized = re.sub(r"[^a-z0-9_-]+", " ", classes)
        return any(hint in normalized for hint in cls._article_region_hints)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._ignored:
            self._ignore_depth += 1
        if self._article_depth:
            self._article_depth += 1
        if self._hint_depth:
            self._hint_depth += 1
        if self._is_article_region(tag, attrs) and tag != "article":
            self.article_regions.append([])
            self.article_region_hints.append(True)
            self._hint_depth = 1
        elif tag == "article" and self._article_depth == 0:
            self._article_parts = []
            self._article_depth = 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if self._hint_depth:
            self._hint_depth -= 1
        if self._article_depth:
            self._article_depth -= 1
            if self._article_depth == 0 and self._article_parts:
                self.article_regions.append(self._article_parts)
                self.article_region_hints.append(False)
                self._article_parts = []
        if tag in self._ignored and self._ignore_depth:
            self._ignore_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignore_depth or not data.strip():
            return
        value = html.unescape(data).strip()
        if not value:
            return
        if self._in_title:
            self.title_parts.append(value)
        self.text_parts.append(value)
        if self._article_depth:
            self._article_parts.append(value)
        if self._hint_depth and self.article_regions:
            self.article_regions[-1].append(value)


def _clean_text(values: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(values)).strip()


def _fetch_html(url: str, *, timeout: float = 20.0) -> tuple[int, str, str]:
    request = Request(url, headers=DEFAULT_HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return int(getattr(response, "status", 200) or 200), str(response.geturl()), raw
    except HTTPError as exc:
        raise TopicCollectionError(f"HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise TopicCollectionError(f"连接失败：{type(exc).__name__}") from exc


def collect_topic_source(selection: Mapping[str, Any], *, timeout: float = 20.0) -> dict[str, Any]:
    selection_id = str(selection.get("selection_id") or "").strip()
    source_url = str(selection.get("source_url") or "").strip()
    declared_platform = str(selection.get("platform") or "").strip()
    platform = declared_platform or detect_source_platform(source_url)
    collected_at = utc_now()
    base = {
        "selection_id": selection_id,
        "source_url": source_url,
        "platform": platform,
        "title": str(selection.get("title") or "").strip(),
        "collected_at": collected_at,
        "content": "",
        "content_type": "",
        "http_status": None,
        "final_url": source_url,
        "error_code": "",
        "message": "",
    }
    if not selection_id or not source_url:
        return {**base, "status": "invalid_input", "error_code": "missing_selection_fields", "message": "选题包缺少 selection_id/source_url"}
    source_metadata = selection.get("source_metadata") if isinstance(selection.get("source_metadata"), Mapping) else {}
    parsed_url = urlparse(source_url)
    if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
        return {
            **base,
            "status": "invalid_input",
            "error_code": "invalid_source_url",
            "message": "source_url 必须是带域名的 HTTP(S) 地址",
            "source_metadata": dict(source_metadata),
        }
    base["source_metadata"] = dict(source_metadata)
    collector_route = str(
        (selection.get("collection") or {}).get("route") if isinstance(selection.get("collection"), Mapping) else ""
    ).strip()
    if platform == "douyin" and (collector_route == "douyin_hot_search" or source_metadata.get("collector_route") == "douyin_hot_search"):
        return {
            **base,
            "status": "delegated",
            "content_type": "search_topic",
            "error_code": "douyin_hot_search_collector_required",
            "message": "这是抖音热点搜索入口，不是具体视频；需要抖音关键词采集器取得具体视频/字幕后，才能进入文案节点",
            "collector_route": "douyin_hot_search",
        }
    if platform == "douyin":
        try:
            video = collect_douyin_case_video(source_url)
            transcript = str(video.get("transcript") or "").strip()
            return {
                **base,
                "status": "ready" if len(transcript) >= 80 else "content_missing",
                "content_type": "video_transcript",
                "title": str(video.get("title") or base["title"]),
                "content": transcript if len(transcript) >= 80 else "",
                "transcript_status": str(video.get("transcript_status") or ("local_whisper" if transcript else "transcript_missing")),
                "error_code": "" if len(transcript) >= 80 else "transcript_missing",
                "message": "已通过本机抖音采集器取得字幕" if len(transcript) >= 80 else "抖音视频已找到，但本机未取得足够字幕",
                "source_metadata": {**dict(source_metadata), "collector": "douyin_style_collector", "video_id": video.get("video_id")},
            }
        except DouyinCollectorError as exc:
            return {**base, "status": "blocked", "content_type": "video_transcript", "error_code": "douyin_collector_blocked", "message": str(exc)}
    if platform == "bilibili":
        try:
            video = collect_bilibili_case_video(source_url, timeout=timeout)
            transcript = str(video.get("transcript") or "").strip()
            return {
                **base,
                "status": "ready" if len(transcript) >= 80 else "content_missing",
                "content_type": "video_transcript",
                "title": str(video.get("title") or base["title"]),
                "content": transcript if len(transcript) >= 80 else "",
                "transcript_status": str(video.get("transcript_status") or ("official_subtitle" if transcript else "subtitle_missing")),
                "error_code": "" if len(transcript) >= 80 else "subtitle_missing",
                "message": "已通过B站公开字幕接口取得字幕" if len(transcript) >= 80 else "B站视频未提供足够字幕",
                "source_metadata": {**dict(source_metadata), "collector": "bilibili_style_collector", "bvid": video.get("bvid")},
            }
        except BilibiliCollectorError as exc:
            return {**base, "status": "blocked", "content_type": "video_transcript", "error_code": "bilibili_collector_blocked", "message": str(exc)}
    # 搜索结果里的 /answers/、/articles/ 可由 TikHub 详情接口补正文；
    # 普通知乎页面仍走公开 HTML，保留其 401/403 可审计状态。
    if platform == "zhihu" and re.search(r"/(answers|articles|questions)/\d+", parsed_url.path, re.I):
        try:
            detail = TikHubTransport.from_env().fetch_zhihu_detail(source_url)
            content = str(detail.get("content") or "").strip()
            ready = detail.get("status") == "ready" and len(content) >= 80
            return {
                **base,
                "status": "ready" if ready else "content_missing",
                "content_type": "zhihu_detail",
                "title": str(detail.get("title") or base["title"]),
                "content": content if ready else "",
                "transcript_status": "tikhub_zhihu_detail" if ready else "detail_missing",
                "error_code": "" if ready else "detail_content_missing",
                "message": str(detail.get("message") or ("已通过TikHub知乎详情接口取得正文" if ready else "知乎详情未返回足够正文")),
                "source_metadata": {
                    **dict(source_metadata),
                    "collector": "tikhub_zhihu_detail",
                    "endpoint": detail.get("endpoint", ""),
                    "item_id": detail.get("item_id", ""),
                },
            }
        except TikHubTransportError as exc:
            return {
                **base,
                "status": "blocked",
                "content_type": "zhihu_detail",
                "error_code": str(exc.code or "tikhub_detail_failed"),
                "message": str(exc),
                "source_metadata": {**dict(source_metadata), "collector": "tikhub_zhihu_detail"},
            }
    try:
        http_status, final_url, raw = _fetch_html(source_url, timeout=timeout)
    except TopicCollectionError as exc:
        message = str(exc)
        code = "http_blocked" if message.startswith("HTTP 401") or message.startswith("HTTP 403") else "fetch_failed"
        return {**base, "status": "blocked" if code == "http_blocked" else "failed", "error_code": code, "message": message}
    parser = _VisibleTextParser()
    parser.feed(raw)
    title = _clean_text(parser.title_parts) or base["title"]
    # 优先使用页面标记的文章主体（如 article、articleBody、post-content）；
    # 只有页面没有主体标记时才回退到全部可见文本，避免把导航/推荐列表
    # 当成正文返回。
    hinted_contents = [
        _clean_text(parts)
        for parts, hinted in zip(parser.article_regions, parser.article_region_hints)
        if hinted and _clean_text(parts)
    ]
    article_contents = hinted_contents or [_clean_text(parts) for parts in parser.article_regions if _clean_text(parts)]
    content = max(article_contents, key=len, default=_clean_text(parser.text_parts))
    if len(content) < 80:
        return {
            **base,
            "status": "content_missing",
            "content_type": "html_text",
            "http_status": http_status,
            "final_url": final_url,
            "title": title,
            "error_code": "content_too_short",
            "message": "页面已打开，但没有达到文案消费所需的正文长度",
        }
    return {
        **base,
        "status": "ready",
        "content_type": "html_text",
        "http_status": http_status,
        "final_url": final_url,
        "title": title,
        "content": content,
        "message": "已读取公开 HTML 文本，等待文案节点消费",
    }


__all__ = ["TopicCollectionError", "collect_topic_source", "detect_source_platform", "utc_now"]
