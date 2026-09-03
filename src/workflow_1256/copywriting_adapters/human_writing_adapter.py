"""human-writing 安全收口边界。

当前上游仓库不可验证，因此默认只做可审计的安全门禁并原样返回正文，
不会臆造“去 AI”结果，也不会修改事实和核心观点。未来接入上游检查脚本
时应替换 provider，不改变这个返回契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HumanWritingResult:
    text: str
    status: str
    changed: bool
    checks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status,
            "changed": self.changed,
            "checks": dict(self.checks),
        }


def run_safe_human_writing_gate(
    text: str,
    *,
    source_ids: list[str] | tuple[str, ...] = (),
    fact_trace_complete: bool = True,
    upstream_available: bool = False,
) -> HumanWritingResult:
    """只执行不改变正文的收口门禁。"""

    value = str(text or "").strip()
    if not value:
        return HumanWritingResult(
            text="",
            status="BLOCKED",
            changed=False,
            checks={"reason": "初稿为空", "source_ids": list(source_ids)},
        )
    if not fact_trace_complete:
        return HumanWritingResult(
            text=value,
            status="BLOCKED",
            changed=False,
            checks={"reason": "事实追踪不完整", "source_ids": list(source_ids)},
        )
    return HumanWritingResult(
        text=value,
        status="READY_FOR_HUMAN_REVIEW" if not upstream_available else "READY",
        changed=False,
        checks={
            "upstream_check": "not_verified" if not upstream_available else "available",
            "facts_unchanged": True,
            "source_ids": list(source_ids),
        },
    )


__all__ = ["HumanWritingResult", "run_safe_human_writing_gate"]
