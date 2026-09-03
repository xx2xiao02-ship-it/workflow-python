"""账号知识库的数据契约。

契约刻意使用标准库 dataclass，避免把 Obsidian、模型供应商或向量库
变成当前生产链路的硬依赖。所有卡片都保留来源和内容哈希，便于回溯。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any, Mapping
from urllib.parse import urlparse


SCHEMA_VERSION = 1
ALLOWED_CONTENT_GRADES = frozenset({"S", "A", "B", "C"})
ALLOWED_PERFORMANCE_GRADES = frozenset({"S", "A", "B", "C", "UNRATED"})
ALLOWED_KNOWLEDGE_STATUSES = frozenset(
    {"RAW", "DISTILLED", "REVIEW_PENDING", "APPROVED", "PUBLISHED", "ARCHIVED", "REJECTED"}
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_HTTP_SCHEMES = frozenset({"http", "https"})
_RELATIVE_SOURCE_RE = re.compile(r"^[^\\/]+(?:[/\\][^\\/]+)*$")


class KnowledgeContractError(ValueError):
    """知识卡片不符合版本化契约。"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def content_sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _validate_id(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not _ID_RE.fullmatch(normalized):
        raise KnowledgeContractError(f"{field_name} 必须是安全的字母数字/下划线/短横线编号")
    return normalized


def _validate_http_url(value: str, field_name: str = "source_url") -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    if parsed.scheme.lower() not in _HTTP_SCHEMES or not parsed.netloc:
        raise KnowledgeContractError(f"{field_name} 必须是 HTTP(S) 地址")
    return normalized


def _validate_relative_ref(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        return ""
    path = PurePath(normalized)
    if path.is_absolute() or ".." in path.parts or not _RELATIVE_SOURCE_RE.fullmatch(normalized):
        raise KnowledgeContractError("source_ref 必须是知识库内的相对路径，不能包含绝对路径或 ..")
    return normalized


def _clean_list(value: Any, field_name: str, *, max_items: int = 100) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise KnowledgeContractError(f"{field_name} 必须是字符串数组")
    if len(values) > max_items:
        raise KnowledgeContractError(f"{field_name} 条目过多")
    result: list[str] = []
    for item in values:
        text = normalize_text(item)
        if text and text not in result:
            result.append(text)
    return result


def build_work_id(
    *, account_id: str, title: str, source_url: str = "", published_at: str = "", content: str = ""
) -> str:
    """按作品身份生成稳定编号，重复导入不会产生第二个 work_id。"""

    account = _validate_id(account_id, "account_id")
    identity = "\x1f".join(
        [account, normalize_text(title).lower(), str(source_url or "").strip(), str(published_at or "").strip()]
    )
    if not normalize_text(title) and not str(source_url or "").strip() and not content:
        raise KnowledgeContractError("作品至少需要 title、source_url 或 content 之一")
    if content and not str(source_url or "").strip() and not str(published_at or "").strip():
        identity += "\x1f" + content_sha256(content)
    return "work-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


@dataclass
class WorkCard:
    work_id: str
    account_id: str
    title: str
    platform: str = ""
    source_url: str = ""
    published_at: str = ""
    source_ref: str = ""
    # 与统一知识源注册表的可追踪关联；旧作品卡缺失时保持空值兼容。
    source_id: str = ""
    source_version: str = "source-v1"
    content_hash: str = ""
    facts: list[str] = field(default_factory=list)
    core_thesis: str = ""
    angle: str = ""
    reasoning_steps: list[str] = field(default_factory=list)
    evidence_types: list[str] = field(default_factory=list)
    counterpoints: list[str] = field(default_factory=list)
    conclusion_boundary: str = ""
    content_grade: str = "B"
    performance_grade: str = "UNRATED"
    manual_reviewed: bool = False
    knowledge_status: str = "RAW"
    retrieval_weight: float | None = None
    distilled_by: str = ""
    prompt_version: str = ""
    distilled_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> "WorkCard":
        self.work_id = _validate_id(self.work_id, "work_id")
        self.account_id = _validate_id(self.account_id, "account_id")
        self.title = normalize_text(self.title)
        if not self.title:
            raise KnowledgeContractError("作品 title 不能为空")
        self.platform = normalize_text(self.platform)
        self.source_url = _validate_http_url(self.source_url)
        self.source_ref = _validate_relative_ref(self.source_ref)
        self.source_id = normalize_text(self.source_id)
        self.source_version = normalize_text(self.source_version) or "source-v1"
        self.content_hash = str(self.content_hash or "").strip().lower()
        if self.content_hash and not re.fullmatch(r"[0-9a-f]{64}", self.content_hash):
            raise KnowledgeContractError("content_hash 必须是 SHA-256 十六进制值")
        self.facts = _clean_list(self.facts, "facts")
        self.reasoning_steps = _clean_list(self.reasoning_steps, "reasoning_steps")
        self.evidence_types = _clean_list(self.evidence_types, "evidence_types")
        self.counterpoints = _clean_list(self.counterpoints, "counterpoints")
        self.core_thesis = normalize_text(self.core_thesis)
        self.angle = normalize_text(self.angle)
        self.conclusion_boundary = normalize_text(self.conclusion_boundary)
        if self.content_grade not in ALLOWED_CONTENT_GRADES:
            raise KnowledgeContractError("content_grade 必须是 S/A/B/C")
        if self.performance_grade not in ALLOWED_PERFORMANCE_GRADES:
            raise KnowledgeContractError("performance_grade 必须是 S/A/B/C/UNRATED")
        if self.knowledge_status not in ALLOWED_KNOWLEDGE_STATUSES:
            raise KnowledgeContractError("knowledge_status 不在允许范围内")
        if self.retrieval_weight is None:
            self.retrieval_weight = {"S": 1.0, "A": 0.8, "B": 0.35, "C": 0.0}[self.content_grade]
        try:
            self.retrieval_weight = float(self.retrieval_weight)
        except (TypeError, ValueError) as exc:
            raise KnowledgeContractError("retrieval_weight 必须是数字") from exc
        if not 0.0 <= self.retrieval_weight <= 1.0:
            raise KnowledgeContractError("retrieval_weight 必须在 0 到 1 之间")
        if self.schema_version != SCHEMA_VERSION:
            raise KnowledgeContractError(f"不支持的作品卡 schema_version: {self.schema_version}")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkCard":
        if not isinstance(value, Mapping):
            raise KnowledgeContractError("作品卡必须是对象")
        fields = {name for name in cls.__dataclass_fields__}
        payload = {key: value[key] for key in fields if key in value}
        return cls(**payload).validate()


@dataclass
class CognitionCard:
    cognition_id: str
    account_id: str
    title: str
    claim: str
    reasoning_pattern: str = ""
    suitable_events: list[str] = field(default_factory=list)
    evidence_work_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    knowledge_status: str = "DISTILLED"
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> "CognitionCard":
        self.cognition_id = _validate_id(self.cognition_id, "cognition_id")
        self.account_id = _validate_id(self.account_id, "account_id")
        self.title = normalize_text(self.title)
        self.claim = normalize_text(self.claim)
        if not self.title or not self.claim:
            raise KnowledgeContractError("认知卡 title 和 claim 不能为空")
        self.reasoning_pattern = normalize_text(self.reasoning_pattern)
        self.suitable_events = _clean_list(self.suitable_events, "suitable_events")
        self.evidence_work_ids = _clean_list(self.evidence_work_ids, "evidence_work_ids")
        try:
            self.confidence = float(self.confidence)
        except (TypeError, ValueError) as exc:
            raise KnowledgeContractError("confidence 必须是数字") from exc
        if not 0.0 <= self.confidence <= 1.0:
            raise KnowledgeContractError("confidence 必须在 0 到 1 之间")
        if self.knowledge_status not in ALLOWED_KNOWLEDGE_STATUSES:
            raise KnowledgeContractError("knowledge_status 不在允许范围内")
        if self.schema_version != SCHEMA_VERSION:
            raise KnowledgeContractError(f"不支持的认知卡 schema_version: {self.schema_version}")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CognitionCard":
        if not isinstance(value, Mapping):
            raise KnowledgeContractError("认知卡必须是对象")
        fields = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value[key] for key in fields if key in value}).validate()


