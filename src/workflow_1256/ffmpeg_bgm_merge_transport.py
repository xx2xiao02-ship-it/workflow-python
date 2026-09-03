"""本地 ``merge_bgm_timeline`` 融合 transport。

原工作流的 165818 插件源码未随导出包提供。本模块按已确认的输入输出契约，
使用本地 FFmpeg 下载、裁剪、按微秒时间线排布并混合 BGM，最后通过显式注入的
发布器返回可访问 URL。它不伪造 URL，也不把本地临时文件路径当作结果。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .merge_bgm_timeline import BgmMergeValidationError, build_request


class FfmpegBgmMergeTransportError(RuntimeError):
    """本地 BGM 融合、下载或发布失败。"""


AudioDownloader = Callable[[str], bytes]
AudioPublisher = Callable[[bytes, str, Mapping[str, Any]], Any]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _default_download(url: str) -> bytes:
    request = Request(url, headers={"Accept": "audio/*"}, method="GET")
    try:
        with urlopen(request, timeout=180) as response:
            data = response.read()
    except Exception as exc:  # pragma: no cover - exercised by real transport
        raise FfmpegBgmMergeTransportError(
            f"下载 BGM 失败：{type(exc).__name__}"
        ) from exc
    if not data:
        raise FfmpegBgmMergeTransportError("下载 BGM 返回空文件")
    return data


def _published_url(value: Any) -> str:
    if isinstance(value, str):
        url = value.strip()
    elif isinstance(value, Mapping):
        url = _text(value.get("url") or value.get("link"))
    else:
        url = ""
    if not url.startswith(("http://", "https://")):
        raise FfmpegBgmMergeTransportError(
            "BGM 发布器必须返回可访问的 http(s) URL"
        )
    return url


class FfmpegBgmMergeTransport:
    """将 165818 输入契约映射为本地 FFmpeg 融合与 URL 发布。"""

    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        downloader: AudioDownloader | None = None,
        publisher: AudioPublisher | None = None,
        temp_root: str | os.PathLike[str] | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        resolved_ffmpeg = ffmpeg_path or os.environ.get("FFMPEG_PATH", "").strip()
        resolved_ffmpeg = resolved_ffmpeg or shutil.which("ffmpeg") or ""
        if not resolved_ffmpeg:
            raise FfmpegBgmMergeTransportError(
                "未找到 ffmpeg；请安装 FFmpeg 或设置 FFMPEG_PATH"
            )
        self.ffmpeg_path = resolved_ffmpeg
        self.downloader = downloader or _default_download
        if publisher is None:
            raise FfmpegBgmMergeTransportError(
                "未配置 BGM 发布器；不能把本地临时文件路径当作 audio_url"
            )
        self.publisher = publisher
        self.temp_root = Path(temp_root) if temp_root else None
        self.request_id_factory = request_id_factory or (lambda: uuid.uuid4().hex)

    @classmethod
    def from_env(cls) -> "FfmpegBgmMergeTransport":
        from .tts_audio_publisher import TOSAudioPublisher

        try:
            publisher = TOSAudioPublisher.from_env()
        except Exception as exc:
            raise FfmpegBgmMergeTransportError(
                f"BGM TOS 发布器未配置：{type(exc).__name__}"
            ) from exc
        return cls(publisher=publisher)

    @staticmethod
    def _filter_complex(
        timelines: list[Mapping[str, Any]],
        transition_schemes: list[str],
    ) -> tuple[str, float]:
        base_start = min(int(item["start"]) for item in timelines)
        total_end = max(int(item["end"]) for item in timelines)
        total_seconds = (total_end - base_start) / 1_000_000
        if total_seconds <= 0:
            raise FfmpegBgmMergeTransportError("BGM 融合总时长必须大于 0")

        durations = [
            (int(item["end"]) - int(item["start"])) / 1_000_000
            for item in timelines
        ]
        filters: list[str] = []
        for index, item in enumerate(timelines):
            duration = durations[index]
            chain = f"[{index}:a]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS"

            if index < len(timelines) - 1:
                scheme = transition_schemes[index]
                if scheme == "crossfade":
                    overlap = min(4.0, duration / 4, durations[index + 1] / 4)
                    if overlap > 0:
                        chain += f",afade=t=out:st={max(0.0, duration - overlap):.6f}:d={overlap:.6f}"
                elif scheme == "fade_gap":
                    fade = min(1.2, duration / 4)
                    if fade > 0:
                        chain += f",afade=t=out:st={max(0.0, duration - fade):.6f}:d={fade:.6f}"

            delay_ms = max(0, int(round((int(item["start"]) - base_start) / 1000)))
            chain += f",adelay={delay_ms}:all=1"
            filters.append(f"{chain}[a{index}]")

        joined = ";".join(filters)
        joined += ";" + "".join(f"[a{index}]" for index in range(len(timelines)))
        joined += f"amix=inputs={len(timelines)}:duration=longest:normalize=0,atrim=duration={total_seconds:.6f},asetpts=PTS-STARTPTS[out]"
        return joined, total_seconds

    def __call__(self, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = build_request(params)
        except BgmMergeValidationError as exc:
            raise FfmpegBgmMergeTransportError(str(exc)) from exc

        audio_urls = request["audio_urls"]
        timelines = request["timelines"]
        schemes = request["transition_schemes"]
        filter_complex, duration = self._filter_complex(timelines, schemes)
        request_id = self.request_id_factory()

        with tempfile.TemporaryDirectory(
            prefix="workflow-bgm-merge-", dir=str(self.temp_root) if self.temp_root else None
        ) as temp_dir:
            root = Path(temp_dir)
            input_paths: list[Path] = []
            for index, url in enumerate(audio_urls):
                data = self.downloader(url)
                if not data:
                    raise FfmpegBgmMergeTransportError(
                        f"audio_urls[{index}] 下载结果为空"
                    )
                path = root / f"input-{index}.mp3"
                path.write_bytes(data)
                input_paths.append(path)

            output_path = root / "merged.mp3"
            command = [self.ffmpeg_path, "-y", "-loglevel", "error"]
            for path in input_paths:
                command.extend(["-i", str(path)])
            command.extend(
                [
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    "[out]",
                    "-t",
                    f"{duration:.6f}",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    str(output_path),
                ]
            )
            try:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=240,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise FfmpegBgmMergeTransportError(
                    f"FFmpeg 执行失败：{type(exc).__name__}"
                ) from exc
            if completed.returncode != 0 or not output_path.exists():
                detail = completed.stderr.decode("utf-8", errors="replace")[-500:]
                raise FfmpegBgmMergeTransportError(
                    f"FFmpeg 融合失败：{detail or '未生成输出文件'}"
                )

            published = self.publisher(
                output_path.read_bytes(),
                f"bgm-merge-{request_id}.mp3",
                {
                    "duration": duration,
                    "timeline_start": min(item["start"] for item in timelines),
                    "timeline_end": max(item["end"] for item in timelines),
                    "source_count": len(audio_urls),
                    "transition_schemes": list(schemes),
                },
            )

        url = _published_url(published)
        return {
            "audio_url": url,
            "audio_url_list": [url],
            "duration": duration,
        }


__all__ = ["FfmpegBgmMergeTransport", "FfmpegBgmMergeTransportError"]
