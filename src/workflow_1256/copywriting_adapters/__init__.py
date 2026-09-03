"""第三方文案 Skill 的项目适配边界。

这里不复制上游源码，也不在缺少上游实现时伪造成功。每个适配器只负责
输入边界、版本追踪和结果契约；实际 provider 必须由生产配置显式注入。
"""

from .cangjie_adapter import CangjieAdapterUnavailable, require_confirmed_sources
from .human_writing_adapter import HumanWritingResult, run_safe_human_writing_gate
from .nuwa_adapter import NuwaAdapterUnavailable, require_obsidian_work_cards
from .writing_dna_adapter import WritingDnaSampleGate, validate_writing_dna_sample_count

__all__ = [
    "CangjieAdapterUnavailable",
    "HumanWritingResult",
    "NuwaAdapterUnavailable",
    "WritingDnaSampleGate",
    "require_confirmed_sources",
    "require_obsidian_work_cards",
    "run_safe_human_writing_gate",
    "validate_writing_dna_sample_count",
]
