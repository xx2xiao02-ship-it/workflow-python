"""账号采集第一阶段的可追踪证据资产契约。

本模块只负责本地契约和状态门禁，不调用模型、不抓取外部平台。RawSource
记录一篇原始作品与其来源，EvidenceSet 记录账号级合格作品集合，
DistillationJob 记录采集任务的可恢复状态。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping


EVIDENCE_SCHEMA_VERSION = "evidence-v1"
DISTILLATION_JOB_SCHEMA_VERSION = "distillation-job-v1"
EVIDENCE_STATUSES = frozenset({"WAITING_FOR_EVIDENCE", "READY_TO_DISTILL"})
JOB_STATUSES = frozenset({"COLLECTING", "WAITING_FOR_EVIDENCE", "READY_TO_DISTILL", "FAILED"})


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]


def _id(value: Any, name: str) -> str:
    result = _text(value, 160)
    if not result or "/" in result or "\\" in result or ".." in result:
        raise ValueError(f"{name} 不合法")
    return result


def _ids(value: Any, name: str, limit: int = 200) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple, set)) else []
    if len(values) > limit:
        raise ValueError(f"{name} 条目过多")
    result: list[str] = []
    for item in values:
        item_id = _text(item, 160)
        if item_id and item_id not in result:
            result.append(item_id)
    return result


def build_evidence_set_id(account_id: str, *, source_ids: list[str] | tuple[str, ...] = (), seed: str = "") -> str:
    material = "\x1f".join([_id(account_id, "account_id"), _text(seed, 200), *sorted(_ids(source_ids, "source_ids"))])
    return "evidence-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


@dataclass
class RawSource:
    source_id: str
    account_id: str
    work_id: str
    source_version: str = "source-v1"
    raw_url: str = ""
    platform: str = ""
    source_kind: str = "video"
    author: str = ""
    title: str = ""
    published_at: str = ""
    transcript: str = ""
    content_hash: str = ""
    vault_path: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def validate(self) -> "RawSource":
        self.source_id = _id(self.source_id, "source_id")
        self.account_id = _id(self.account_id, "account_id")
        self.work_id = _id(self.work_id, "work_id")
        self.source_version = _text(self.source_version, 80) or "source-v1"
        self.raw_url = _text(self.raw_url, 2_000)
        self.platform = _text(self.platform, 80)
        self.source_kind = _text(self.source_kind, 80) or "video"
        self.author = _text(self.author, 200)
        self.title = _text(self.title, 2_000)
        self.published_at = _text(self.published_at, 120)
        self.transcript = _text(self.transcript, 200_000)
        self.content_hash = _text(self.content_hash, 64).lower()
        self.vault_path = _text(self.vault_path, 2_000)
        if self.schema_version != EVIDENCE_SCHEMA_VERSION:
            raise ValueError(f"不支持的 RawSource schema_version: {self.schema_version}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.validate())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RawSource":
        fields = set(cls.__dataclass_fields__)
        return cls(**{key: value[key] for key in fields if key in value}).validate()


@dataclass
class EvidenceSet:
    evidence_set_id: str
    account_id: str
    source_ids: list[str] = field(default_factory=list)
    source_versions: dict[str, str] = field(default_factory=dict)
    candidate_count: int = 0
    eligible_work_count: int = 0
    target_valid_works: int = 50
    target_reached: bool = False
    status: str = "WAITING_FOR_EVIDENCE"
    work_ids: list[str] = field(default_factory=list)
    checkpoint_state: str = "saved"
    task_id: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def validate(self) -> "EvidenceSet":
        self.evidence_set_id = _id(self.evidence_set_id, "evidence_set_id")
        self.account_id = _id(self.account_id, "account_id")
        self.source_ids = _ids(self.source_ids, "source_ids")
        self.source_versions = {
            _text(key, 160): _text(value, 80) or "source-v1"
            for key, value in (self.source_versions.items() if isinstance(self.source_versions, Mapping) else [])
            if _text(key, 160)
        }
        self.candidate_count = max(0, int(self.candidate_count or 0))
        self.eligible_work_count = max(0, int(self.eligible_work_count or 0))
        self.target_valid_works = max(1, int(self.target_valid_works or 50))
        self.target_reached = self.eligible_work_count >= self.target_valid_works
        self.status = "READY_TO_DISTILL" if self.target_reached else "WAITING_FOR_EVIDENCE"
        self.work_ids = _ids(self.work_ids, "work_ids")
        self.checkpoint_state = _text(self.checkpoint_state, 80) or "saved"
        self.task_id = _text(self.task_id, 160)
        if self.schema_version != EVIDENCE_SCHEMA_VERSION:
            raise ValueError(f"不支持的 EvidenceSet schema_version: {self.schema_version}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.validate())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceSet":
        fields = set(cls.__dataclass_fields__)
        return cls(**{key: value[key] for key in fields if key in value}).validate()


@dataclass
class DistillationJob:
    job_id: str
    account_id: str
    target_valid_works: int = 50
    candidate_count: int = 0
    eligible_work_count: int = 0
    status: str = "COLLECTING"
    checkpoint: dict[str, Any] = field(default_factory=dict)
    evidence_set_id: str = ""
    source_ids: list[str] = field(default_factory=list)
    error: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: str = DISTILLATION_JOB_SCHEMA_VERSION

    def validate(self) -> "DistillationJob":
        self.job_id = _id(self.job_id, "job_id")
        self.account_id = _id(self.account_id, "account_id")
        self.target_valid_works = max(1, int(self.target_valid_works or 50))
        self.candidate_count = max(0, int(self.candidate_count or 0))
        self.eligible_work_count = max(0, int(self.eligible_work_count or 0))
        if self.status not in JOB_STATUSES:
            raise ValueError(f"DistillationJob 状态不支持：{self.status}")
        self.checkpoint = dict(self.checkpoint) if isinstance(self.checkpoint, Mapping) else {}
        self.evidence_set_id = _text(self.evidence_set_id, 160)
        self.source_ids = _ids(self.source_ids, "source_ids")
        self.error = _text(self.error, 2_000)
        if self.schema_version != DISTILLATION_JOB_SCHEMA_VERSION:
            raise ValueError(f"不支持的 DistillationJob schema_version: {self.schema_version}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.validate())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DistillationJob":
        fields = set(cls.__dataclass_fields__)
        return cls(**{key: value[key] for key in fields if key in value}).validate()


__all__ = [
    "DISTILLATION_JOB_SCHEMA_VERSION",
    "EVIDENCE_SCHEMA_VERSION",
    "EVIDENCE_STATUSES",
    "JOB_STATUSES",
    "DistillationJob",
    "EvidenceSet",
    "RawSource",
    "build_evidence_set_id",
    "utc_now",
]
