from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from tools import local_service_governance as governance
from tools import run_video_production_console_dev as dev
from tools import video_production_console as console


def _fake_console() -> SimpleNamespace:
    spec = governance.ServiceSpec(
        service_name="video_production_console",
        port=8768,
        entry_path=Path("tools/video_production_console.py"),
        health_path="/api/executor/health",
        runtime_dir_name="video_production_console",
    )
    return SimpleNamespace(
        SERVICE_SPEC=spec,
        HOST="127.0.0.1",
        PORT=8768,
        DEV_READ_ONLY=False,
        SERVICE_IDENTITY={},
    )


def test_dev_runtime_uses_actual_port_and_read_only_identity() -> None:
    console = _fake_console()

    identity = dev.configure_dev_runtime(console, host="127.0.0.1", port=8791)

    assert console.PORT == 8791
    assert console.SERVICE_SPEC.port == 8791
    assert console.SERVICE_SPEC.service_name == "video_production_console_dev"
    assert console.DEV_READ_ONLY is True
    assert identity["port"] == 8791
    assert identity["service_name"] == "video_production_console_dev"
    assert identity["runtime_role"] == "dev_read_only"
    assert identity["read_only"] is True


def test_dev_runtime_cannot_masquerade_as_main_port() -> None:
    with pytest.raises(ValueError, match="不能占用 8768"):
        dev.configure_dev_runtime(_fake_console(), host="127.0.0.1", port=8768)


def test_task_snapshot_persistence_leaves_no_shared_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(console, "STYLE_TASK_DIR", tmp_path)

    console._persist_task({"task_id": "persistence-check", "state": "failed"})

    saved = tmp_path / "persistence-check.json"
    assert saved.is_file()
    assert saved.read_text(encoding="utf-8").find('"state": "failed"') >= 0
    assert list(tmp_path.glob("*.tmp")) == []
