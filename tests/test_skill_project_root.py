from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "skills" / "daily-update-te" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from project_root import find_project_root  # noqa: E402


def test_project_skill_finds_root_from_current_directory() -> None:
    assert find_project_root() == PROJECT_ROOT


def test_global_skill_can_use_explicit_project_root(monkeypatch) -> None:
    monkeypatch.chdir(PROJECT_ROOT / "tests")
    monkeypatch.setenv("DAILY_UPDATE_TE_ROOT", str(PROJECT_ROOT))

    assert find_project_root() == PROJECT_ROOT
