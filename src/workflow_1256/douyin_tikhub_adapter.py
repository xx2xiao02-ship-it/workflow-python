"""TikHub 驱动的抖音账号采集适配器。

本模块只负责把 TikHub 的抖音账号/作品响应归一化成项目自己的公开数据
结构。它不复制第三方 SDK 源码，也不接触浏览器 Cookie。所有外部调用都
通过现有 :class:`TikHubTransport`，因此可以用固定响应样本做离线测试。

注意：TikHub 返回的 ``music.play_url`` 是配乐/原声资源，不能直接作为
博主口播音频。本适配器只从 ``video`` 资源或高质量播放地址中选媒体，下载
后再交给 FFmpeg 提取音频。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .tikhub_transport import TikHubTransport, TikHubTransportError


DOUYIN_GET_SEC_USER_ID_PATH = "/api/v1/douyin/web/get_sec_user_id"
DOUYIN_USER_POST_VIDEOS_PATH = "/api/v1/douyin/web/fetch_user_post_videos"
DOUYIN_ONE_VIDEO_PATH = "/api/v1/douyin/web/fetch_one_video"
DOUYIN_ONE_VIDEO_BY_SHARE_PATH = "/api/v1/douyin/web/fetch_one_video_by_share_url"
DOUYIN_HIGH_QUALITY_PLAY_URL_PATH = "/api/v1/douyin/web/fetch_video_high_quality_play_url"

_URL_RE = re.compile(r"https?://[^\s，。；、）》)\]】]+", re.IGNORECASE)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,200}$")
_MUSIC_KEYS = {
    "music", "music_info", "music_info_v2", "song", "audio", "audio_info",
    "music_play_url", "music_url", "audio_url",
}
_MEDIA_KEYS = {
    "video_url", "video_play_url", "download_url", "play_addr", "download_addr",
    "play_url", "url_list", "url", "uri",
}


class TikHubDouyinAdapterError(RuntimeError):
    """TikHub 抖音账号适配器错误。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "tikhub_douyin_error",
        endpoint: str = "",
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.endpoint = endpoint
        self.request_id = request_id


@dataclass(frozen=True)
class TikHubVideo:
    """供现有转写/Obsidian桥接使用的最小作品对象。"""

    aweme_id: str
    title: str = ""
    share_url: str = ""
    video_url: str = ""
    duration_ms: int = 0
    create_time: int = 0
    author_name: str = ""
    cover_url: str = ""
    media_source: str = "video.play_addr"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def video_id(self) -> str:
        """兼容旧 ``douyin_style_collector`` 的字段名。"""

        return self.aweme_id

    @property
    def public_url(self) -> str:
        return self.share_url or (
            f"https://www.douyin.com/video/{self.aweme_id}" if self.aweme_id else ""
        )


@dataclass(frozen=True)
class TikHubUserVideosPage:
    sec_user_id: str
    items: list[TikHubVideo]
    max_cursor: str = ""
    has_more: bool = False
    request_id: str = ""
    endpoint: str = DOUYIN_USER_POST_VIDEOS_PATH


def _clean_text(value: Any, limit: int = 2_000) -> str:
    if isinstance(value, (list, tuple)):
        value = next((item for item in value if item is not None and str(item).strip()), "")
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _first_value(value: Any, keys: Sequence[str], *, max_depth: int = 8) -> Any:
    """大小写不敏感地从 TikHub 的多种包装中取第一个字段。"""

    wanted = {str(key).lower() for key in keys}
    seen: set[int] = set()

    def walk(node: Any, depth: int) -> Any:
        if depth > max_depth or id(node) in seen:
            return None
        if isinstance(node, Mapping):
            seen.add(id(node))
            for key, item in node.items():
                if str(key).lower() in wanted and item not in (None, "", [], {}):
                    return item
            for item in node.values():
                found = walk(item, depth + 1)
                if found not in (None, "", [], {}):
                    return found
        elif isinstance(node, list):
            seen.add(id(node))
            for item in node:
                found = walk(item, depth + 1)
                if found not in (None, "", [], {}):
                    return found
        return None

    return walk(value, 0)


