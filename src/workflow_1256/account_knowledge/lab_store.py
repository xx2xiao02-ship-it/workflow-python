"""V2 Lab 的独立 JSON 产物仓库。"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

from ..copywriting_knowledge_v2 import validate_copywriting_draft
from ..topic_knowledge_v2 import validate_topic_plan


class LabArtifactStoreError(RuntimeError):
    pass


class LabArtifactStore:
    """默认写入 outputs/account_knowledge_lab，不进入旧 topic/writing 目录。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    @staticmethod
    def _safe_id(value: Any) -> str:
        identifier = str(value or "").strip()
        if not identifier or "/" in identifier or "\\" in identifier or ".." in identifier:
            raise LabArtifactStoreError("V2 产物编号不合法")
        return identifier

    def _write(self, kind: str, identifier: str, payload: Mapping[str, Any]) -> Path:
        safe_kind = self._safe_id(kind)
        safe_id = self._safe_id(identifier)
        path = self.root / safe_kind / f"{safe_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            raise LabArtifactStoreError(f"无法写入 V2 产物：{path.name}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return path

    def _read(self, kind: str, identifier: str) -> dict[str, Any]:
        safe_kind = self._safe_id(kind)
        safe_id = self._safe_id(identifier)
        path = self.root / safe_kind / f"{safe_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LabArtifactStoreError(f"无法读取 V2 产物：{path.name}") from exc
        if not isinstance(payload, dict):
            raise LabArtifactStoreError("V2 产物必须是对象")
        return payload

    def save_topic_plan(self, plan: Mapping[str, Any]) -> Path:
        validated = validate_topic_plan(plan)
        return self._write("topic_plans", validated["topic_plan_id"], validated)

    def load_topic_plan(self, topic_plan_id: str) -> dict[str, Any]:
        return validate_topic_plan(self._read("topic_plans", topic_plan_id))

    def save_draft(self, draft: Mapping[str, Any]) -> Path:
        validated = validate_copywriting_draft(draft)
        return self._write("drafts", validated["draft_id"], validated)

    def load_draft(self, draft_id: str) -> dict[str, Any]:
        return validate_copywriting_draft(self._read("drafts", draft_id))

    def save_handoff(self, handoff: Mapping[str, Any]) -> Path:
        if handoff.get("handoff_type") != "v2_manual_approval" or not handoff.get("approved_copy"):
            raise LabArtifactStoreError("只有显式人工批准 handoff 才能保存")
        return self._write("handoffs", handoff.get("draft_id", ""), handoff)


__all__ = ["LabArtifactStore", "LabArtifactStoreError"]