@dataclass
class GradeCard:
    grade_id: str
    work_id: str
    account_id: str
    content_grade: str
    performance_grade: str = "UNRATED"
    reviewer: str = ""
    notes: str = ""
    created_at: str = field(default_factory=utc_now)
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> "GradeCard":
        self.grade_id = _validate_id(self.grade_id, "grade_id")
        self.work_id = _validate_id(self.work_id, "work_id")
        self.account_id = _validate_id(self.account_id, "account_id")
        if self.content_grade not in ALLOWED_CONTENT_GRADES:
            raise KnowledgeContractError("content_grade 必须是 S/A/B/C")
        if self.performance_grade not in ALLOWED_PERFORMANCE_GRADES:
            raise KnowledgeContractError("performance_grade 必须是 S/A/B/C/UNRATED")
        self.reviewer = normalize_text(self.reviewer)
        self.notes = normalize_text(self.notes)
        if self.schema_version != SCHEMA_VERSION:
            raise KnowledgeContractError(f"不支持的评级卡 schema_version: {self.schema_version}")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GradeCard":
        if not isinstance(value, Mapping):
            raise KnowledgeContractError("评级卡必须是对象")
        fields = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value[key] for key in fields if key in value}).validate()


__all__ = [
    "ALLOWED_CONTENT_GRADES",
    "ALLOWED_KNOWLEDGE_STATUSES",
    "ALLOWED_PERFORMANCE_GRADES",
    "CognitionCard",
    "GradeCard",
    "KnowledgeContractError",
    "SCHEMA_VERSION",
    "WorkCard",
    "build_work_id",
    "content_sha256",
    "normalize_text",
    "utc_now",
]
