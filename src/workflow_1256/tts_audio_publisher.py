"""把官方 TTS Base64 音频发布为 CapCut Mate 可访问的 URL。

使用火山 TOS SDK，但所有凭据只从本机环境变量读取。未配置时明确阻断，
不会写入代码、样本或日志，也不会把私有对象地址伪装成公开 URL。
"""

from __future__ import annotations

import io
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable


class TTSAudioPublisherError(RuntimeError):
    """TOS 音频发布失败。"""


class TTSAudioPublisherConfigError(TTSAudioPublisherError):
    """TOS 音频发布配置不完整。"""


@dataclass(frozen=True)
class TOSBucketConfig:
    access_key: str
    secret_key: str
    bucket: str
    endpoint: str
    region: str


TOSClientFactory = Callable[[TOSBucketConfig], Any]


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _config_from_env(prefix: str) -> TOSBucketConfig | None:
    values = {
        "access_key": _env(f"{prefix}_ACCESS_KEY"),
        "secret_key": _env(f"{prefix}_SECRET_KEY"),
        "bucket": _env(f"{prefix}_BUCKET"),
        "endpoint": _env(f"{prefix}_ENDPOINT"),
        "region": _env(f"{prefix}_REGION"),
    }
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise TTSAudioPublisherConfigError(
            f"{prefix} 配置必须同时包含 ACCESS_KEY、SECRET_KEY、BUCKET、ENDPOINT、REGION"
        )
    return TOSBucketConfig(**values)


class TOSAudioPublisher:
    """按主桶、备用桶顺序上传音频，并返回公开 URL。"""

    def __init__(
        self,
        buckets: list[TOSBucketConfig],
        *,
        prefix: str = "1256/tts",
        url_expires: int = 7200,
        failover_strategy: str = "primary_then_backup",
        client_factory: TOSClientFactory | None = None,
    ) -> None:
        if not buckets:
            raise TTSAudioPublisherConfigError("至少需要一个 TOS 音频桶")
        if isinstance(url_expires, bool) or not isinstance(url_expires, int) or url_expires <= 0:
            raise TTSAudioPublisherConfigError("TOS 音频 URL 有效期必须是正整数秒")
        self.buckets = list(buckets)
        self.prefix = prefix.strip("/")
        self.url_expires = url_expires
        self.failover_strategy = str(failover_strategy or "primary_then_backup").strip().lower()
        if self.failover_strategy not in {"primary_then_backup", "primary_only"}:
            raise TTSAudioPublisherConfigError(
                "TOS_FAILOVER_STRATEGY 只支持 primary_then_backup 或 primary_only"
            )
        self.client_factory = client_factory or self._default_client

    @classmethod
    def from_env(cls) -> "TOSAudioPublisher":
        buckets = [
            config
            for config in (
                _config_from_env("ARK_TTS_TOS"),
                _config_from_env("ARK_TTS_TOS_BACKUP"),
            )
            if config is not None
        ]
        if not buckets:
            raise TTSAudioPublisherConfigError(
                "未配置 ARK_TTS_TOS_* 音频发布参数；不会把 Base64 当成 URL"
            )
        raw_expires = _env("ARK_TTS_TOS_URL_EXPIRES") or "7200"
        try:
            url_expires = int(raw_expires)
        except ValueError as exc:
            raise TTSAudioPublisherConfigError(
                "ARK_TTS_TOS_URL_EXPIRES 必须是正整数秒"
            ) from exc
        return cls(
            buckets,
            prefix=_env("ARK_TTS_TOS_PREFIX") or "1256/tts",
            url_expires=url_expires,
            failover_strategy=_env("TOS_FAILOVER_STRATEGY") or "primary_then_backup",
        )

    @staticmethod
    def _default_client(config: TOSBucketConfig) -> Any:
        try:
            import tos
        except ImportError as exc:
            raise TTSAudioPublisherError(
                "未安装 tos SDK，无法发布 TTS 音频"
            ) from exc
        return tos.TosClientV2(
            config.access_key,
            config.secret_key,
            config.endpoint,
            region=config.region,
        )

    def __call__(
        self,
        audio: bytes,
        filename: str,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not audio:
            raise TTSAudioPublisherError("不能发布空音频")
        key = f"{self.prefix}/{filename}" if self.prefix else filename
        errors: list[str] = []
        bucket_candidates = self.buckets if self.failover_strategy == "primary_then_backup" else self.buckets[:1]
        for config in bucket_candidates:
            try:
                client = self.client_factory(config)
                client.put_object(
                    config.bucket,
                    key,
                    content=io.BytesIO(audio),
                )
                url = f"https://{config.bucket}.{config.endpoint.strip('/')}/{key}"
                if hasattr(client, "pre_signed_url"):
                    try:
                        import tos

                        signed = client.pre_signed_url(
                            tos.enum.HttpMethodType.Http_Method_Get,
                            config.bucket,
                            key,
                            expires=self.url_expires,
                        )
                        signed_url = getattr(signed, "signed_url", "")
                        if isinstance(signed_url, str) and signed_url.strip():
                            url = signed_url.strip()
                    except Exception as exc:
                        raise TTSAudioPublisherError(
                            "TOS 音频对象已上传，但无法生成可访问的预签名 URL"
                        ) from exc
                result: dict[str, Any] = {"url": url}
                if isinstance(metadata.get("duration"), (int, float)):
                    result["duration"] = metadata["duration"]
                return result
            except Exception as exc:
                errors.append(type(exc).__name__)
        raise TTSAudioPublisherError(
            f"TOS 音频发布失败，已尝试 {len(bucket_candidates)} 个桶：{','.join(errors)}"
        )


__all__ = [
    "TOSAudioPublisher",
    "TOSBucketConfig",
    "TTSAudioPublisherConfigError",
    "TTSAudioPublisherError",
]
