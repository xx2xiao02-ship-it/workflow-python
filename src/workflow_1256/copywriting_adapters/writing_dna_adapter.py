"""Writing-DNA 的样本门禁；具体蒸馏仍由本地 Skill/provider 执行。"""

from __future__ import annotations


class WritingDnaSampleGate(ValueError):
    """样本数量不足或参数不合法。"""


def validate_writing_dna_sample_count(sample_count: int) -> dict[str, object]:
    try:
        count = int(sample_count)
    except (TypeError, ValueError) as exc:
        raise WritingDnaSampleGate("Writing-DNA 样本数必须是整数") from exc
    if count < 0:
        raise WritingDnaSampleGate("Writing-DNA 样本数不能为负数")
    if count < 30:
        return {
            "status": "EXPERIMENTAL",
            "formal": False,
            "sample_count": count,
            "reliability_note": "少于 30 篇完整作品，只能作为实验版。",
        }
    return {
        "status": "READY_FOR_REVIEW",
        "formal": True,
        "sample_count": count,
        "reliability_note": "达到 30 篇完整作品，可进入人工审核；80—100 篇更稳健。",
    }


__all__ = ["WritingDnaSampleGate", "validate_writing_dna_sample_count"]
