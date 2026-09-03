"""1256 工作流节点 1904923「关键帧信息」的独立入口。

该节点与 151394 使用同一 CapCut Mate API，但上游片段来源不同，故保留独立模块。
"""

from .keyframes_infos import (
    calculate_relative_time_offset,
    keyframes_infos,
    normalize_keyframe_value,
    run_keyframes_infos,
)

__all__ = [
    "calculate_relative_time_offset",
    "keyframes_infos",
    "normalize_keyframe_value",
    "run_keyframes_infos",
]
