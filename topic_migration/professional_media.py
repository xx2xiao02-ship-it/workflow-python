"""专业媒体最小入口。

只保留源码审计确认的三个供应商及其当前能力：人人都是产品经理支持
公开 RSS 近期文章，雪球和 36 氪只支持用户粘贴 HTTPS 文章详情链接。
此处只登记候选，不声称正文已验证；正文仍走统一采集和 CONTENT_READY 闸门。
"""

from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

PROFESSIONAL_MEDIA = {
    "woshipm": "人人都是产品经理",
    "xueqiu": "雪球",
    "36kr": "36氪",
}
_HOSTS = {
    "woshipm": {"woshipm.com", "www.woshipm.com"},
    "xueqiu": {"xueqiu.com", "www.xueqiu.com"},
    "36kr": {"36kr.com", "www.36kr.com"},
}
_PATH_PATTERNS = {
    "woshipm": r"/[A-Za-z0-9_-]+/\d+\.html",
    "xueqiu": r"/\d+/\d+",
    "36kr": r"/p/\d+",
}


class ProfessionalMediaError(ValueError):
    """专业媒体输入或公开 RSS 响应不符合边界。"""


def _article_url(provider: str, value: Any) -> str:
    parsed = urlparse(str(value or "").strip())
    hostname = (parsed.hostname or "").lower()
    if (
        provider not in _HOSTS
        or parsed.scheme.lower() != "https"
        or hostname not in _HOSTS[provider]
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not re.fullmatch(_PATH_PATTERNS[provider], parsed.path)
    ):
        raise ProfessionalMediaError("请粘贴所选媒体的 HTTPS 文章详情链接，不支持首页、短链或其他域名")
    return parsed._replace(query="", fragment="").geturl()


def professional_media_contents(
    payload: Mapping[str, Any],
    *,
    requester: Callable[[], bytes] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ProfessionalMediaError("专业媒体请求必须是对象")
    provider = str(payload.get("provider") or "woshipm").strip().lower()
    action = str(payload.get("action") or "recent").strip().lower()
    if provider not in PROFESSIONAL_MEDIA:
        raise ProfessionalMediaError("不支持的专业媒体")
    keyword = str(payload.get("keyword") or "").strip()[:100]
    rows: list[tuple[str, str, str]] = []
    if action == "import":
        url = _article_url(provider, payload.get("url"))
        title = str(payload.get("title") or "").strip()[:500]
        if not title:
            raise ProfessionalMediaError("请填写文章标题；导入后仍须采集正文")
        rows.append((title, url, ""))
    elif action == "recent" and provider == "woshipm":
        if requester is None:
            with urlopen(Request("https://www.woshipm.com/feed", headers={"User-Agent": "TopicCenterMigration/1.0"}), timeout=10) as response:
                data = response.read(2_000_001)
        else:
            data = requester()
        if len(data) > 2_000_000 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ProfessionalMediaError("RSS 响应超限或包含不支持的 XML 声明")
        try:
            feed = ET.fromstring(data)
        except ET.ParseError as exc:
            raise ProfessionalMediaError("媒体返回的不是有效 RSS") from exc
        for item in feed.findall("./channel/item")[:50]:
            title = str(item.findtext("title") or "").strip()
            try:
                url = _article_url(provider, item.findtext("link") or "")
            except ProfessionalMediaError:
                continue
            if title and (not keyword or keyword.casefold() in title.casefold()):
                rows.append((title[:500], url, item.findtext("pubDate") or ""))
    else:
        raise ProfessionalMediaError("该媒体当前仅支持文章链接导入，不提供关键词搜索")
    contents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for title, url, published in rows:
        if url in seen:
            continue
        seen.add(url)
        contents.append({
            "content_id": "media-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
            "provider": "ProfessionalMedia",
            "platform": provider,
            "content_type": "article",
            "title": html.unescape(title),
            "url": url,
            "source_url": url,
            "author": PROFESSIONAL_MEDIA[provider],
            "published_at": published,
            "content_preview": "",
            "metrics": {},
            "source_metadata": {"intake": action, "body_verified": False, "provider": "ProfessionalMedia"},
        })
    candidate_key = provider + "|" + keyword + "|" + str(payload.get("url") or "")
    return {
        "status": "ready" if contents else "empty",
        "contents": contents,
        "candidate_id": "media-" + hashlib.sha256(candidate_key.encode("utf-8")).hexdigest()[:16],
        "title": contents[0]["title"] if action == "import" and contents else (keyword or PROFESSIONAL_MEDIA[provider] + "近期文章"),
        "message": (
            "仅筛选 RSS 近期文章标题，不是全站搜索；建立选题包后仍需采集正文。"
            if action == "recent"
            else "链接已导入，尚未验证正文；请选择主素材并建立选题包。"
        ),
    }


__all__ = ["PROFESSIONAL_MEDIA", "ProfessionalMediaError", "professional_media_contents"]