def _first_mapping(value: Any, keys: Sequence[str], *, max_depth: int = 8) -> Mapping[str, Any] | None:
    wanted = {str(key).lower() for key in keys}
    seen: set[int] = set()

    def walk(node: Any, depth: int) -> Mapping[str, Any] | None:
        if depth > max_depth or id(node) in seen:
            return None
        if isinstance(node, Mapping):
            seen.add(id(node))
            for key, item in node.items():
                if str(key).lower() in wanted and isinstance(item, Mapping):
                    return item
            for item in node.values():
                found = walk(item, depth + 1)
                if found is not None:
                    return found
        elif isinstance(node, list):
            seen.add(id(node))
            for item in node:
                found = walk(item, depth + 1)
                if found is not None:
                    return found
        return None

    return walk(value, 0)


def _first_list(value: Any, keys: Sequence[str], *, max_depth: int = 8) -> list[Mapping[str, Any]]:
    wanted = {str(key).lower() for key in keys}
    seen: set[int] = set()

    def walk(node: Any, depth: int) -> list[Mapping[str, Any]]:
        if depth > max_depth or id(node) in seen:
            return []
        if isinstance(node, Mapping):
            seen.add(id(node))
            for key, item in node.items():
                if str(key).lower() in wanted and isinstance(item, list):
                    found = [entry for entry in item if isinstance(entry, Mapping)]
                    if found:
                        return found
            for item in node.values():
                found = walk(item, depth + 1)
                if found:
                    return found
        elif isinstance(node, list):
            seen.add(id(node))
            direct = [entry for entry in node if isinstance(entry, Mapping)]
            if direct and any(
                _first_value(entry, ("aweme_id", "item_id", "video_id"), max_depth=2)
                for entry in direct
            ):
                return direct
            for item in node:
                found = walk(item, depth + 1)
                if found:
                    return found
        return []

    return walk(value, 0)


def _extract_url_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip().startswith(("http://", "https://")) else []
    if isinstance(value, Mapping):
        for key in ("url_list", "urls", "url", "uri"):
            if key in value:
                found = _extract_url_list(value[key])
                if found:
                    return found
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip().startswith(("http://", "https://"))]
    return []


def _looks_like_media_url(url: str, *, key: str = "") -> bool:
    text = str(url or "").strip()
    if not text.startswith(("http://", "https://")):
        return False
    lower = text.lower()
    key_lower = str(key or "").lower()
    if any(token in lower for token in (".jpg", ".jpeg", ".png", ".webp", ".gif", "douyinpic.com")):
        return False
    return bool(
        key_lower in {"video_url", "video_play_url", "download_url", "play_addr", "download_addr", "play_url"}
        or any(token in lower for token in (".mp4", ".m4s", ".webm", "/video/", "douyinvod", "mime_type=video_mp4", "aweme/v1/play"))
    )


def _video_media_candidates(value: Any, *, ancestors: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """只从 video 资源遍历媒体 URL，跳过 music.play_url。"""

    candidates: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).lower()
            if key in _MUSIC_KEYS or any(parent in _MUSIC_KEYS for parent in ancestors):
                continue
            if key in _MEDIA_KEYS:
                for url in _extract_url_list(item):
                    if _looks_like_media_url(url, key=key):
                        candidates.append((url, ".".join((*ancestors, key))))
            if isinstance(item, (Mapping, list, tuple)):
                candidates.extend(_video_media_candidates(item, ancestors=(*ancestors, key)))
    elif isinstance(value, (list, tuple)):
        for item in value:
            candidates.extend(_video_media_candidates(item, ancestors=ancestors))
    return candidates


def _duration_ms(value: Any) -> int:
    if isinstance(value, Mapping):
        value = _first_value(value, ("duration_ms", "duration", "milliseconds", "value"), max_depth=2)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    # TikHub/Douyin video.duration is normally milliseconds.  Do not treat an
    # implausibly tiny millisecond value as seconds; the caller can still keep
    # it as an unknown duration and avoid a false long-video rejection.
    if 0 < number < 1_000:
        number *= 1_000
    return max(0, int(round(number)))


