"""项目内文案 Skill 治理清单读取器。

治理清单是运行时展示和审计的唯一来源。它不下载上游仓库，也不把
``not_verified`` 记录转换为“已安装”。清单缺失时返回保守的空状态，保证
生产链仍可启动但页面不会伪造第三方能力。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


DEFAULT_GOVERNANCE_PATH = Path(__file__).resolve().parents[2] / "docs" / "skill-governance.json"


def load_skill_governance(path: Path | str | None = None) -> dict[str, Any]:
    candidate = Path(path or DEFAULT_GOVERNANCE_PATH).expanduser()
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "schema_version": "skill-governance-v1",
            "status": "unavailable",
            "skills": [],
            "path": str(candidate),
        }
    if not isinstance(payload, Mapping):
        return {
            "schema_version": "skill-governance-v1",
            "status": "invalid",
            "skills": [],
            "path": str(candidate),
        }
    records = payload.get("skills")
    skills = [dict(item) for item in records if isinstance(item, Mapping)] if isinstance(records, list) else []
    return {
        **dict(payload),
        "schema_version": str(payload.get("schema_version") or "skill-governance-v1"),
        "status": "ready",
        "skills": skills,
        "path": str(candidate),
    }


def runtime_skill_versions(governance: Mapping[str, Any] | None = None) -> dict[str, str]:
    """保持旧 API 的简单 key/value，同时不掩盖未验证状态。"""

    result: dict[str, str] = {
        "source_registry": "knowledge-source-v1",
        "copywriting_orchestrator": "copywriting-orchestrator-v1",
        "cangjie_adapter": "adapter-unverified",
        "nuwa_adapter": "adapter-unverified",
        "writing_dna": "local-writing-dna-skill",
        "human_writing": "local-safe-adapter-v1",
    }
    records = governance.get("skills") if isinstance(governance, Mapping) else []
    if not isinstance(records, list):
        return result
    aliases = {
        "cangjie": "cangjie_adapter",
        "nuwa": "nuwa_adapter",
        "writing-dna": "writing_dna",
        "human-writing": "human_writing",
        "copywriting-orchestrator": "copywriting_orchestrator",
    }
    # ``upstream_version`` is an audit/provenance field, not proof that the
    # provider is installed.  The governance file deliberately records
    # read-only audits as ``audited_read_only_not_installed``; treating only
    # the old ``not_verified`` spelling as unavailable made the UI display
    # e.g. ``v2.5.0`` for an adapter that cannot execute at runtime.
    unverified_availability = {
        "not_verified",
        "audited_read_only_not_installed",
        "not_installed",
        "unavailable",
        "missing",
    }
    for item in records:
        if not isinstance(item, Mapping):
            continue
        key = aliases.get(str(item.get("skill_id") or ""))
        if not key:
            continue
        version = str(item.get("upstream_version") or "").strip()
        status = str(item.get("availability") or "").strip()
        unavailable = (
            status in unverified_availability
            or "not_installed" in status
            or "not_verified" in status
            or not version
        )
        if unavailable:
            result[key] = "adapter-unverified" if key.endswith("_adapter") or key == "human_writing" else result[key]
        elif key == "writing_dna" and status == "local_expected":
            result[key] = "local-writing-dna-skill"
        else:
            result[key] = version
    return result


__all__ = ["DEFAULT_GOVERNANCE_PATH", "load_skill_governance", "runtime_skill_versions"]
