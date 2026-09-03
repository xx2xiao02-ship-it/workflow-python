"""复用项目现有抖音登录态采集器，为文案创作层提供可追溯的作品/转写清单。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .douyin_tikhub_adapter import (
    TikHubDouyinAdapter,
    TikHubDouyinAdapterError,
    TikHubVideo,
)


class DouyinCollectorError(RuntimeError):
    """抖音登录态、采集、下载或本地转写不可用。"""


# Keep the historical ten-minute behavior for callers that do not supply a
# collection policy.  The production writing intake now passes five minutes
# explicitly, so a resumed task retains the user's chosen bound instead of
# relying on a process-global default.
DEFAULT_STYLE_VIDEO_DURATION_SECONDS = 10 * 60
MAX_STYLE_VIDEO_DURATION_MS = DEFAULT_STYLE_VIDEO_DURATION_SECONDS * 1000
MIN_TIKHUB_TRANSCRIPT_CHARS = 80


MONITOR_ROOT = Path(__file__).resolve().parents[2] / "services" / "douyin-monitor"
TranscriptProvider = Callable[[Any], str]
ProgressReporter = Callable[[str, int, str], None]
CheckpointReporter = Callable[[dict[str, Any]], None]
ItemReporter = Callable[[dict[str, Any]], None]
TranscriptionCheckpointReporter = Callable[[int, dict[str, str], list[str]], None]
ShareUrlOpener = Callable[[Request], Any]

_DOUYIN_SHARE_URL = re.compile(
    r"https?://(?:(?:(?:www|v)\.)?douyin\.com|(?:www\.)?iesdouyin\.com)/[^\s，。；、]+",
    re.IGNORECASE,
)


def extract_douyin_share_url(value: str) -> str:
    """从抖音分享口令或纯链接中提取可解析的链接。"""
    match = _DOUYIN_SHARE_URL.search(str(value).strip())
    if not match:
        raise DouyinCollectorError("未识别到抖音链接，请粘贴含 v.douyin.com 或 douyin.com 的分享文本")
    return match.group(0).rstrip("。；，,;）)】]")


def resolve_creator_url(value: str, *, opener: ShareUrlOpener | None = None) -> str:
    """把分享文本或短链展开为可供采集器使用的抖音主页地址。"""
    share_url = extract_douyin_share_url(value)
    parsed = urlparse(share_url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "douyin.com" and parsed.path.startswith("/user/"):
        return share_url
    if host != "v.douyin.com":
        raise DouyinCollectorError("请粘贴达人主页分享链接；当前链接不是抖音主页或短链")
    request = Request(share_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        open_request = opener or (lambda request: urlopen(request, timeout=15))
        response = open_request(request)
        try:
            resolved_url = str(response.geturl())
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        raise DouyinCollectorError(f"抖音分享短链展开失败：{type(exc).__name__}: {exc}") from exc
    resolved = urlparse(resolved_url)
    resolved_host = resolved.netloc.lower().removeprefix("www.")
    if resolved_host == "douyin.com" and resolved.path.startswith("/user/"):
        return resolved_url
    if resolved_host == "iesdouyin.com":
        parts = [part for part in resolved.path.split("/") if part]
        if len(parts) >= 3 and parts[:2] == ["share", "user"] and parts[2]:
            return f"https://www.douyin.com/user/{parts[2]}"
    if resolved_host == "douyin.com" and resolved.path.startswith("/video/"):
        raise DouyinCollectorError("该分享链接指向单条视频，不是达人主页；请在抖音点“查看 TA 的更多作品”后分享主页链接")
    raise DouyinCollectorError("短链已展开，但未指向达人主页；请复制“查看 TA 的更多作品”进入后的主页分享链接")


def resolve_case_video_url(value: str, *, opener: ShareUrlOpener | None = None) -> str:
    """把单条视频分享文本或短链展开为标准视频链接。"""
    share_url = extract_douyin_share_url(value)
    parsed = urlparse(share_url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "douyin.com" and parsed.path.startswith("/video/"):
        return f"https://www.douyin.com/video/{parse_video_url(share_url)}"
    if host == "iesdouyin.com" and parsed.path.startswith("/share/video/"):
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[:2] == ["share", "video"] and parts[2].isdigit():
            return f"https://www.douyin.com/video/{parts[2]}"
    if host != "v.douyin.com":
        raise DouyinCollectorError("请粘贴抖音单条视频链接或包含该链接的完整分享文案")

    request = Request(share_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        open_request = opener or (lambda request: urlopen(request, timeout=15))
        response = open_request(request)
        try:
            resolved_url = str(response.geturl())
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        raise DouyinCollectorError(f"抖音视频分享短链展开失败：{type(exc).__name__}: {exc}") from exc

    resolved = urlparse(resolved_url)
    resolved_host = resolved.netloc.lower().removeprefix("www.")
    if resolved_host == "douyin.com" and resolved.path.startswith("/video/"):
        return f"https://www.douyin.com/video/{parse_video_url(resolved_url)}"
    if resolved_host == "iesdouyin.com":
        parts = [part for part in resolved.path.split("/") if part]
        if len(parts) >= 3 and parts[:2] == ["share", "video"] and parts[2].isdigit():
            return f"https://www.douyin.com/video/{parts[2]}"
    raise DouyinCollectorError("短链已展开，但未指向单条视频；请确认复制的是作品分享链接")


def parse_creator_url(url: str) -> str:
    parsed = urlparse(str(url).strip())
    host = parsed.netloc.lower().removeprefix("www.")
    if host != "douyin.com":
        raise DouyinCollectorError("请粘贴展开后的抖音主页链接，例如 https://www.douyin.com/user/SEC_UID")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "user" or not parts[1]:
        raise DouyinCollectorError("无法从抖音主页链接识别 sec_uid；请使用 /user/ 开头的完整主页链接")
    return parts[1]


def parse_video_url(url: str) -> str:
    parsed = urlparse(str(url).strip())
    host = parsed.netloc.lower().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    if host != "douyin.com" or len(parts) != 2 or parts[0] != "video" or not parts[1].isdigit():
        raise DouyinCollectorError("请粘贴抖音单条视频链接，例如 https://www.douyin.com/video/123456")
    return parts[1]


def _load_spider_runtime() -> Any:
    if not MONITOR_ROOT.exists():
        raise DouyinCollectorError("未找到项目内 services/douyin-monitor，无法调用抖音登录态采集器")
    # The video-production console is often started with the system Python,
    # while the Douyin monitor owns its browser/runtime dependencies in its
    # project venv.  Make that environment importable before loading spider.py
    # (which imports Playwright at module import time).  This keeps the
    # collector's existing login/session behavior and avoids requiring a
    # second server process or installing project-only packages globally.
    monitor_site_packages = MONITOR_ROOT / ".venv" / "Lib" / "site-packages"
    if monitor_site_packages.is_dir():
        site_packages_text = str(monitor_site_packages)
        if site_packages_text not in sys.path:
            sys.path.insert(0, site_packages_text)
    root_text = str(MONITOR_ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        from spider import DouyinSpider
    except Exception as exc:
        raise DouyinCollectorError(f"抖音主页采集运行环境不可用：{type(exc).__name__}: {exc}") from exc
    return DouyinSpider


def _load_transcriber_runtime() -> tuple[Any, Any, Any, Any]:
    _load_spider_runtime()
    try:
        from transcriber import FFMPEG_PATH, VideoFetcher, WHISPER_AVAILABLE, download_video, extract_audio, transcribe
    except Exception as exc:
        raise DouyinCollectorError(f"抖音转写运行环境不可用：{type(exc).__name__}: {exc}") from exc
    if not WHISPER_AVAILABLE:
        raise DouyinCollectorError("本机未安装 faster-whisper，不能把抖音视频转成可用文案样本")
    if not FFMPEG_PATH:
        raise DouyinCollectorError("本机未配置 ffmpeg，不能提取抖音视频音频进行转写")
    return VideoFetcher, download_video, extract_audio, transcribe


def _load_tikhub_transcriber_runtime() -> tuple[Any, Any, str, str]:
    """加载 TikHub 路径需要的本地 FFmpeg/Whisper，不启动浏览器。

    旧的 ``_load_transcriber_runtime`` 会先导入 Playwright 采集器；TikHub
    账号采集不能因为这个兼容依赖不可用而偷偷切回扫码，所以单独提供只
    负责音频处理的加载边界。
    """

    if not MONITOR_ROOT.exists():
        raise DouyinCollectorError("未找到项目内 services/douyin-monitor，无法加载本地转写运行时")
    monitor_site_packages = MONITOR_ROOT / ".venv" / "Lib" / "site-packages"
    if monitor_site_packages.is_dir() and str(monitor_site_packages) not in sys.path:
        sys.path.insert(0, str(monitor_site_packages))
    root_text = str(MONITOR_ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        from transcriber import FFMPEG_PATH, WHISPER_AVAILABLE, extract_audio, transcribe
    except Exception as exc:
        raise DouyinCollectorError(f"TikHub 本地音频转写运行环境不可用：{type(exc).__name__}: {exc}") from exc
    if not WHISPER_AVAILABLE:
        raise DouyinCollectorError("本机未安装 faster-whisper，不能把 TikHub 视频转成作品样本")
    if not FFMPEG_PATH:
        raise DouyinCollectorError("本机未配置 ffmpeg，不能提取 TikHub 视频音频")
    return extract_audio, transcribe, str(FFMPEG_PATH), str(shutil.which("ffprobe") or "")


def _tikhub_account_id(value: str, sec_user_id: str) -> str:
    """将账号身份归一为可写入 WorkCard 的稳定编号。"""

    raw = str(value or sec_user_id or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_-")[:64]
    if safe:
        return safe
    return "creator_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _tikhub_checkpoint_payload(
    *,
    account_id: str,
    sec_user_id: str,
    max_cursor: str,
    aweme_id: str,
    completed_work_ids: set[str],
    transcript_status: Mapping[str, str],
    source_id: str = "",
    evidence_set_id: str = "",
    candidate_videos: Sequence[Mapping[str, Any]] = (),
    candidate_inventory_complete: bool = False,
) -> dict[str, object]:
    """只生成可恢复所需的稳定字段，不保存临时媒体 URL。

    ``candidate_videos`` 是一次发现阶段生成的本地作品清单。它只保存
    aweme_id、分享链接、标题、时长等稳定字段，恢复处理阶段无需再次
    购买主页分页请求；临时 video_url 明确排除。
    """

    inventory: list[dict[str, object]] = []
    seen_inventory: set[str] = set()
    for item in candidate_videos:
        if not isinstance(item, Mapping):
            continue
        aweme_id = str(item.get("aweme_id") or item.get("video_id") or "").strip()
        if not aweme_id or aweme_id in seen_inventory:
            continue
        seen_inventory.add(aweme_id)
        inventory.append({
            "aweme_id": aweme_id,
            "title": str(item.get("title") or "").strip(),
            "share_url": str(item.get("share_url") or item.get("source_url") or "").strip(),
            "duration_ms": int(item.get("duration_ms") or round(float(item.get("duration_seconds") or 0) * 1000)),
            "create_time": int(item.get("create_time") or 0),
            "author_name": str(item.get("author_name") or item.get("author") or "").strip(),
            "cover_url": str(item.get("cover_url") or "").strip(),
            "media_source": str(item.get("media_source") or "video").strip(),
        })

    return {
        "account_id": str(account_id or "").strip(),
        "sec_user_id": str(sec_user_id or "").strip(),
        "max_cursor": str(max_cursor or "0"),
        "aweme_id": str(aweme_id or "").strip(),
        "completed_work_ids": sorted({str(value).strip() for value in completed_work_ids if str(value).strip()}),
        "transcript_status": {
            str(key).strip(): str(value).strip()
            for key, value in transcript_status.items()
            if str(key).strip() and str(value).strip()
        },
        "source_id": str(source_id or "").strip(),
        "evidence_set_id": str(evidence_set_id or "").strip(),
        "candidate_videos": inventory,
        "candidate_inventory_complete": bool(candidate_inventory_complete),
    }


def _tikhub_transcript_text(segments: Any) -> str:
    """把本地 Whisper 的多种返回形状归一为可审计正文。"""

    if isinstance(segments, str):
        return segments.strip()
    if isinstance(segments, Mapping):
        segments = [segments]
    try:
        values = list(segments or [])
    except TypeError:
        return ""
    return "\n".join(
        str(item.get("text") or "").strip()
        for item in values
        if isinstance(item, Mapping) and str(item.get("text") or "").strip()
    ).strip()


def _tikhub_item(video: TikHubVideo, *, transcript: str = "", transcript_status: str = "pending", source_id: str = "") -> dict[str, object]:
    try:
        published_at = datetime.fromtimestamp(int(video.create_time), tz=UTC).isoformat() if video.create_time else ""
    except (TypeError, ValueError, OSError):
        published_at = ""
    return {
        "platform": "douyin",
        "source_url": video.public_url,
        "aweme_id": video.aweme_id,
        "video_id": video.aweme_id,
        "title": video.title,
        "author": video.author_name,
        "published_at": published_at,
        "duration_seconds": round(float(video.duration_ms or 0) / 1000, 3),
        "transcript": str(transcript or "").strip(),
        "transcript_status": transcript_status,
        "media_source": video.media_source,
        "source_id": str(source_id or "").strip(),
    }


def _resolve_tikhub_transcriber_runtime(runtime: Any | None) -> tuple[Any, Any, str, str]:
    """解析测试注入或本机的 ``(extract_audio, transcribe, ffmpeg, ffprobe)``。"""

    if runtime is None:
        return _load_tikhub_transcriber_runtime()
    if isinstance(runtime, Mapping):
        extract_audio = runtime.get("extract_audio")
        transcribe = runtime.get("transcribe")
        if not callable(extract_audio) or not callable(transcribe):
            raise DouyinCollectorError("注入的 TikHub 转写运行时缺少 extract_audio/transcribe")
        return (
            extract_audio,
            transcribe,
            str(runtime.get("ffmpeg_path") or ""),
            str(runtime.get("ffprobe_path") or ""),
        )
    try:
        values = list(runtime)
    except TypeError as exc:
        raise DouyinCollectorError("注入的 TikHub 转写运行时格式无效") from exc
    if len(values) < 2 or not callable(values[0]) or not callable(values[1]):
        raise DouyinCollectorError("注入的 TikHub 转写运行时至少需要 extract_audio 和 transcribe")
    return values[0], values[1], str(values[2] or "") if len(values) > 2 else "", str(values[3] or "") if len(values) > 3 else ""


def collect_creator_tikhub(
    creator_url: str,
    *,
    limit: int = 50,
    target_samples: int | None = None,
    crawl_limit: int | None = None,
    max_video_duration_seconds: int | None = None,
    progress_reporter: ProgressReporter | None = None,
    checkpoint: Mapping[str, Any] | None = None,
    checkpoint_reporter: Callable[[dict[str, object]], None] | None = None,
    item_reporter: ItemReporter | None = None,
    adapter: TikHubDouyinAdapter | None = None,
    transcriber_runtime: Any | None = None,
    account_id: str = "",
    creator_name: str = "",
    source_id: str = "",
    evidence_set_id: str = "",
    # TikHub 单次允许的最大条数；分页阶段再按游标补齐 URL 清单。
    page_size: int = 20,
    max_pages: int = 100,
) -> dict[str, object]:
    """TikHub 默认的账号采集闭环。

    该函数只使用 TikHub 账号接口和本地 FFmpeg/Whisper。任何 TikHub 失败
    都会返回明确的 blocked/partial 报告，绝不调用旧浏览器或扫码回退。
    """

    values = dict(checkpoint or {}) if isinstance(checkpoint, Mapping) else {}
    target = max(1, min(int(target_samples or limit or 50), 100))
    requested_limit = max(1, min(int(limit or target), 100))
    crawl = max(requested_limit, min(200, int(crawl_limit or max(requested_limit, target * 2))))
    duration_limit = _normalize_video_duration_limit_seconds(max_video_duration_seconds)
    source_id = str(source_id or values.get("source_id") or "").strip()
    evidence_set_id = str(evidence_set_id or values.get("evidence_set_id") or "").strip()
    completed: set[str] = {
        str(item).strip()
        for item in (values.get("completed_work_ids") or [])
        if str(item).strip()
    } if isinstance(values.get("completed_work_ids"), (list, tuple, set)) else set()
    transcript_status: dict[str, str] = {
        str(key).strip(): str(value).strip()
        for key, value in (values.get("transcript_status") or {}).items()
        if str(key).strip() and str(value).strip()
    } if isinstance(values.get("transcript_status"), Mapping) else {}
    valid_count = sum(1 for key in completed if transcript_status.get(key) == "local_whisper")
    cursor = str(values.get("max_cursor") or "0")
    current_aweme_id = str(values.get("aweme_id") or "").strip()
    page_count = 0
    candidate_count = 0
    listed_videos: list[TikHubVideo] = []
    inventory_complete = bool(values.get("candidate_inventory_complete") is True)
    # 优先复用已经付费获取并持久化的作品清单。恢复时不重新请求主页分页。
    saved_inventory = values.get("candidate_videos")
    if isinstance(saved_inventory, list):
        for raw in saved_inventory:
            if not isinstance(raw, Mapping):
                continue
            aweme_id = str(raw.get("aweme_id") or "").strip()
            if not aweme_id or any(item.aweme_id == aweme_id for item in listed_videos):
                continue
            listed_videos.append(TikHubVideo(
                aweme_id=aweme_id,
                title=str(raw.get("title") or f"抖音作品 {aweme_id}"),
                share_url=str(raw.get("share_url") or ""),
                duration_ms=int(raw.get("duration_ms") or 0),
                create_time=int(raw.get("create_time") or 0),
                author_name=str(raw.get("author_name") or ""),
                cover_url=str(raw.get("cover_url") or ""),
                media_source=str(raw.get("media_source") or "video"),
            ))
        candidate_count = len(listed_videos)
    items: list[dict[str, object]] = []
    skipped_long_videos: list[dict[str, object]] = []
    warnings: list[str] = []
    adapter = adapter or TikHubDouyinAdapter()

    try:
        sec_user_id = str(values.get("sec_user_id") or "").strip() or adapter.resolve_sec_user_id(creator_url)
    except TikHubDouyinAdapterError as exc:
        return {
            "collection_status": "blocked",
            "platform": "douyin",
            "creator_url": str(creator_url or "").strip(),
            "creator_id": "",
            "items": [],
            "warnings": [str(exc)],
            "diagnostics": {"collector": "douyin_tikhub_adapter", "error_code": exc.code, "tikhub": adapter.diagnostics()},
            "checkpoint": _tikhub_checkpoint_payload(
                account_id=_tikhub_account_id(account_id, ""), sec_user_id="", max_cursor=cursor,
                aweme_id=current_aweme_id, completed_work_ids=completed, transcript_status=transcript_status,
                source_id=source_id, evidence_set_id=evidence_set_id,
            ),
        }

    durable_account_id = _tikhub_account_id(account_id or values.get("account_id", ""), sec_user_id)
    if progress_reporter is not None:
        progress_reporter("creator_resolved", 8, "TikHub 已识别抖音主页，准备分页读取作品")

    def save_checkpoint(*, aweme_id: str = "", cursor_value: str | None = None, inventory_done: bool | None = None) -> None:
        if checkpoint_reporter is None:
            return
        checkpoint_reporter(_tikhub_checkpoint_payload(
            account_id=durable_account_id,
            sec_user_id=sec_user_id,
            max_cursor=cursor if cursor_value is None else str(cursor_value or "0"),
            aweme_id=aweme_id,
            completed_work_ids=completed,
            transcript_status=transcript_status,
            source_id=source_id,
            evidence_set_id=evidence_set_id,
            candidate_videos=[{
                "aweme_id": item.aweme_id, "title": item.title, "share_url": item.share_url,
                "duration_ms": item.duration_ms, "create_time": item.create_time,
                "author_name": item.author_name, "cover_url": item.cover_url,
                "media_source": item.media_source,
            } for item in listed_videos],
            candidate_inventory_complete=inventory_complete if inventory_done is None else bool(inventory_done),
        ))

    # 恢复时若之前已经达到目标，完全不再请求 TikHub；这也是服务重启后
    # 不重复下载/转写的关键门禁。
    if valid_count >= target:
        save_checkpoint(aweme_id="")
        return {
            "collection_status": "completed",
            "platform": "douyin",
            "creator_url": str(creator_url or "").strip(),
            "creator_id": sec_user_id,
            "account_id": durable_account_id,
            "creator_name": str(creator_name or values.get("creator_name") or "").strip(),
            "requested_limit": requested_limit,
            "target_samples": target,
            "crawl_limit": crawl,
            "max_video_duration_seconds": duration_limit,
            "listed_count": 0,
            "raw_listed_count": 0,
            "candidate_count": 0,
            "transcript_ready_count": valid_count,
            "items": [],
            "warnings": ["已从断点确认目标样本，不重复请求或转写"],
            "checkpoint": _tikhub_checkpoint_payload(
                account_id=durable_account_id, sec_user_id=sec_user_id, max_cursor=cursor,
                aweme_id="", completed_work_ids=completed, transcript_status=transcript_status,
                source_id=source_id, evidence_set_id=evidence_set_id,
            ),
            "diagnostics": {"collector": "douyin_tikhub_adapter", "resumed_from_checkpoint": True, "tikhub": adapter.diagnostics()},
        }

    extract_audio = transcribe = None
    ffprobe_path = ""
    collection_error = ""
    stopped_at_target = False
    # 阶段一：先把候选作品清单完整写入断点，再进入下载/转写阶段。
    while not inventory_complete and page_count < max(1, int(max_pages)) and len(listed_videos) < crawl:
        page_cursor = cursor
        try:
            page = adapter.fetch_user_post_videos(sec_user_id, max_cursor=page_cursor, count=min(max(1, int(page_size)), 20))
        except TikHubDouyinAdapterError as exc:
            collection_error = str(exc)
            warnings.append(f"TikHub 作品分页失败：{exc}")
            save_checkpoint(aweme_id=current_aweme_id, cursor_value=page_cursor)
            break
        page_count += 1
        added_page: list[TikHubVideo] = []
        for video in page.items:
            if video.aweme_id in {item.aweme_id for item in listed_videos}:
                continue
            if len(listed_videos) >= crawl:
                break
            listed_videos.append(video)
            added_page.append(video)
        candidate_count += len(added_page)
        # 只保存稳定作品字段，不保存临时媒体 URL；处理阶段重试不再购买主页分页。
        next_cursor = str(page.max_cursor or "").strip()
        # 同时校验 has_more 与游标：官方接口要求后续请求使用 max_cursor；
        # 游标不推进时禁止重复请求，避免重复计费。
        has_more = bool(getattr(page, "has_more", False))
        if len(listed_videos) >= crawl or not has_more or not next_cursor or next_cursor == page_cursor:
            cursor = next_cursor or page_cursor
            inventory_complete = True
        else:
            cursor = next_cursor
        save_checkpoint(aweme_id="", cursor_value=cursor, inventory_done=inventory_complete)
        if progress_reporter is not None:
            progress_reporter("collecting", min(20, 10 + page_count), f"TikHub 已获取第 {page_count} 页，累计候选 {candidate_count} 条")

        if inventory_complete:
            break
    if not inventory_complete and page_count >= max(1, int(max_pages)):
        warnings.append(f"TikHub分页达到本地上限 {max_pages} 页，已保存 URL 清单断点")

    # 阶段二：仅消费本地 URL 清单；每篇完成后立即写入 Obsidian 并更新断点。
    for video in listed_videos:
        if video.aweme_id in completed:
            continue
        current_aweme_id = video.aweme_id
        save_checkpoint(aweme_id=current_aweme_id, cursor_value=cursor)
        duration_seconds = round(float(video.duration_ms or 0) / 1000, 3)
        if duration_seconds > duration_limit:
            transcript_status[video.aweme_id] = "skipped_duration"
            completed.add(video.aweme_id)
            skipped_long_videos.append({
                    "aweme_id": video.aweme_id,
                    "video_id": video.aweme_id,
                    "title": video.title,
                    "duration_seconds": duration_seconds,
                    "reason": f"超过 {_duration_limit_label(duration_limit)}，跳过下载和转写",
            })
            save_checkpoint(aweme_id="", cursor_value=cursor)
            continue
        try:
            if extract_audio is None or transcribe is None:
                    extract_audio, transcribe, _ffmpeg_path, ffprobe_path = _resolve_tikhub_transcriber_runtime(transcriber_runtime)
            if progress_reporter is not None:
                    progress_reporter("downloading", 25, f"正在下载作品 {video.aweme_id}")
            with tempfile.TemporaryDirectory(prefix="tikhub_douyin_") as temporary:
                root = Path(temporary)
                video_path = root / "source.mp4"
                audio_path = root / "source.wav"
                media_url = adapter.fetch_video_media_url(video.aweme_id, share_url=video.public_url, detail=video)
                adapter.download_media(
                        video.aweme_id,
                        video_path,
                        media_url=media_url,
                        share_url=video.public_url,
                        expected_duration_seconds=duration_seconds or None,
                        ffprobe_path=ffprobe_path,
                )
                if progress_reporter is not None:
                        progress_reporter("extracting_audio", 40, f"正在提取作品 {video.aweme_id} 音频")
                extract_audio(str(video_path), str(audio_path))
                if progress_reporter is not None:
                        progress_reporter("transcribing", 60, f"正在用本地 Whisper 转写作品 {video.aweme_id}")
                text = _tikhub_transcript_text(transcribe(str(audio_path)))
            if len(text) < MIN_TIKHUB_TRANSCRIPT_CHARS:
                transcript_status[video.aweme_id] = "transcript_missing"
                report_item = _tikhub_item(video, transcript=text, transcript_status="transcript_missing", source_id=source_id)
                items.append(report_item)
            else:
                report_item = _tikhub_item(video, transcript=text, transcript_status="local_whisper", source_id=source_id)
                    # 先完成逐篇 Obsidian 落盘，再把 aweme_id 标为 completed，
                    # 任何存储异常都会保留断点并允许下次重试。
                if item_reporter is not None:
                    item_reporter({**report_item, "creator_id": sec_user_id})
                items.append(report_item)
                transcript_status[video.aweme_id] = "local_whisper"
                valid_count += 1
            completed.add(video.aweme_id)
            if transcript_status.get(video.aweme_id) != "local_whisper":
                    # 失败/空文本也已完成一次处理，恢复时不会重复下载；
                    # 作品卡桥接会按长度门槛排除它。
                completed.add(video.aweme_id)
            current_aweme_id = ""
            save_checkpoint(aweme_id="", cursor_value=cursor)
            if valid_count >= target:
                stopped_at_target = True
                break
        except Exception as exc:
            transcript_status[video.aweme_id] = "failed"
            warnings.append(f"{video.aweme_id}: TikHub媒体/转写失败（{type(exc).__name__}: {exc}）")
                # 失败项不加入 completed_work_ids，允许用户从断点明确重试。
            save_checkpoint(aweme_id=video.aweme_id, cursor_value=cursor)
        if stopped_at_target:
            break

    if page_count >= max(1, int(max_pages)) and not stopped_at_target:
        warnings.append(f"TikHub分页达到本地上限 {max_pages} 页，已保存断点")
    creator_name = str(creator_name or values.get("creator_name") or "").strip()
    if not creator_name:
        creator_name = next((video.author_name for video in listed_videos if video.author_name), "")
    if collection_error and not items:
        collection_status = "blocked"
    elif collection_error or warnings:
        collection_status = "partial"
    else:
        collection_status = "completed"
    final_checkpoint = _tikhub_checkpoint_payload(
        account_id=durable_account_id, sec_user_id=sec_user_id, max_cursor=cursor,
        aweme_id=current_aweme_id, completed_work_ids=completed, transcript_status=transcript_status,
        source_id=source_id, evidence_set_id=evidence_set_id,
        candidate_videos=[{
            "aweme_id": item.aweme_id, "title": item.title, "share_url": item.share_url,
            "duration_ms": item.duration_ms, "create_time": item.create_time,
            "author_name": item.author_name, "cover_url": item.cover_url,
            "media_source": item.media_source,
        } for item in listed_videos],
        candidate_inventory_complete=inventory_complete,
    )
    return {
        "collection_status": collection_status,
        "platform": "douyin",
        "creator_url": str(creator_url or "").strip(),
        "creator_id": sec_user_id,
        "account_id": durable_account_id,
        "creator_name": creator_name,
        "requested_limit": requested_limit,
        "target_samples": target,
        "crawl_limit": crawl,
        "max_video_duration_seconds": duration_limit,
        "listed_count": len(listed_videos),
        "raw_listed_count": candidate_count,
        "candidate_count": candidate_count,
        "eligible_candidate_count": max(0, candidate_count - len(skipped_long_videos)),
        "skipped_long_video_count": len(skipped_long_videos),
        "skipped_long_videos": skipped_long_videos,
        "transcript_ready_count": valid_count,
        "items": items,
        "warnings": warnings,
        "checkpoint": final_checkpoint,
        "diagnostics": {
            "collector": "douyin_tikhub_adapter",
            "provider": "TikHub",
            "sec_user_id": sec_user_id,
            "page_count": page_count,
            "max_cursor": cursor,
            "stopped_at_target": stopped_at_target,
            "completed_work_ids": sorted(completed),
            "transcript_status": dict(transcript_status),
            "error": collection_error,
            "tikhub": adapter.diagnostics(),
        },
    }


def _run_fetch(
    spider: Any,
    sec_uid: str,
    *,
    timeout_seconds: int = 45,
    diagnostics: dict[str, Any] | None = None,
) -> list[Any]:
    started = time.monotonic()
    if diagnostics is not None:
        diagnostics.update({
            "phase": "作品列表采集",
            "timeout_seconds": timeout_seconds,
            "fetch_started_at": datetime.now(UTC).isoformat(),
            "fetch_status": "running",
        })
    async def fetch_with_deadline() -> list[Any]:
        try:
            videos = await asyncio.wait_for(spider.fetch(sec_uid), timeout=max(1, int(timeout_seconds)))
            if diagnostics is not None:
                diagnostics.update({
                    "fetch_status": "returned",
                    "fetch_elapsed_seconds": round(time.monotonic() - started, 2),
                    "video_count": len(videos or []),
                    "spider_error": getattr(spider, "_error", None),
                    "browser_executable_path": getattr(spider, "_browser_executable_path", None),
                    "scroll_count": getattr(spider, "_scroll_count", None),
                    "api_pages": getattr(spider, "_api_pages", None),
                    "next_cursor": str(getattr(spider, "_next_cursor", "") or ""),
                    "has_more": getattr(spider, "_has_more", None),
                    "last_api_url": str(getattr(spider, "_last_api_url", "") or "")[:500],
                    "profile_detected": bool(getattr(spider, "profile", None)),
                })
            return videos
        except TimeoutError as exc:
            partial_videos = list(getattr(spider, "videos", []) or [])
            if diagnostics is not None:
                diagnostics.update({
                    "fetch_status": "partial_timeout" if partial_videos else "timeout",
                    "fetch_elapsed_seconds": round(time.monotonic() - started, 2),
                    "video_count": len(partial_videos),
                    "partial_video_count": len(partial_videos),
                    "spider_error": getattr(spider, "_error", None),
                    "browser_executable_path": getattr(spider, "_browser_executable_path", None),
                    "scroll_count": getattr(spider, "_scroll_count", None),
                    "api_pages": getattr(spider, "_api_pages", None),
                    "next_cursor": str(getattr(spider, "_next_cursor", "") or ""),
                    "has_more": getattr(spider, "_has_more", None),
                    "last_api_url": str(getattr(spider, "_last_api_url", "") or "")[:500],
                    "profile_detected": bool(getattr(spider, "profile", None)),
                    "likely_causes": ["登录态/风控导致作品接口未返回", "浏览器页面加载或 Playwright 启动卡住", "作品列表滚动等待未结束"],
                })
            if partial_videos:
                return partial_videos
            raise DouyinCollectorError(
                f"读取抖音主页作品列表超时（{timeout_seconds} 秒）；请检查本机抖音登录态、网络或风控状态后重试"
            ) from exc

    try:
        return asyncio.run(fetch_with_deadline())
    except RuntimeError as exc:
        if "asyncio.run" not in str(exc):
            raise DouyinCollectorError(f"抖音主页采集失败：{exc}") from exc
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(spider.fetch(sec_uid))
        finally:
            loop.close()


def fetch_creator_name(creator_url: str, *, spider_factory: Callable[[], Any] | None = None) -> str:
    """Read the verified nickname from the same logged-in homepage collector."""
    canonical_creator_url = resolve_creator_url(creator_url)
    sec_uid = parse_creator_url(canonical_creator_url)
    if spider_factory is None:
        spider_cls = _load_spider_runtime()
        spider_factory = lambda: spider_cls(headless=True, max_scrolls=1, idle_limit=1)
    spider = spider_factory()
    _run_fetch(spider, sec_uid)
    nickname = str(getattr(getattr(spider, "profile", None), "nickname", "") or "").strip()
    if not nickname:
        raise DouyinCollectorError("未能从达人主页读取博主昵称")
    return nickname


def _to_item(video: Any, transcript: str = "", transcript_status: str = "pending") -> dict[str, Any]:
    created = getattr(video, "create_time", 0)
    try:
        published_at = datetime.fromtimestamp(int(created), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        published_at = ""
    video_id = str(getattr(video, "video_id", ""))
    return {
        "platform": "douyin",
        "source_url": f"https://www.douyin.com/video/{video_id}",
        "video_id": video_id,
        "title": str(getattr(video, "title", "") or "").strip(),
        "published_at": published_at,
        "duration_seconds": round(float(getattr(video, "duration_ms", 0) or 0) / 1000, 2),
        "transcript": transcript,
        "transcript_status": transcript_status,
    }


def _valid_transcript_count(transcripts: dict[str, str]) -> int:
    """Count transcripts that meet the minimum text length for style analysis."""
    return sum(1 for value in transcripts.values() if len(value) >= 80)


def _normalize_video_duration_limit_seconds(value: object | None) -> int:
    """Return a bounded duration policy while preserving legacy callers."""

    if value in (None, ""):
        return DEFAULT_STYLE_VIDEO_DURATION_SECONDS
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise DouyinCollectorError("视频时长筛选值必须是秒数") from exc
    if not 1 <= seconds <= DEFAULT_STYLE_VIDEO_DURATION_SECONDS:
        raise DouyinCollectorError("视频时长筛选范围必须在 1 秒到 10 分钟之间")
    return seconds


def _duration_limit_label(seconds: int) -> str:
    minutes = seconds / 60
    return f"{minutes:g} 分钟" if seconds % 60 == 0 else f"{seconds} 秒"


def _filter_style_videos(
    videos: Sequence[Any], *, max_video_duration_seconds: int | None = None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Filter before download, audio extraction, or Whisper transcription."""

    duration_limit_seconds = _normalize_video_duration_limit_seconds(max_video_duration_seconds)
    duration_limit_ms = duration_limit_seconds * 1000
    duration_label = _duration_limit_label(duration_limit_seconds)
    eligible: list[Any] = []
    skipped: list[dict[str, Any]] = []
    for video in videos:
        duration_ms = max(0, int(getattr(video, "duration_ms", 0) or 0))
        if duration_ms > duration_limit_ms:
            skipped.append({
                "video_id": str(getattr(video, "video_id", "") or ""),
                "title": str(getattr(video, "title", "") or ""),
                "duration_ms": duration_ms,
                "duration_minutes": round(duration_ms / 60000, 2),
                "reason": f"超过 {duration_label}，跳过转写",
            })
            continue
        eligible.append(video)
    return eligible, skipped


