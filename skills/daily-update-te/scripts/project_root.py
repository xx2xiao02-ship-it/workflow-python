"""定位 Daily_Update_Te 项目根目录，兼容项目版和全局 Skill。"""

from __future__ import annotations

import os
from pathlib import Path


def find_project_root() -> Path:
    candidates: list[Path] = []
    configured = (
        os.environ.get("DAILY_UPDATE_TE_ROOT", "").strip()
        or os.environ.get("WORKFLOW_1256_ROOT", "").strip()
    )
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend((Path.cwd(), Path(__file__).resolve().parents[3]))
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "src" / "workflow_1256").is_dir():
            return resolved
    raise RuntimeError(
        "找不到 Daily_Update_Te 项目。请在项目根目录执行，或设置 DAILY_UPDATE_TE_ROOT。"
    )


__all__ = ["find_project_root"]
