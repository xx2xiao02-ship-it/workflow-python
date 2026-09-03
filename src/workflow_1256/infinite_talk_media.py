"""Media transports supplied with the InfiniteTalk plugin family.

Implements the observable contracts of nodes 175652, 143380 and 160040.
The default processor uses ffmpeg and a configured Volcengine TOS bucket;
tests and callers may inject ``splitter``, ``merger`` and ``uploader`` so no
network or fake URL is used implicitly.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class InfiniteTalkMediaError(RuntimeError):
    """Media processing or upload was not possible."""


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _list(value: Any) -> list[Any]:
    if isinstance(value, str):
        import json
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InfiniteTalkMediaError("输入数组 JSON 无效") from exc
        value = parsed
    if not isinstance(value, list):
        raise InfiniteTalkMediaError("输入必须是数组")
    return list(value)


def _timeline(item: Any) -> tuple[int, int]:
    start = int(_value(item, "start", 0) or 0)
    end = int(_value(item, "end", 0) or 0)
    if end <= start:
        raise InfiniteTalkMediaError(f"时间线无效：start={start}, end={end}")
    return start, end


@dataclass(frozen=True)
class MediaConfig:
    ffmpeg_path: str = ""
    tos_access_key: str = ""
    tos_secret_key: str = ""
    tos_bucket: str = ""
    tos_endpoint: str = "https://tos-cn-beijing.volces.com"
    tos_region: str = "cn-beijing"

    @classmethod
    def from_env(cls) -> "MediaConfig":
        return cls(
            ffmpeg_path=os.environ.get("INFINITETALK_FFMPEG_PATH", "").strip(),
            tos_access_key=os.environ.get("INFINITETALK_TOS_ACCESS_KEY", "").strip(),
            tos_secret_key=os.environ.get("INFINITETALK_TOS_SECRET_KEY", "").strip(),
            tos_bucket=os.environ.get("INFINITETALK_TOS_BUCKET", "").strip(),
            tos_endpoint=os.environ.get("INFINITETALK_TOS_ENDPOINT", "https://tos-cn-beijing.volces.com").strip(),
            tos_region=os.environ.get("INFINITETALK_TOS_REGION", "cn-beijing").strip(),
        )


Splitter = Callable[[str, int, int, str], bytes]
Merger = Callable[[list[str]], tuple[bytes, float]]
Uploader = Callable[[bytes, str, str], Mapping[str, Any]]


class InfiniteTalkMediaTransport:
    def __init__(self, config: MediaConfig | None = None, *, splitter: Splitter | None = None, merger: Merger | None = None, uploader: Uploader | None = None, requests_module: Any | None = None) -> None:
        self.config = config or MediaConfig.from_env()
        self._splitter = splitter
        self._merger = merger
        self._uploader = uploader
        if requests_module is None:
            import requests as requests_module  # type: ignore
        self.http = requests_module

    def _upload(self, content: bytes, filename: str, mime: str) -> Mapping[str, Any]:
        if self._uploader is not None:
            return self._uploader(content, filename, mime)
        if not (self.config.tos_access_key and self.config.tos_secret_key and self.config.tos_bucket):
            raise InfiniteTalkMediaError("未配置 INFINITETALK_TOS_ACCESS_KEY/SECRET_KEY/BUCKET")
        try:
            import tos  # type: ignore
            client = tos.TosClientV2(self.config.tos_access_key, self.config.tos_secret_key, self.config.tos_endpoint, self.config.tos_region)
            client.put_object(self.config.tos_bucket, filename, content=content, content_type=mime)
        except Exception as exc:
            raise InfiniteTalkMediaError("TOS 上传失败") from exc
        endpoint = self.config.tos_endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        return {"success": True, "url": f"https://{self.config.tos_bucket}.{endpoint}/{filename}", "bucket": self.config.tos_bucket}

    def _download(self, url: str) -> bytes:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise InfiniteTalkMediaError("媒体 URL 必须是 http/https")
        response = self.http.get(url, timeout=120, allow_redirects=True)
        status = int(getattr(response, "status_code", 0) or 0)
        if status < 200 or status >= 300:
            raise InfiniteTalkMediaError(f"媒体下载失败 HTTP {status}")
        return bytes(getattr(response, "content", b""))

    def _ffmpeg(self) -> str:
        if self.config.ffmpeg_path:
            return self.config.ffmpeg_path
        try:
            import imageio_ffmpeg  # type: ignore
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            raise InfiniteTalkMediaError("未配置 ffmpeg；请设置 INFINITETALK_FFMPEG_PATH") from exc

    def _default_split(self, url: str, start: int, end: int, kind: str) -> bytes:
        source = self._download(url)
        ffmpeg = self._ffmpeg()
        with tempfile.TemporaryDirectory(prefix="infinitetalk_split_") as folder:
            suffix = ".mp3" if kind == "audio" else ".mp4"
            src = Path(folder) / ("source" + suffix)
            out = Path(folder) / ("segment" + suffix)
            src.write_bytes(source)
            start_sec = start / 1_000_000
            duration_sec = (end - start) / 1_000_000
            if kind == "audio":
                args = [ffmpeg, "-y", "-ss", str(start_sec), "-i", str(src), "-t", str(duration_sec), "-vn", "-acodec", "libmp3lame", str(out)]
            else:
                args = [ffmpeg, "-y", "-ss", str(start_sec), "-i", str(src), "-t", str(duration_sec), "-c", "copy", str(out)]
            completed = subprocess.run(args, capture_output=True, text=True, timeout=180)
            if completed.returncode != 0 or not out.is_file():
                raise InfiniteTalkMediaError("ffmpeg 切分失败")
            return out.read_bytes()

    def host_audio_split(self, params: Any) -> dict[str, Any]:
        links = [str(v or "").strip() for v in _list(_value(params, "links", []))]
        timelines = _list(_value(params, "host_audio_timeline", []))
        if not links or not timelines:
            return {"status": "failed", "msg": "links 和 host_audio_timeline 不能为空", "url_list": [], "segments": [], "failed_segments": []}
        if len(links) != len(timelines):
            return {"status": "failed", "msg": f"输入长度不一致：links={len(links)}，host_audio_timeline={len(timelines)}", "url_list": [], "segments": [], "failed_segments": []}
        segments, failed = [], []
        for index, (url, item) in enumerate(zip(links, timelines)):
            start, end = _timeline(item)
            try:
                content = (self._splitter or self._default_split)(url, start, end, "audio")
                uploaded = self._upload(content, f"audio/host_audio_{index}_{uuid.uuid4().hex[:10]}.mp3", "audio/mpeg")
                if not uploaded.get("success", True) or not uploaded.get("url"):
                    raise InfiniteTalkMediaError("TOS 上传失败")
                segments.append({"index": index, "source_url": url, "start": start, "end": end, "duration_sec": round((end - start) / 1_000_000, 3), "url": str(uploaded["url"]), "bucket": uploaded.get("bucket", ""), "account": uploaded.get("account", "")})
            except Exception as exc:
                failed.append({"index": index, "source_url": url, "start": start, "end": end, "error": str(exc)})
        segments.sort(key=lambda item: item["index"])
        result = {"status": "success" if segments and not failed else "partial_success" if segments else "failed", "msg": f"Host 音频切割完成：成功 {len(segments)} 段，失败 {len(failed)} 段", "url_list": [item["url"] for item in segments], "segments": segments, "failed_segments": failed}
        return result

    def video_split(self, params: Any) -> dict[str, Any]:
        url = str(_value(params, "video_url", "") or "").strip()
        timeline = _list(_value(params, "timeline", []))
        if not url or not timeline:
            raise InfiniteTalkMediaError("video_url 和 timeline 不能为空")
        segments, failed = [], []
        for index, item in enumerate(timeline):
            start, end = _timeline(item)
            try:
                content = (self._splitter or self._default_split)(url, start, end, "video")
                uploaded = self._upload(content, f"video/seg_{index}_{uuid.uuid4().hex[:10]}.mp4", "video/mp4")
                if not uploaded.get("success", True) or not uploaded.get("url"):
                    raise InfiniteTalkMediaError("TOS 上传失败")
                segments.append({"index": index, "start": start, "end": end, "duration_sec": (end - start) / 1_000_000, "url": str(uploaded["url"]), "bucket": uploaded.get("bucket", ""), "account": uploaded.get("account", "")})
            except Exception as exc:
                failed.append({"index": index, "error": str(exc)})
        if not segments:
            raise InfiniteTalkMediaError(f"切分任务全部失败：{failed}")
        segments.sort(key=lambda item: item["index"])
        return {"status": "success" if not failed else "partial_success", "msg": f"切分完成：成功 {len(segments)} 段，失败 {len(failed)} 段", "total": len(timeline), "success_count": len(segments), "failed_count": len(failed), "url_list": [item["url"] for item in segments], "segments": segments, "failed_segments": failed}

    def merge_audio_urls(self, params: Any) -> dict[str, Any]:
        urls = [str(v or "").strip() for v in _list(_value(params, "audio_urls", [])) if str(v or "").strip()]
        if not urls:
            return {"status": "error", "msg": "请输入音频 URL", "url": "", "timeline": []}
        if self._merger is None:
            raise InfiniteTalkMediaError("未注入音频合并器；真实执行需要 ffmpeg 合并实现")
        content, duration = self._merger(urls)
        uploaded = self._upload(content, f"audio/merged_{uuid.uuid4().hex[:10]}.mp3", "audio/mpeg")
        if not uploaded.get("success", True) or not uploaded.get("url"):
            raise InfiniteTalkMediaError("合并音频上传失败")
        return {"status": "success", "msg": f"合并完成: {len(urls)} 个音频", "total": len(urls), "success": len(urls), "failed_count": 0, "failed": [], "duration": round(float(duration), 2), "format": "mp3", "url": str(uploaded["url"]), "bucket": uploaded.get("bucket"), "timeline": [{"start": 0, "end": int(float(duration) * 1_000_000)}]}


__all__ = ["InfiniteTalkMediaError", "InfiniteTalkMediaTransport", "MediaConfig"]