def _checkpoint_candidate(video: Any) -> dict[str, Any]:
    """Return only the public fields needed to resume a local transcription."""
    return {
        "video_id": str(getattr(video, "video_id", "") or ""),
        "title": str(getattr(video, "title", "") or ""),
        "create_time": getattr(video, "create_time", 0) or 0,
        "duration_ms": getattr(video, "duration_ms", 0) or 0,
        "video_url": str(getattr(video, "video_url", "") or ""),
    }


def _restore_checkpoint_candidates(value: object) -> list[Any]:
    if not isinstance(value, list):
        return []
    candidates: list[Any] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        video_id = str(item.get("video_id") or "").strip()
        if not video_id:
            continue
        candidates.append(
            SimpleNamespace(
                video_id=video_id,
                title=str(item.get("title") or ""),
                create_time=item.get("create_time") or 0,
                duration_ms=item.get("duration_ms") or 0,
                video_url=str(item.get("video_url") or ""),
            )
        )
    return candidates


def _checkpoint_transcripts(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(video_id): str(text)
        for video_id, text in value.items()
        if str(video_id).strip() and isinstance(text, str)
    }


def _checkpoint_warnings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _transcribe_videos(
    videos: Sequence[Any], *, target_samples: int | None = None, progress_reporter: ProgressReporter | None = None,
    transcription_diagnostics: dict[str, Any] | None = None, resume_transcripts: Mapping[str, str] | None = None,
    resume_warnings: Sequence[str] | None = None, resume_processed_count: int = 0,
    checkpoint_reporter: TranscriptionCheckpointReporter | None = None,
    item_reporter: ItemReporter | None = None, creator_id: str = "",
) -> tuple[dict[str, str], list[str]]:
    transcripts = _checkpoint_transcripts(resume_transcripts)
    warnings = _checkpoint_warnings(resume_warnings)
    total = len(videos)
    processed_count = max(0, min(int(resume_processed_count or 0), total))
    target_reached = target_samples is not None and _valid_transcript_count(transcripts) >= target_samples

    def save_checkpoint() -> None:
        if checkpoint_reporter is not None:
            checkpoint_reporter(processed_count, dict(transcripts), list(warnings))

    if target_reached and progress_reporter is not None:
        progress_reporter(
            "transcribing",
            70,
            f"已恢复 {processed_count}/{total} 条候选；有效样本已达到 {_valid_transcript_count(transcripts)}/{target_samples}，无需重复转写",
        )
    elif processed_count < total:
        video_fetcher_cls, download_video, extract_audio, transcribe = _load_transcriber_runtime()
        with video_fetcher_cls() as fetcher:
            for index, video in enumerate(videos[processed_count:], start=processed_count + 1):
                video_id = str(getattr(video, "video_id", ""))
                base_progress = 25 + int(45 * (index - 1) / max(1, total))
                valid_count = _valid_transcript_count(transcripts)
                target_label = str(target_samples) if target_samples is not None else "未设置"
                progress_context = f"有效样本 {valid_count}/{target_label}；候选扫描 {index}/{total}"
                try:
                    if not video_id:
                        warnings.append(f"候选 {index}: 缺少视频编号，已跳过")
                        continue
                    if progress_reporter is not None:
                        progress_reporter("downloading", base_progress, f"正在下载：{progress_context}")
                    with tempfile.TemporaryDirectory(prefix="douyin_style_") as temporary:
                        root = Path(temporary)
                        video_path = root / "source.mp4"
                        audio_path = root / "source.wav"
                        download_video(fetcher, video_id, str(video_path), getattr(video, "video_url", "") or None)
                        if progress_reporter is not None:
                            progress_reporter("extracting_audio", base_progress, f"正在提取音频：{progress_context}")
                        extract_audio(str(video_path), str(audio_path))
                        if progress_reporter is not None:
                            progress_reporter(
                                "transcribing",
                                base_progress,
                                f"正在转写：{progress_context}（首次运行会预热本地 Whisper 模型）",
                            )
                        segments = transcribe(str(audio_path))
                        text = "\n".join(
                            str(segment.get("text", "")).strip()
                            for segment in segments
                            if isinstance(segment, dict) and str(segment.get("text", "")).strip()
                        )
                        if text:
                            transcripts[video_id] = text
                            valid_count = _valid_transcript_count(transcripts)
                            if target_samples is not None and valid_count >= target_samples:
                                target_reached = True
                                if progress_reporter is not None:
                                    progress_reporter(
                                        "transcribing",
                                        base_progress,
                                        f"有效样本已达到 {valid_count}/{target_samples}；已扫描候选 {index}/{total}，停止继续转写",
                                    )
                        else:
                            warnings.append(f"{video_id}: Whisper 未返回文本")
                except Exception as exc:
                    warnings.append(f"{video_id}: 转写失败（{type(exc).__name__}: {exc}）")
                finally:
                    # A forced process exit can lose only the candidate currently being processed.
                    processed_count = index
                    save_checkpoint()
                    if item_reporter is not None:
                        item_payload = _to_item(
                            video,
                            transcripts.get(video_id, ""),
                            "local_whisper" if transcripts.get(video_id, "") else "transcript_missing",
                        )
                        # ``_transcribe_videos`` is a standalone helper; the
                        # creator identifier must be passed explicitly rather
                        # than relying on ``collect_creator``'s local ``sec_uid``.
                        item_payload["creator_id"] = creator_id
                        item_reporter(item_payload)
                if target_reached:
                    break
    else:
        save_checkpoint()
    if transcription_diagnostics is not None:
        transcription_diagnostics.update({
            "candidate_total": len(videos),
            "candidate_scanned_count": processed_count,
            "target_samples": target_samples,
            "valid_sample_count": _valid_transcript_count(transcripts),
            "stopped_at_target": target_reached,
        })
    return transcripts, warnings


