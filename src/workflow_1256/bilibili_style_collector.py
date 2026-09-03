"""B 站公开主页与视频官方字幕采集适配器。

只访问公开元数据及官方/AI 字幕接口；没有字幕时明确返回缺失状态，不伪造转写。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class BilibiliCollectorError(RuntimeError):
    """公开接口不可用、链接不合法或返回格式异常。"""


Requester = Callable[[str, Mapping[str, str], float], Mapping[str, Any]]
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VideoProductionConsole/1.0",
}
DEFAULT_STYLE_VIDEO_DURATION_SECONDS = 10 * 60


def _request_json(url: str, headers: Mapping[str, str], timeout: float) -> Mapping[str, Any]:
    request = Request(url, headers=dict(headers))
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise BilibiliCollectorError(f"B 站公开接口 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise BilibiliCollectorError(f"B 站公开接口连接失败：{type(exc).__name__}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BilibiliCollectorError("B 站公开接口未返回合法 JSON") from exc
    if not isinstance(payload, Mapping):
        raise BilibiliCollectorError("B 站公开接口根节点不是对象")
    if payload.get("code") not in (0, None):
        raise BilibiliCollectorError(f"B 站公开接口业务错误：{payload.get('message') or payload.get('code')}")
    return payload


def _iso_from_unix(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _normalize_video_duration_limit_seconds(value: Any | None) -> int:
    if value in (None, ""):
        return DEFAULT_STYLE_VIDEO_DURATION_SECONDS
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise BilibiliCollectorError("视频时长筛选值必须是秒数") from exc
    if not 1 <= seconds <= DEFAULT_STYLE_VIDEO_DURATION_SECONDS:
        raise BilibiliCollectorError("视频时长筛选范围必须在 1 秒到 10 分钟之间")
    return seconds


def _listing_duration_seconds(video: Mapping[str, Any]) -> int:
    """Read B 站列表的 length/duration without making a detail request."""

    value = video.get("length") or video.get("duration") or 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    parts = str(value or "").strip().split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return max(0, seconds)


def parse_creator_url(url: str) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.netloc.lower() not in {"space.bilibili.com", "www.space.bilibili.com"}:
        raise BilibiliCollectorError("当前仅支持 B 站主页链接，例如 https://space.bilibili.com/123456")
    match = re.fullmatch(r"/(\d+)/?", parsed.path)
    if not match:
        raise BilibiliCollectorError("无法从 B 站主页链接识别 UP 主 mid")
    return match.group(1)


def parse_video_url(url: str) -> str:
    match = re.search(r"\b(BV[0-9A-Za-z]+)\b", str(url).strip(), flags=re.IGNORECASE)
    if not match:
        raise BilibiliCollectorError("当前仅支持包含 BV 号的 B 站单条视频链接")
    return match.group(1)


def _call(requester: Requester, url: str, timeout: float) -> Mapping[str, Any]:
    return requester(url, DEFAULT_HEADERS, timeout)


def fetch_video_transcript(bvid: str, *, requester: Requester = _request_json, timeout: float = 20.0) -> dict[str, Any]:
    view_url = "https://api.bilibili.com/x/web-interface/view?" + urlencode({"bvid": bvid})
    view = _call(requester, view_url, timeout).get("data")
    if not isinstance(view, Mapping):
        raise BilibiliCollectorError("B 站视频详情缺少 data")
    cid = view.get("cid")
    if not cid and isinstance(view.get("pages"), list) and view["pages"]:
        first_page = view["pages"][0]
        cid = first_page.get("cid") if isinstance(first_page, Mapping) else None
    if not cid:
        raise BilibiliCollectorError("B 站视频详情缺少 cid")
    player_url = "https://api.bilibili.com/x/player/v2?" + urlencode({"bvid": bvid, "cid": cid})
    player = _call(requester, player_url, timeout).get("data")
    subtitles = []
    if isinstance(player, Mapping):
        subtitle = player.get("subtitle")
        subtitles = subtitle.get("subtitles", []) if isinstance(subtitle, Mapping) else []
    transcript = ""
    subtitle_url = ""
    if isinstance(subtitles, list) and subtitles:
        first = next((item for item in subtitles if isinstance(item, Mapping) and item.get("subtitle_url")), None)
        if first:
            subtitle_url = str(first["subtitle_url"])
            if subtitle_url.startswith("//"):
                subtitle_url = "https:" + subtitle_url
            subtitle_payload = _call(requester, subtitle_url, timeout)
            body = subtitle_payload.get("body")
            if isinstance(body, list):
                transcript = "\n".join(
                    str(item.get("content", "")).strip()
                    for item in body
                    if isinstance(item, Mapping) and str(item.get("content", "")).strip()
                )
    return {
        "platform": "bilibili",
        "source_url": f"https://www.bilibili.com/video/{bvid}",
        "bvid": bvid,
        "title": str(view.get("title") or "").strip(),
        "published_at": _iso_from_unix(view.get("pubdate")),
        "duration_seconds": int(view.get("duration") or 0),
        "transcript": transcript,
        "transcript_status": "official_subtitle" if transcript else "subtitle_missing",
        "subtitle_url": subtitle_url,
    }


def collect_creator(
    creator_url: str, *, limit: int = 50, requester: Requester = _request_json, timeout: float = 20.0,
    max_video_duration_seconds: int | None = None,
) -> dict[str, Any]:
    mid = parse_creator_url(creator_url)
    limit = max(1, min(int(limit), 100))
    duration_limit_seconds = _normalize_video_duration_limit_seconds(max_video_duration_seconds)
    listing_url = "https://api.bilibili.com/x/space/arc/search?" + urlencode(
        {"mid": mid, "pn": 1, "ps": limit, "order": "pubdate", "jsonp": "jsonp"}
    )
    listing = _call(requester, listing_url, timeout).get("data")
    vlist: list[Any] = []
    if isinstance(listing, Mapping) and isinstance(listing.get("list"), Mapping):
        raw_list = listing["list"].get("vlist")
        vlist = raw_list if isinstance(raw_list, list) else []
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    skipped_long_videos: list[dict[str, Any]] = []
    for video in vlist[:limit]:
        if not isinstance(video, Mapping) or not video.get("bvid"):
            continue
        bvid = str(video["bvid"])
        listed_duration_seconds = _listing_duration_seconds(video)
        if listed_duration_seconds > duration_limit_seconds:
            skipped_long_videos.append({
                "bvid": bvid,
                "title": str(video.get("title") or "").strip(),
                "duration_seconds": listed_duration_seconds,
                "reason": f"超过 {duration_limit_seconds / 60:g} 分钟，跳过字幕提取",
            })
            continue
        try:
            item = fetch_video_transcript(bvid, requester=requester, timeout=timeout)
        except BilibiliCollectorError as exc:
            item = {
                "platform": "bilibili", "source_url": f"https://www.bilibili.com/video/{bvid}", "bvid": bvid,
                "title": str(video.get("title") or "").strip(), "published_at": _iso_from_unix(video.get("created")),
                "duration_seconds": 0, "transcript": "", "transcript_status": "fetch_failed", "subtitle_url": "",
            }
            warnings.append(f"{bvid}: {exc}")
        if int(item.get("duration_seconds") or 0) > duration_limit_seconds:
            skipped_long_videos.append({
                "bvid": bvid,
                "title": str(item.get("title") or video.get("title") or "").strip(),
                "duration_seconds": int(item.get("duration_seconds") or 0),
                "reason": f"超过 {duration_limit_seconds / 60:g} 分钟，跳过字幕提取",
            })
            continue
        items.append(item)
    usable = sum(1 for item in items if len(item["transcript"]) >= 80)
    return {
        "collection_status": "completed",
        "platform": "bilibili",
        "creator_url": creator_url,
        "creator_id": mid,
        "requested_limit": limit,
        "max_video_duration_seconds": duration_limit_seconds,
        "listed_count": len(vlist),
        "skipped_long_video_count": len(skipped_long_videos),
        "skipped_long_videos": skipped_long_videos,
        "transcript_ready_count": usable,
        "items": items,
        "warnings": warnings,
    }


def collect_case_video(video_url: str, *, requester: Requester = _request_json, timeout: float = 20.0) -> dict[str, Any]:
    return fetch_video_transcript(parse_video_url(video_url), requester=requester, timeout=timeout)


__all__ = [
    "BilibiliCollectorError", "collect_case_video", "collect_creator", "fetch_video_transcript",
    "parse_creator_url", "parse_video_url",
]
