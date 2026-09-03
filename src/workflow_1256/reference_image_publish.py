"""本地参考图的临时发布适配器。

图片生成服务只能拉取 HTTP(S) 地址；这里将用户选择的本地文件发布为短期签名 URL，
不会把本地路径伪装成可访问 URL，也不会把凭据写入项目文件。
"""
from __future__ import annotations

import mimetypes
import os
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .tts_audio_publisher import TOSAudioPublisher, TTSAudioPublisherConfigError


class ReferenceImagePublishError(RuntimeError):
    pass


ImagePublisher = Callable[[bytes, str, dict[str, Any]], dict[str, Any]]
_SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def split_reference_inputs(values: Sequence[str]) -> tuple[list[str], list[str]]:
    """返回 (远程 URL, 本地绝对路径)，并先验证本地文件存在。"""
    remote, local = [], []
    for index, raw in enumerate(values):
        value = str(raw).strip()
        if not value:
            continue
        if value.startswith(("https://", "http://")):
            remote.append(value)
            continue
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ReferenceImagePublishError(f"参考图 {index + 1} 不是可读取的本地文件：{value}")
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            raise ReferenceImagePublishError("本地参考图仅支持 JPG、PNG、WEBP")
        local.append(str(path))
    return remote, local


def default_image_publisher() -> TOSAudioPublisher:
    """复用已配置的私有 TOS 签名 URL 能力；未配置则明确阻断。"""
    try:
        publisher = TOSAudioPublisher.from_env()
    except TTSAudioPublisherConfigError as exc:
        raise ReferenceImagePublishError(
            "未配置 ARK_TTS_TOS_* 临时媒体桶，无法把本地参考图发布为签名 URL"
        ) from exc
    publisher.prefix = os.environ.get("REFERENCE_IMAGE_TOS_PREFIX", "1256/reference-images").strip("/")
    return publisher


def publish_local_reference_images(
    paths: Sequence[str], *, publisher: ImagePublisher | None = None
) -> list[str]:
    """发布本地参考图并返回可被图片服务读取的短期 URL。"""
    _, local_paths = split_reference_inputs(paths)
    active_publisher = publisher or default_image_publisher()
    urls: list[str] = []
    for source in local_paths:
        path = Path(source)
        content = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        result = active_publisher(
            content,
            f"ref_{uuid.uuid4().hex}{path.suffix.lower()}",
            {"mime_type": mime, "source_name": path.name},
        )
        url = result.get("url") if isinstance(result, dict) else ""
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise ReferenceImagePublishError(f"本地参考图发布后未获得有效 URL：{path.name}")
        urls.append(url)
    return urls


__all__ = [
    "ImagePublisher", "ReferenceImagePublishError", "publish_local_reference_images",
    "split_reference_inputs",
]