def collect_creator(
    creator_url: str, *, limit: int = 50, spider_factory: Callable[[], Any] | None = None,
    transcript_provider: TranscriptProvider | None = None, progress_reporter: ProgressReporter | None = None,
    target_samples: int | None = None, crawl_limit: int | None = None, fetch_timeout_seconds: int = 45,
    checkpoint: Mapping[str, Any] | None = None, checkpoint_reporter: CheckpointReporter | None = None,
    max_video_duration_seconds: int | None = None,
    item_reporter: ItemReporter | None = None,
) -> dict[str, Any]:
    canonical_creator_url = resolve_creator_url(creator_url)
    sec_uid = parse_creator_url(canonical_creator_url)
    limit = max(1, min(int(limit), 100))
    target_samples = max(1, min(int(target_samples or limit), 100))
    duration_limit_seconds = _normalize_video_duration_limit_seconds(max_video_duration_seconds)
    duration_limit_label = _duration_limit_label(duration_limit_seconds)
    # A video can fail to download/transcribe or be too short for style analysis.
    # Scan extra candidates automatically, but keep a bounded request footprint.
    crawl_limit = max(limit, min(100, int(crawl_limit or max(limit, target_samples * 2))))
    if progress_reporter is not None:
        progress_reporter("creator_resolved", 8, "已识别抖音主页，准备采集公开作品")
    checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    checkpoint_videos = _restore_checkpoint_candidates(checkpoint.get("candidates"))
    spider = None
    if checkpoint_videos:
        # A checkpoint is a resume cursor, not a frozen candidate universe. Fetch
        # the current homepage again and append unseen video IDs so a partial
        # timeout can actually make progress on the next resume.
        try:
            spider_cls = _load_spider_runtime() if spider_factory is None else None
            if spider_factory is None:
                spider_factory = lambda: spider_cls(headless=True, max_scrolls=80, idle_limit=20)
            spider = spider_factory()
        except DouyinCollectorError:
            spider = None
    if not checkpoint_videos:
        if spider_factory is None:
            try:
                spider_cls = _load_spider_runtime()
            except DouyinCollectorError as exc:
                return {"collection_status": "blocked", "platform": "douyin", "creator_url": canonical_creator_url, "creator_share_input": creator_url, "creator_id": sec_uid, "items": [], "warnings": [str(exc)]}
            spider_factory = lambda: spider_cls(headless=True, max_scrolls=80, idle_limit=20)
        spider = spider_factory()
    diagnostics: dict[str, Any] = {
        "collector": "douyin_style_collector",
        "creator_id": sec_uid,
        "canonical_creator_url": canonical_creator_url,
        "timeout_seconds": max(1, int(fetch_timeout_seconds)),
        "max_video_duration_seconds": duration_limit_seconds,
        "started_at": datetime.now(UTC).isoformat(),
    }
    if checkpoint_videos:
        videos = checkpoint_videos
        creator_name = str(checkpoint.get("creator_name") or "").strip()
        crawl_error = str(checkpoint.get("crawl_error") or "").strip() or None
        diagnostics.update({"resumed_from_checkpoint": True, "fetch_status": checkpoint.get("fetch_status") or "completed"})
        if spider is not None and not (checkpoint.get("target_reached") is True):
            try:
                fetched = _run_fetch(spider, sec_uid, timeout_seconds=fetch_timeout_seconds, diagnostics=diagnostics)
                known = {str(getattr(video, "video_id", "") or "") for video in videos}
                for video in list(fetched or []):
                    video_id = str(getattr(video, "video_id", "") or "")
                    if video_id and video_id not in known:
                        videos.append(video)
                        known.add(video_id)
                videos = videos[:crawl_limit]
                creator_name = creator_name or str(getattr(getattr(spider, "profile", None), "nickname", "") or "").strip()
                crawl_error = crawl_error or getattr(spider, "_error", None)
            except DouyinCollectorError as exc:
                crawl_error = crawl_error or str(exc)
    else:
        if progress_reporter is not None:
            progress_reporter("collecting", 15, "正在读取主页作品列表")
        try:
            videos = _run_fetch(
                spider, sec_uid, timeout_seconds=fetch_timeout_seconds, diagnostics=diagnostics,
            )[:crawl_limit]
        except DouyinCollectorError as exc:
            diagnostics.update({"finished_at": datetime.now(UTC).isoformat(), "error": str(exc)})
            return {
                "collection_status": "blocked", "platform": "douyin", "creator_url": canonical_creator_url,
                "creator_share_input": creator_url, "creator_id": sec_uid, "items": [],
                "warnings": [str(exc)], "diagnostics": diagnostics,
            }
        creator_name = str(getattr(getattr(spider, "profile", None), "nickname", "") or "").strip()
        crawl_error = getattr(spider, "_error", None)
    raw_videos = list(videos)
    raw_listed_count = len(raw_videos)
    videos, skipped_long_videos = _filter_style_videos(
        videos, max_video_duration_seconds=duration_limit_seconds,
    )
    raw_processed_count = max(0, min(int(checkpoint.get("processed_count") or 0), raw_listed_count))
    processed_count_after_filter = len(_filter_style_videos(
        raw_videos[:raw_processed_count], max_video_duration_seconds=duration_limit_seconds,
    )[0])
    if skipped_long_videos:
        diagnostics["skipped_long_video_count"] = len(skipped_long_videos)
        diagnostics["skipped_long_videos"] = skipped_long_videos
    if checkpoint_reporter is not None:
        checkpoint_reporter({
            "creator_name": creator_name,
            "candidates": [_checkpoint_candidate(video) for video in videos],
            "fetch_status": diagnostics.get("fetch_status") or "completed",
            "crawl_error": str(crawl_error or ""),
            "processed_count": processed_count_after_filter,
            "transcripts": _checkpoint_transcripts(checkpoint.get("transcripts")),
            "warnings": _checkpoint_warnings(checkpoint.get("warnings")),
        })
    if not videos:
        return {
            "collection_status": "blocked", "platform": "douyin", "creator_url": canonical_creator_url, "creator_share_input": creator_url, "creator_id": sec_uid,
            "items": [], "warnings": [
                *([f"已跳过 {len(skipped_long_videos)} 条超过 {duration_limit_label} 的视频"] if skipped_long_videos else []),
                str(crawl_error or "未采集到可转写作品；请检查抖音登录态、主页可访问性或风控状态"),
            ],
            "diagnostics": {**diagnostics, "finished_at": datetime.now(UTC).isoformat(), "error": str(crawl_error or "未采集到作品")},
        }
    warnings: list[str] = _checkpoint_warnings(checkpoint.get("warnings"))
    if skipped_long_videos:
        warnings.append(f"已跳过 {len(skipped_long_videos)} 条超过 {duration_limit_label} 的视频，避免长视频阻塞风格转写")
    if diagnostics.get("fetch_status") == "partial_timeout":
        warnings.append(
            f"主页采集达到 {diagnostics.get('timeout_seconds', fetch_timeout_seconds)} 秒上限，已保留 {len(videos)} 条已获取作品并继续处理"
        )
    if crawl_error:
        warnings.append(f"主页采集器报告告警，但已保留已获取作品：{crawl_error}")
    transcripts: dict[str, str] = _checkpoint_transcripts(checkpoint.get("transcripts"))
    transcription_diagnostics: dict[str, Any] = {}
    if progress_reporter is not None:
        progress_reporter(
            "collected", 22,
            f"已获取 {len(videos)} 条可转写候选作品；跳过超过 {duration_limit_label} 的视频 {len(skipped_long_videos)} 条；参考上限是 {target_samples} 条有效转写样本",
        )
    if transcript_provider is not None:
        processed_count = max(0, min(processed_count_after_filter, len(videos)))
        target_reached = target_samples is not None and _valid_transcript_count(transcripts) >= target_samples
        remaining_videos = [] if target_reached else videos[processed_count:]
        for index, video in enumerate(remaining_videos, start=processed_count + 1):
            processed_count = index
            try:
                text = transcript_provider(video)
                video_id = str(getattr(video, "video_id", ""))
                transcripts[video_id] = text
                if item_reporter is not None:
                    item_payload = _to_item(video, text, "local_whisper" if text else "transcript_missing")
                    item_payload["creator_id"] = sec_uid
                    item_reporter(item_payload)
                if len(text) >= 80 and _valid_transcript_count(transcripts) >= target_samples:
                    target_reached = True
                    break
            except Exception as exc:
                warnings.append(f"{getattr(video, 'video_id', '')}: 转写失败（{type(exc).__name__}: {exc}）")
            if checkpoint_reporter is not None:
                checkpoint_reporter({
                    "creator_name": creator_name,
                    "candidates": [_checkpoint_candidate(item) for item in videos],
                    "fetch_status": diagnostics.get("fetch_status") or "completed",
                    "crawl_error": str(crawl_error or ""),
                    "processed_count": processed_count,
                    "transcripts": dict(transcripts),
                    "warnings": list(warnings),
                })
        transcription_diagnostics.update({
            "candidate_total": len(videos),
            "candidate_scanned_count": processed_count,
            "target_samples": target_samples,
            "valid_sample_count": _valid_transcript_count(transcripts),
            "stopped_at_target": target_reached,
        })
    else:
        try:
            transcripts, transcription_warnings = _transcribe_videos(
                videos, target_samples=target_samples, progress_reporter=progress_reporter,
                transcription_diagnostics=transcription_diagnostics,
                resume_transcripts=checkpoint.get("transcripts"), resume_warnings=checkpoint.get("warnings"),
                resume_processed_count=processed_count_after_filter,
                checkpoint_reporter=lambda processed_count, saved_transcripts, saved_warnings: checkpoint_reporter({
                    "creator_name": creator_name,
                    "candidates": [_checkpoint_candidate(item) for item in videos],
                    "fetch_status": diagnostics.get("fetch_status") or "completed",
                    "crawl_error": str(crawl_error or ""),
                    "processed_count": processed_count,
                    "transcripts": saved_transcripts,
                    "warnings": saved_warnings,
                    "target_reached": _valid_transcript_count(saved_transcripts) >= (target_samples or 0),
                }) if checkpoint_reporter is not None else None,
                item_reporter=item_reporter,
                creator_id=sec_uid,
            )
            warnings.extend(transcription_warnings)
        except DouyinCollectorError as exc:
            warnings.append(str(exc))
    items = [
        _to_item(video, transcripts.get(str(getattr(video, "video_id", "")), ""), "local_whisper" if str(getattr(video, "video_id", "")) in transcripts else "transcript_missing")
        for video in videos
    ]
    if progress_reporter is not None:
        progress_reporter(
            "transcribed", 72,
            f"转写完成：有效样本 {transcription_diagnostics.get('valid_sample_count', 0)}/{target_samples}；"
            f"候选扫描 {transcription_diagnostics.get('candidate_scanned_count', 0)}/{len(videos)}",
        )
    collection_status = "partial" if diagnostics.get("fetch_status") == "partial_timeout" or crawl_error else "completed"
    return {
        "collection_status": collection_status, "platform": "douyin", "creator_url": canonical_creator_url, "creator_share_input": creator_url, "creator_id": sec_uid,
        "creator_name": creator_name,
        "requested_limit": limit, "target_samples": target_samples, "crawl_limit": crawl_limit,
        "max_video_duration_seconds": duration_limit_seconds,
        "listed_count": len(videos), "raw_listed_count": raw_listed_count,
        "skipped_long_video_count": len(skipped_long_videos), "skipped_long_videos": skipped_long_videos,
        "transcript_ready_count": sum(1 for item in items if len(item["transcript"]) >= 80), "items": items, "warnings": warnings,
        "diagnostics": {
            **diagnostics,
            "transcription": transcription_diagnostics,
            "finished_at": datetime.now(UTC).isoformat(),
            "fetch_status": diagnostics.get("fetch_status") or "completed",
            "partial_result": collection_status == "partial",
            "candidate_count": raw_listed_count,
            "eligible_candidate_count": len(videos),
        },
    }


def collect_case_video(video_url: str) -> dict[str, Any]:
    canonical_video_url = resolve_case_video_url(video_url)
    video_id = parse_video_url(canonical_video_url)
    class _CaseVideo:
        def __init__(self) -> None:
            self.video_id = video_id
            self.title = ""
            self.duration_ms = 0
            self.create_time = 0
            self.video_url = ""
    transcripts, warnings = _transcribe_videos([_CaseVideo()])
    text = transcripts.get(video_id, "")
    return {
        **_to_item(_CaseVideo(), text, "local_whisper" if text else "transcript_missing"),
        "warnings": warnings,
        "source_url": canonical_video_url,
    }


__all__ = [
    "DouyinCollectorError",
    "collect_case_video",
    "collect_creator",
    "collect_creator_tikhub",
    "extract_douyin_share_url",
    "fetch_creator_name",
    "parse_creator_url",
    "parse_video_url",
    "resolve_case_video_url",
    "resolve_creator_url",
]
