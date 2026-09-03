"""仓颉适配边界。

仓库不可用时保持明确阻断；本模块不复制或假实现上游 Skill。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class CangjieAdapterUnavailable(RuntimeError):
    """上游仓颉实现未在当前项目中可验证。"""


def require_confirmed_sources(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """验证仓颉输入已经进入 Obsidian 且经过来源确认。"""

    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise CangjieAdapterUnavailable("仓颉适配器需要已登记知识源数组")
    normalized: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise CangjieAdapterUnavailable("仓颉输入包含无效知识源记录")
        source_id = str(record.get("source_id") or "").strip()
        status = str(record.get("status") or "").strip().upper()
        obsidian_path = str(record.get("obsidian_path") or "").strip()
        if not source_id or not obsidian_path:
            raise CangjieAdapterUnavailable("仓颉只接受已写入 Obsidian 的知识源")
        if status in {"PENDING_CONFIRMATION", "FAILED"}:
            raise CangjieAdapterUnavailable(f"知识源 {source_id} 尚未完成确认或已失败")
        normalized.append({
            "source_id": source_id,
            "source_version": str(record.get("source_version") or "source-v1"),
            "obsidian_path": obsidian_path,
            "status": status,
        })
    if not normalized:
        raise CangjieAdapterUnavailable("仓颉至少需要一个已确认知识源")
    return normalized


__all__ = ["CangjieAdapterUnavailable", "require_confirmed_sources"]