def _unwrap_aweme(value: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("aweme_info", "item", "video_info"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and _first_value(nested, ("aweme_id", "item_id", "video_id"), max_depth=2):
            return nested
    data = value.get("data")
    if isinstance(data, Mapping):
        nested = _unwrap_aweme(data)
        if nested is not data:
            return nested
    return value


def normalize_tikhub_video(value: Mapping[str, Any], *, fallback_share_url: str = "") -> TikHubVideo | None:
    raw = _unwrap_aweme(value)
    aweme_id = _clean_text(_first_value(raw, ("aweme_id", "item_id", "video_id", "id"), max_depth=4), 220)
    if not aweme_id or not _ID_RE.fullmatch(aweme_id):
        return None
    title = _clean_text(_first_value(raw, ("desc", "title", "item_title", "video_title", "share_title"), max_depth=4), 2_000)
    author = _first_mapping(raw, ("author", "author_info", "user", "creator"), max_depth=4)
    author_name = _clean_text(
        _first_value(author or raw, ("nickname", "nick_name", "name", "unique_id", "sec_uid"), max_depth=3), 200
    )
    video = _first_mapping(raw, ("video",), max_depth=2) or {}
    media_candidates = _video_media_candidates(video)
    if not media_candidates:
        # Some endpoints flatten video_url/play_addr at the top level.
        media_candidates = _video_media_candidates(raw)
    media_url, media_source = media_candidates[0] if media_candidates else ("", "")
    share_url = _clean_text(
        _first_value(raw, ("share_url", "aweme_url", "video_url_share"), max_depth=5) or fallback_share_url,
        2_000,
    )
    if not share_url:
        share_url = f"https://www.douyin.com/video/{aweme_id}"
    cover = _first_mapping(video, ("cover", "origin_cover", "dynamic_cover"), max_depth=2)
    cover_url = _clean_text(_extract_url_list(cover or {})[0] if _extract_url_list(cover or {}) else "", 2_000)
    try:
        create_time = int(float(_first_value(raw, ("create_time", "publish_time", "published_at"), max_depth=4) or 0))
    except (TypeError, ValueError):
        create_time = 0
    return TikHubVideo(
        aweme_id=aweme_id,
        title=title or f"抖音作品 {aweme_id}",
        share_url=share_url,
        video_url=media_url,
        duration_ms=_duration_ms(_first_value(video or raw, ("duration", "duration_ms", "video_duration"), max_depth=3)),
        create_time=create_time,
        author_name=author_name,
        cover_url=cover_url,
        media_source=media_source or "video",
        raw=dict(raw),
    )


def extract_sec_user_id_from_url(value: str) -> str:
    """从完整抖音主页 URL 提取 sec_user_id；短链留给 TikHub 解析。"""

    raw = str(value or "").strip()
    for candidate in _URL_RE.findall(raw) or [raw]:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host not in {"douyin.com", "iesdouyin.com"} and not host.endswith(".douyin.com"):
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0].lower() == "user" and parts[1]:
            return parts[1]
        query = dict(part.split("=", 1) for part in parsed.query.split("&") if "=" in part)
        for key in ("sec_uid", "sec_user_id", "sec_userid"):
            if query.get(key):
                return query[key]
    return ""


def _source_url(value: str) -> str:
    match = _URL_RE.search(str(value or "").strip())
    return match.group(0).rstrip("。；，,;）)】]") if match else str(value or "").strip()


class TikHubDouyinAdapter:
    """账号主页、作品分页、详情与媒体地址适配器。"""

    def __init__(
        self,
        transport: TikHubTransport | Any | None = None,
        *,
        requester: Callable[..., Any] | None = None,
        media_opener: Callable[..., Any] = urlopen,
        share_url_opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport or TikHubTransport.from_env()
        self.requester = requester
        self.media_opener = media_opener
        # 分享短链展开与媒体下载使用不同注入点，便于离线测试且不把
        # 短链跳转错误误判成 TikHub 鉴权错误。
        self.share_url_opener = share_url_opener or urlopen
        self.sleeper = sleeper
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []
        self.last_error: dict[str, Any] = {}

    def _request(self, method: str, endpoint: str, *, params: Mapping[str, Any] | None = None) -> Any:
        self.call_count += 1
        try:
            if self.requester is not None:
                response = self.requester(method, endpoint, params=dict(params or {}))
            elif hasattr(self.transport, "request_json"):
                response = self.transport.request_json(method, endpoint, params=params)
            else:
                response = self.transport._request_json(method, endpoint, params=params)
            request_id = _clean_text(_first_value(response, ("request_id", "requestId", "requestid"), max_depth=3), 200)
            self.calls.append({"endpoint": endpoint, "request_id": request_id, "status": "ok"})
            return response
        except TikHubTransportError as exc:
            entry = {
                "endpoint": endpoint,
                "request_id": "",
                "status": "error",
                "error_type": exc.code,
                "http_status": exc.http_status,
            }
            self.calls.append(entry)
            self.last_error = entry
            raise TikHubDouyinAdapterError(
                str(exc), code=exc.code, endpoint=endpoint,
            ) from exc
        except Exception as exc:
            entry = {"endpoint": endpoint, "request_id": "", "status": "error", "error_type": type(exc).__name__}
            self.calls.append(entry)
            self.last_error = entry
            raise TikHubDouyinAdapterError(str(exc), code="request_failed", endpoint=endpoint) from exc

    def diagnostics(self) -> dict[str, Any]:
        return {
            "provider": "TikHub",
            "adapter": "douyin_account_v1",
            "call_count": self.call_count,
            "calls": [dict(item) for item in self.calls[-100:]],
            "last_error": dict(self.last_error),
        }

    def resolve_sec_user_id(self, homepage_url: str) -> str:
        direct = extract_sec_user_id_from_url(homepage_url)
        if direct:
            return direct
        source_url = _source_url(homepage_url)
        if not source_url.startswith(("http://", "https://")):
            raise TikHubDouyinAdapterError("主页链接不是有效 HTTP(S) 地址", code="invalid_input")
        parsed = urlparse(source_url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        # TikHub 的 get_sec_user_id 要求用户主页 URL，不保证接受
        # v.douyin.com 分享短链。先在本地展开短链，再把最终 /user/ URL
        # 交给 TikHub；展开失败时明确返回入口错误，不盲目发送无效请求。
        if host == "v.douyin.com":
            try:
                request = Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
                with self.share_url_opener(request, timeout=15) as response:
                    resolved = str(getattr(response, "geturl", lambda: "")() or "")
                resolved_id = extract_sec_user_id_from_url(resolved)
                if resolved_id:
                    return resolved_id
            except Exception as exc:
                raise TikHubDouyinAdapterError(
                    f"抖音主页分享短链展开失败：{type(exc).__name__}",
                    code="short_link_unresolved",
                ) from exc
            raise TikHubDouyinAdapterError(
                "抖音分享短链未展开为 /user/ 主页链接，请复制‘查看TA的更多作品’后的主页链接",
                code="short_link_unresolved",
            )
        response = self._request("GET", DOUYIN_GET_SEC_USER_ID_PATH, params={"url": source_url})
        value = _first_value(response, ("sec_user_id", "sec_uid", "sec_userid"), max_depth=8)
        sec_user_id = _clean_text(value, 220)
        if not sec_user_id:
            raise TikHubDouyinAdapterError("TikHub 未返回 sec_user_id", code="sec_user_id_missing", endpoint=DOUYIN_GET_SEC_USER_ID_PATH)
        return sec_user_id

    def fetch_user_post_videos(
        self,
        sec_user_id: str,
        *,
        max_cursor: str | int = 0,
        count: int = 20,
        filter_type: int | str = 0,
        cookie: str = "",
    ) -> TikHubUserVideosPage:
        sec = _clean_text(sec_user_id, 220)
        if not sec:
            raise TikHubDouyinAdapterError("缺少 sec_user_id", code="invalid_input")
        # TikHub 官方文档要求主页分页 count 不超过 20；超过时会出现
        # 返回数量不足或游标异常，进而误判“没有下一页”。
        count = max(1, min(int(count), 20))
        params: dict[str, Any] = {
            "sec_user_id": sec,
            "max_cursor": str(max_cursor or "0"),
            "count": count,
            "filter_type": filter_type,
        }
        # TikHub 账号接口的 Cookie 是可选兼容参数；默认绝不传递。
        if str(cookie or "").strip():
            params["cookie"] = str(cookie).strip()
        response = self._request("GET", DOUYIN_USER_POST_VIDEOS_PATH, params=params)
        rows = _first_list(response, ("aweme_list", "item_list", "items", "list", "videos", "data"))
        items: list[TikHubVideo] = []
        seen: set[str] = set()
        for row in rows:
            video = normalize_tikhub_video(row)
            if video is None or video.aweme_id in seen:
                continue
            seen.add(video.aweme_id)
            items.append(video)
        cursor = _clean_text(_first_value(response, ("max_cursor", "next_max_cursor", "nextMaxCursor", "next_page_cursor", "nextPageCursor", "next_cursor", "cursor", "min_cursor"), max_depth=8), 200)
        has_more_raw = _first_value(response, ("has_more", "hasMore", "more"), max_depth=8)
        has_more = bool(has_more_raw is True or str(has_more_raw).strip().lower() in {"1", "true", "yes"})
        request_id = _clean_text(_first_value(response, ("request_id", "requestId", "requestid"), max_depth=3), 200)
        return TikHubUserVideosPage(
            sec_user_id=sec,
            items=items,
            max_cursor=cursor,
            has_more=has_more,
            request_id=request_id,
        )

    def fetch_video(self, aweme_id: str, *, share_url: str = "") -> TikHubVideo:
        item_id = _clean_text(aweme_id, 220)
        if not item_id:
            raise TikHubDouyinAdapterError("缺少 aweme_id", code="invalid_input")
        if share_url:
            endpoint = DOUYIN_ONE_VIDEO_BY_SHARE_PATH
            response = self._request("GET", endpoint, params={"share_url": share_url})
        else:
            endpoint = DOUYIN_ONE_VIDEO_PATH
            response = self._request("GET", endpoint, params={"aweme_id": item_id})
        rows = _first_list(response, ("aweme_list", "item_list", "items", "list"))
        candidate: Mapping[str, Any] | None = rows[0] if rows else None
        if candidate is None:
            mapping = _first_mapping(response, ("aweme_info", "item", "data", "result"), max_depth=6)
            candidate = mapping or (response if isinstance(response, Mapping) else None)
        video = normalize_tikhub_video(candidate or {}, fallback_share_url=share_url)
        if video is None or video.aweme_id != item_id:
            # 详情接口有时只返回单层 data；允许 ID 缺失但禁止把其它作品混入。
            if video is None:
                raise TikHubDouyinAdapterError("TikHub 单作品响应缺少 aweme_id", code="aweme_id_missing", endpoint=endpoint)
            video = TikHubVideo(
                aweme_id=item_id, title=video.title, share_url=video.share_url,
                video_url=video.video_url, duration_ms=video.duration_ms,
                create_time=video.create_time, author_name=video.author_name,
                cover_url=video.cover_url, media_source=video.media_source, raw=video.raw,
            )
        return video

    def fetch_video_media_url(self, aweme_id: str, *, share_url: str = "", detail: TikHubVideo | None = None) -> str:
        if detail is not None and detail.video_url and _looks_like_media_url(detail.video_url, key="video_url"):
            return detail.video_url
        params: dict[str, Any] = {"aweme_id": str(aweme_id).strip()}
        if share_url:
            params["share_url"] = share_url
        # URL 过期后的刷新优先按 aweme_id 请求高质量播放地址，避免先
        # 复用旧详情或把分享短链重新导向浏览器；详情接口只作为网关未
        # 提供高质量地址时的兼容兜底。
        response = self._request("GET", DOUYIN_HIGH_QUALITY_PLAY_URL_PATH, params=params)
        candidates = _video_media_candidates(response)
        if candidates:
            return candidates[0][0]
        if detail is None:
            video = self.fetch_video(aweme_id, share_url=share_url)
            if video.video_url and _looks_like_media_url(video.video_url, key="video_url"):
                return video.video_url
        raise TikHubDouyinAdapterError(
            "TikHub 未返回可下载的视频媒体地址（未使用 music.play_url）",
            code="media_url_missing", endpoint=DOUYIN_HIGH_QUALITY_PLAY_URL_PATH,
        )

    @staticmethod
    def _validate_download(path: Path, *, content_type: str = "", min_bytes: int = 1_024) -> dict[str, Any]:
        if not path.is_file():
            raise TikHubDouyinAdapterError("媒体文件未生成", code="media_missing")
        size = path.stat().st_size
        if size < min_bytes:
            raise TikHubDouyinAdapterError("媒体文件过小，可能是错误页", code="media_too_small")
        head = path.read_bytes()[:512].lower()
        if b"<html" in head or b"<!doctype" in head or "text/html" in str(content_type).lower():
            raise TikHubDouyinAdapterError("下载结果不是视频文件", code="media_not_video")
        # MP4/M4S 通常在前 32 字节内含 ftyp；测试/代理可能只给视频 MIME，
        # 因此 MIME 与扩展名都作为合法证据保留。
        has_ftyp = b"ftyp" in head[:64]
        suffix_ok = path.suffix.lower() in {".mp4", ".m4s", ".webm", ".mov"}
        if not has_ftyp and not suffix_ok and not str(content_type).lower().startswith("video/"):
            raise TikHubDouyinAdapterError("下载结果文件类型无法确认", code="media_type_unknown")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"size_bytes": size, "sha256": digest, "content_type": str(content_type or "")}

    def download_media(
        self,
        aweme_id: str,
        output_path: str | Path,
        *,
        media_url: str = "",
        share_url: str = "",
        expected_duration_seconds: float | None = None,
        min_bytes: int = 1_024,
        ffprobe_path: str = "",
    ) -> dict[str, Any]:
        """下载视频并校验；临时 URL 失效时按 aweme_id 重新解析一次。"""

        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        current_url = media_url or self.fetch_video_media_url(aweme_id, share_url=share_url)
        ffprobe_path = str(ffprobe_path or shutil.which("ffprobe") or "")
        refreshed = False
        errors: list[str] = []
        for attempt in range(2):
            try:
                request = Request(current_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"})
                with self.media_opener(request, timeout=120) as response:
                    content_type = str(getattr(response, "headers", {}).get("Content-Type", "") or "")
                    with target.open("wb") as handle:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            handle.write(chunk)
                metadata = self._validate_download(target, content_type=content_type, min_bytes=min_bytes)
                duration_seconds: float | None = None
                if ffprobe_path:
                    try:
                        result = subprocess.run(
                            [ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(target)],
                            capture_output=True, text=True, timeout=15, check=True,
                        )
                        duration_seconds = round(float(result.stdout.strip()), 3)
                        if expected_duration_seconds is not None and duration_seconds > 0:
                            # CDN 容器时长与列表接口可能有少量舍入误差；
                            # 超过 3 秒说明拿到的并非同一作品媒体。
                            if abs(duration_seconds - float(expected_duration_seconds)) > 3.0:
                                raise TikHubDouyinAdapterError(
                                    "下载媒体时长与作品元数据不一致",
                                    code="media_duration_mismatch",
                                )
                    except (OSError, ValueError, subprocess.SubprocessError):
                        duration_seconds = None
                metadata.update({
                    "aweme_id": str(aweme_id),
                    "media_url": current_url,
                    "media_url_refreshed": refreshed,
                    "duration_seconds": duration_seconds,
                    "duration_validated": duration_seconds is not None,
                    "expected_duration_seconds": expected_duration_seconds,
                })
                return metadata
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                target.unlink(missing_ok=True)
                if attempt == 0:
                    # 不能把旧临时 URL写进 checkpoint；只在内存里重新按 aweme_id解析。
                    current_url = self.fetch_video_media_url(aweme_id, share_url=share_url)
                    refreshed = True
                    continue
                raise TikHubDouyinAdapterError(
                    "TikHub 媒体下载失败：" + "；".join(errors[-2:]),
                    code="media_download_failed",
                ) from exc
        raise TikHubDouyinAdapterError("TikHub 媒体下载失败", code="media_download_failed")


__all__ = [
    "DOUYIN_GET_SEC_USER_ID_PATH",
    "DOUYIN_HIGH_QUALITY_PLAY_URL_PATH",
    "DOUYIN_ONE_VIDEO_BY_SHARE_PATH",
    "DOUYIN_ONE_VIDEO_PATH",
    "DOUYIN_USER_POST_VIDEOS_PATH",
    "TikHubDouyinAdapter",
    "TikHubDouyinAdapterError",
    "TikHubUserVideosPage",
    "TikHubVideo",
    "extract_sec_user_id_from_url",
    "normalize_tikhub_video",
]
