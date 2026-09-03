"""Shared browser executable resolution for the Douyin monitor."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any


BROWSER_EXECUTABLE_ENV = "DOUYIN_BROWSER_EXECUTABLE"


def _env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _existing_file(value: str | os.PathLike[str] | None) -> Path | None:
    if not value:
        return None
    try:
        path = Path(os.path.expandvars(os.path.expanduser(str(value).strip().strip('"'))))
    except (OSError, TypeError, ValueError):
        return None
    return path if path.is_file() else None


def _configured_path(config: Mapping[str, Any] | None) -> str:
    configured = ""
    if isinstance(config, Mapping):
        spider_config = config.get("spider")
        if isinstance(spider_config, Mapping):
            configured = str(spider_config.get("browser_executable_path") or "").strip()
    return os.environ.get(BROWSER_EXECUTABLE_ENV, "").strip() or configured


def _installed_browser_candidates() -> list[Path]:
    program_files_x86 = _env_value("ProgramFiles(x86)", "PROGRAMFILES(X86)") or r"C:\Program Files (x86)"
    program_files = _env_value("ProgramW6432", "ProgramFiles", "PROGRAMW6432", "PROGRAMFILES") or r"C:\Program Files"
    local_app_data = _env_value("LOCALAPPDATA", "LocalAppData")

    candidates = [
        Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    if local_app_data:
        local_root = Path(local_app_data)
        candidates.extend([
            local_root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            local_root / "Google" / "Chrome" / "Application" / "chrome.exe",
        ])
    for executable_name in ("msedge.exe", "chrome.exe"):
        located = shutil.which(executable_name)
        if located:
            candidates.append(Path(located))
    return candidates


def resolve_browser_executable(config: Mapping[str, Any] | None = None) -> Path | None:
    """Return an installed Edge/Chrome executable, if one is available."""

    configured = _existing_file(_configured_path(config))
    if configured is not None:
        return configured.resolve()
    for candidate in _installed_browser_candidates():
        existing = _existing_file(candidate)
        if existing is not None:
            return existing.resolve()
    return None


def browser_launch_options(config: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Build Playwright launch options without forcing a missing executable."""

    executable = resolve_browser_executable(config)
    if executable is None:
        return {}
    return {"executable_path": str(executable)}
