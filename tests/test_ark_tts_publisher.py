from __future__ import annotations

import pytest

from workflow_1256.tts_audio_publisher import (
    TOSAudioPublisher,
    TOSBucketConfig,
    TTSAudioPublisherConfigError,
    TTSAudioPublisherError,
)


def _config(name: str = "main") -> TOSBucketConfig:
    return TOSBucketConfig(
        access_key=f"ak-{name}",
        secret_key=f"sk-{name}",
        bucket=f"bucket-{name}",
        endpoint="tos-cn-beijing.volces.com",
        region="cn-beijing",
    )


def test_tos_publisher_uploads_bytes_without_exposing_credentials() -> None:
    observed = []

    class FakeClient:
        def put_object(self, bucket, key, *, content, **kwargs):
            observed.append((bucket, key, content.read(), kwargs))

    publisher = TOSAudioPublisher([_config()], client_factory=lambda _config: FakeClient())
    result = publisher(b"audio", "tts-1.mp3", {"duration": 2.5})

    assert result == {
        "url": "https://bucket-main.tos-cn-beijing.volces.com/1256/tts/tts-1.mp3",
        "duration": 2.5,
    }
    assert observed == [("bucket-main", "1256/tts/tts-1.mp3", b"audio", {})]


def test_tos_publisher_uses_backup_after_primary_failure() -> None:
    calls = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def put_object(self, bucket, key, *, content, **_kwargs):
            calls.append((self.config.bucket, content.read()))
            if self.config.bucket == "bucket-main":
                raise RuntimeError("primary unavailable")

    publisher = TOSAudioPublisher(
        [_config("main"), _config("backup")],
        client_factory=lambda config: FakeClient(config),
    )
    result = publisher(b"audio", "tts-2.mp3", {})

    assert result["url"].startswith("https://bucket-backup.")
    assert calls == [("bucket-main", b"audio"), ("bucket-backup", b"audio")]


def test_tos_publisher_primary_only_does_not_try_backup() -> None:
    calls = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def put_object(self, bucket, key, *, content, **_kwargs):
            calls.append(self.config.bucket)
            content.read()
            raise RuntimeError("primary unavailable")

    publisher = TOSAudioPublisher(
        [_config("main"), _config("backup")],
        failover_strategy="primary_only",
        client_factory=lambda config: FakeClient(config),
    )
    with pytest.raises(TTSAudioPublisherError):
        publisher(b"audio", "tts-primary-only.mp3", {})

    assert calls == ["bucket-main"]


def test_tos_publisher_prefers_presigned_url_when_sdk_supports_it() -> None:
    class Signed:
        signed_url = "https://signed.example/tts.mp3?signature=redacted"

    class FakeClient:
        def put_object(self, bucket, key, *, content, **_kwargs):
            content.read()

        def pre_signed_url(self, *_args, **_kwargs):
            return Signed()

    publisher = TOSAudioPublisher(
        [_config()],
        url_expires=7200,
        client_factory=lambda _config: FakeClient(),
    )
    result = publisher(b"audio", "tts-3.mp3", {})

    assert result["url"] == "https://signed.example/tts.mp3?signature=redacted"


def test_tos_publisher_requires_nonempty_bucket_configuration() -> None:
    with pytest.raises(TTSAudioPublisherConfigError):
        TOSAudioPublisher([])
