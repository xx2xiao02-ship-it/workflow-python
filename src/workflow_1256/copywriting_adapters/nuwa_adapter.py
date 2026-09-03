"""女娲账号级适配边界；只接受 Obsidian 作品卡引用。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class NuwaAdapterUnavailable(RuntimeError):
    """上游女娲实现未在当前项目中可验证。"""


def require_obsidian_work_cards(
    work_cards: Sequence[Mapping[str, Any]], *, account_id: str
) -> list[dict[str, Any]]:
    account = str(account_id or "").strip()
    if not account:
        raise NuwaAdapterUnavailable("女娲账号适配器需要 account_id")
    if not isinstance(work_cards, Sequence) or isinstance(work_cards, (str, bytes)):
        raise NuwaAdapterUnavailable("女娲输入必须是 Obsidian 作品卡数组")
    normalized: list[dict[str, Any]] = []
    for card in work_cards:
        if not isinstance(card, Mapping):
            raise NuwaAdapterUnavailable("女娲输入包含无效作品卡")
        card_account = str(card.get("account_id") or "").strip()
        work_id = str(card.get("work_id") or "").strip()
        source_ref = str(card.get("source_ref") or "").strip()
        status = str(card.get("knowledge_status") or "").strip().upper()
        if card_account != account or not work_id or not source_ref:
            raise NuwaAdapterUnavailable("女娲只接受同一账号且可追踪原文的作品卡")
        if status in {"FAILED", "RAW"}:
            raise NuwaAdapterUnavailable(f"作品卡 {work_id} 尚未完成知识审核")
        normalized.append({
            "account_id": account,
            "work_id": work_id,
            "source_ref": source_ref,
            "knowledge_status": status,
        })
    if not normalized:
        raise NuwaAdapterUnavailable("女娲至少需要一张 Obsidian 作品卡")
    return normalized


__all__ = ["NuwaAdapterUnavailable", "require_obsidian_work_cards"]
