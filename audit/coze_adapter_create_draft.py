from __future__ import annotations

from typing import Any

from workflow_1256.create_draft import run_create_draft


def run_migrated(params: Any, transport: Any) -> dict[str, str]: return run_create_draft(params, transport=transport)


__all__=["run_migrated"]
