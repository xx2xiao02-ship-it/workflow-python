"""F1 选题到文案的公开契约。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ValidationError

TOPIC_CONTENT_SCHEMA_VERSION = "topic-content-manifest-v1"
MIN_TOPIC_CONTENT_CHARS = 80


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _text(value: Any, field_name: str, *, allow_empty: bool = False, max_chars: int = 200_000) -> str:
    text = str(value or "").strip()
    if not allow_empty and not text:
        raise ValidationError(f"{field_name} 不能为空")
    if len(text) > max_chars:
        raise ValidationError(f"{field_name} 超过允许长度")
    return text


def _http_url(value: Any, field_name: str, *, allow_empty: bool = True) -> str:
    text = _text(value, field_name, allow_empty=allow_empty, max_chars=2_000)
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(f"{field_name} 必须是带域名的 HTTP(S) 地址")
    return text


def _provenance(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or not all(isinstance(item, Mapping) for item in value):
        raise ValidationError("provenance 必须是非空对象数组")
    return [dict(item) for item in value]


@dataclass(frozen=True)
class TopicContentManifest:
    schema_version: str
    project_id: str
    run_id: str
    selection_id: str
    source_content_id: str
    title: str
    content: str
    status: str
    source_url: str = ""
    source_name: str = ""
    content_sha256: str = ""
    provenance: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.schema_version != TOPIC_CONTENT_SCHEMA_VERSION:
            raise ValidationError("TopicContentManifest schema_version 不受支持")
        for name in ("project_id", "run_id", "selection_id", "source_content_id", "title"):
            _text(getattr(self, name), name)
        content = _text(self.content, "content")
        if len(content) < MIN_TOPIC_CONTENT_CHARS:
            raise ValidationError(f"content 少于 {MIN_TOPIC_CONTENT_CHARS} 个字符，不能进入文案模块")
        if self.status != "CONTENT_READY":
            raise ValidationError("只有 CONTENT_READY 才能交接 TopicContentManifest")
        _http_url(self.source_url, "source_url")
        _text(self.source_name, "source_name", allow_empty=True, max_chars=300)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if self.content_sha256 and self.content_sha256.lower() != digest:
            raise ValidationError("content_sha256 与正文不一致")
        _provenance(self.provenance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "selection_id": self.selection_id,
            "source_content_id": self.source_content_id,
            "title": self.title,
            "content": self.content,
            "status": self.status,
            "source_url": self.source_url,
            "source_name": self.source_name,
            "content_sha256": self.content_sha256 or hashlib.sha256(self.content.encode("utf-8")).hexdigest(),
            "provenance": [dict(item) for item in self.provenance],
        }


def adapt_topic_content(source: Mapping[str, Any], *, project_id: str, run_id: str) -> TopicContentManifest:
    """把治理存储中的正文投影为公开交接契约，不回写源记录。"""

    if not isinstance(source, Mapping):
        raise ValidationError("source 必须是对象")
    selection_id = str(source.get("selection_id") or "").strip()
    source_content_id = str(source.get("source_content_id") or "").strip()
    provenance = source.get("provenance")
    if not isinstance(provenance, list) or not provenance:
        provenance = [
            {
                "source": "topic_writing_governance",
                "selection_id": selection_id,
                "source_content_id": source_content_id,
                "source_url": str(source.get("source_url") or "").strip(),
                "evidence_level": "collected_content",
            }
        ]
    return TopicContentManifest(
        schema_version=TOPIC_CONTENT_SCHEMA_VERSION,
        project_id=str(project_id or "").strip(),
        run_id=str(run_id or "").strip(),
        selection_id=selection_id,
        source_content_id=source_content_id,
        title=str(source.get("title") or "").strip(),
        content=str(source.get("content") or "").strip(),
        status=str(source.get("status") or "").strip().upper(),
        source_url=str(source.get("source_url") or "").strip(),
        source_name=str(source.get("source_name") or source.get("platform") or "").strip(),
        content_sha256=str(source.get("content_hash") or source.get("content_sha256") or "").strip(),
        provenance=[dict(item) for item in provenance if isinstance(item, Mapping)],
    )


__all__ = ["MIN_TOPIC_CONTENT_CHARS", "TOPIC_CONTENT_SCHEMA_VERSION", "TopicContentManifest", "adapt_topic_content", "utc_now"]
