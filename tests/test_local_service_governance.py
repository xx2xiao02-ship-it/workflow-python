from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest

from tools import local_service_governance as governance


def _spec() -> governance.ServiceSpec:
    return governance.ServiceSpec(
        service_name="video_production_console",
        port=8768,
        entry_path=Path("tools/video_production_console.py"),
        health_path="/api/executor/health",
        runtime_dir_name="test_video_production_console",
    )


def _owned_process(spec: governance.ServiceSpec, pid: int = 20, parent_pid: int = 10) -> dict:
    return {
        "pid": pid,
        "parent_pid": parent_pid,
        "name": "python",
        "executable_path": str(spec.absolute_entry_path),
        "command_line": f'"{spec.absolute_entry_path}"',
        "creation_date": "2026-08-18T00:00:00",
    }


def test_listener_parser_deduplicates_target_port(monkeypatch: pytest.MonkeyPatch) -> None:
    output = (
        "  TCP    127.0.0.1:8768    0.0.0.0:0    LISTENING    22316\r\n"
        "  TCP    [::1]:8768        [::]:0        LISTENING    22316\r\n"
        "  TCP    127.0.0.1:8765    0.0.0.0:0    LISTENING    7788\r\n"
    )
    monkeypatch.setattr(
        governance.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, ""),
    )
    assert governance.listener_pids(8768) == [22316]


def test_process_snapshot_caches_restricted_cim_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CIM 无权限时只探测一次，后续治理检查不再等待同一超时。"""

    calls: list[tuple[str, float]] = []

    def fake_powershell(script: str, *, timeout: float = 12):
        calls.append((script, timeout))
        if "Get-CimInstance" in script:
            return []
        return [{"pid": 22316, "name": "python", "parent_pid": 0}]

    monkeypatch.setattr(governance, "_powershell_json", fake_powershell)
    monkeypatch.setattr(governance, "_PROCESS_SNAPSHOT_MODE", "unknown")

    first = governance.process_snapshot()
    first_call_count = len(calls)
    second = governance.process_snapshot()

    assert 22316 in first and 22316 in second
    assert first_call_count == 2  # CIM 探测 + 一次轻量回退
    assert len(calls) == 3  # 第二次只调用回退路径
    assert all("Get-CimInstance" not in script for script, _ in calls[2:])


def test_process_ownership_requires_project_and_entry() -> None:
    spec = _spec()
    assert governance._is_project_process(_owned_process(spec), spec)
    foreign = _owned_process(spec)
    foreign["command_line"] = r"C:\other\tools\video_production_console.py"
    foreign["executable_path"] = r"C:\other\python.exe"
    assert not governance._is_project_process(foreign, spec)


def test_current_identity_uses_runtime_metadata_when_commandline_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    monkeypatch.setattr(governance, "_git_commit", lambda _root: "abc123")
    monkeypatch.setattr(governance, "_code_fingerprint", lambda _spec: "fingerprint-a")
    process = {
        "pid": 22316,
        "parent_pid": 0,
        "name": "python",
        "executable_path": r"C:\Python\pythoncore.exe",
        "command_line": "",
    }
    metadata = {
        "pid": 22316,
        "port": 8768,
        "runtime_id": "runtime-a",
        "service": spec.service_name,
        "project_root": str(spec.project_root),
        "worktree_root": str(spec.worktree_root),
        "entry_path": str(spec.absolute_entry_path),
        "git_commit": "abc123",
        "code_fingerprint": "fingerprint-a",
    }
    health = dict(metadata)
    health["status"] = "ready"
    health["service_name"] = spec.service_name
    assert governance._metadata_owns_identity(spec, metadata, health, 22316)
    assert governance._identity_matches(spec, health, process, metadata, 22316)


def test_current_identity_uses_verified_metadata_when_process_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受限权限拿不到进程详情时，健康接口和元数据仍可证明当前实例。"""

    spec = _spec()
    monkeypatch.setattr(governance, "_git_commit", lambda _root: "abc123")
    monkeypatch.setattr(governance, "_code_fingerprint", lambda _spec: "fingerprint-a")
    metadata = {
        "pid": 22316,
        "port": 8768,
        "runtime_id": "runtime-a",
        "service": spec.service_name,
        "project_root": str(spec.project_root),
        "worktree_root": str(spec.worktree_root),
        "entry_path": str(spec.absolute_entry_path),
        "git_commit": "abc123",
        "code_fingerprint": "fingerprint-a",
    }
    health = {**metadata, "status": "ready", "service_name": spec.service_name}

    assert governance._identity_matches(spec, health, None, metadata, 22316)


def test_pid_reuse_with_different_runtime_is_not_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    monkeypatch.setattr(governance, "_git_commit", lambda _root: "abc123")
    monkeypatch.setattr(governance, "_code_fingerprint", lambda _spec: "fingerprint-a")
    metadata = {
        "pid": 22316,
        "port": 8768,
        "runtime_id": "old-runtime",
        "project_root": str(spec.project_root),
        "worktree_root": str(spec.worktree_root),
        "entry_path": str(spec.absolute_entry_path),
        "git_commit": "abc123",
        "code_fingerprint": "fingerprint-a",
    }
    health = dict(metadata)
    health["runtime_id"] = "new-runtime"
    assert not governance._metadata_owns_identity(spec, metadata, health, 22316)


def test_unknown_python_without_metadata_is_foreign() -> None:
    spec = _spec()
    process = {
        "pid": 22316,
        "parent_pid": 0,
        "name": "python",
        "executable_path": r"C:\Python\python.exe",
        "command_line": "",
    }
    assert governance._process_is_python(process)
    assert not governance._metadata_owns_identity(spec, None, None, 22316)
    assert not governance._is_project_process(process, spec)


def test_dead_pid_file_cleanup_does_not_touch_business_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = _spec()
    monkeypatch.setenv("LOCAL_SERVICE_RUNTIME_ROOT", str(tmp_path))
    paths = governance._paths(spec)
    governance._json_write(paths["metadata"], {"pid": 999999, "service": spec.service_name})
    monkeypatch.setattr(governance, "listener_pids", lambda _port: [])
    monkeypatch.setattr(governance, "process_snapshot", lambda: {})
    governance._dead_runtime_cleanup(
        spec,
        {"listener_count": 0, "metadata": {"pid": 999999, "service": spec.service_name}},
    )
    assert not paths["metadata"].exists()
    assert governance._canonical(governance.ROOT / "outputs") != governance._canonical(paths["directory"])


def test_owned_target_walks_to_project_parent() -> None:
    spec = _spec()
    processes = {
        20: _owned_process(spec, 20, 10),
        10: _owned_process(spec, 10, 1),
        1: {"pid": 1, "parent_pid": 0, "name": "cmd", "executable_path": r"C:\Windows\cmd.exe"},
    }
    info = {"instances": [{"pid": 20, "classification": "stale"}], "metadata": None}
    assert governance._owned_top_targets(spec, info, processes) == [10]


def test_owned_target_does_not_trust_stale_metadata_parent_without_live_link() -> None:
    spec = _spec()
    processes = {
        20: _owned_process(spec, 20, 0),
        10: _owned_process(spec, 10, 1),
        1: {"pid": 1, "parent_pid": 0, "name": "cmd", "executable_path": r"C:\Windows\cmd.exe"},
    }
    info = {
        "instances": [{"pid": 20, "classification": "current"}],
        "metadata": {"parent_pid": 10},
    }

    assert governance._owned_top_targets(spec, info, processes) == [20]


def test_owned_target_uses_verified_pid_tree_when_parent_relation_is_unavailable() -> None:
    spec = _spec()
    processes = {
        20: {**_owned_process(spec, 20, 0), "parent_pid": 0, "command_line": ""},
        10: {**_owned_process(spec, 10, 1), "parent_pid": 0, "command_line": ""},
        1: {"pid": 1, "parent_pid": 0, "name": "cmd", "executable_path": r"C:\Windows\cmd.exe"},
    }
    info = {
        "instances": [{"pid": 20, "classification": "stale"}],
        "metadata": {"parent_pid": 10, "pid_tree": [20, 10, 1]},
    }

    assert governance._owned_top_targets(spec, info, processes) == [10]


def test_start_lock_removes_dead_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec = _spec()
    monkeypatch.setenv("LOCAL_SERVICE_RUNTIME_ROOT", str(tmp_path))
    path = governance._paths(spec)["start_lock"]
    governance._json_write(path, {"pid": 999999})
    monkeypatch.setattr(governance, "process_snapshot", lambda: {})
    with governance._start_lock(spec):
        assert path.exists()
    assert not path.exists()


def test_start_lock_ignores_reused_pid_when_process_is_newer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    spec = _spec()
    monkeypatch.setenv("LOCAL_SERVICE_RUNTIME_ROOT", str(tmp_path))
    path = governance._paths(spec)["start_lock"]
    governance._json_write(
        path,
        {"pid": 42, "created_at": "2026-08-30T13:00:00+00:00"},
    )
    monkeypatch.setattr(
        governance,
        "process_snapshot",
        lambda: {42: {"pid": 42, "creation_date": "2026-08-30T14:00:00+00:00"}},
    )
    with governance._start_lock(spec):
        assert path.exists()
    assert not path.exists()


def test_start_lock_still_blocks_same_generation_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    spec = _spec()
    monkeypatch.setenv("LOCAL_SERVICE_RUNTIME_ROOT", str(tmp_path))
    path = governance._paths(spec)["start_lock"]
    governance._json_write(
        path,
        {"pid": 42, "created_at": "2026-08-30T14:00:00+00:00"},
    )
    monkeypatch.setattr(
        governance,
        "process_snapshot",
        lambda: {42: {"pid": 42, "creation_date": "2026-08-30T13:00:00+00:00"}},
    )
    with pytest.raises(governance.ServiceGovernanceError, match="另一个治理操作"):
        with governance._start_lock(spec):
            pass


def test_start_reuses_current_without_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec()
    monkeypatch.setitem(governance.SERVICE_SPECS, spec.service_name, spec)
    monkeypatch.setattr(governance, "_start_lock", lambda _spec: nullcontext())
    monkeypatch.setattr(governance, "_set_watchdog_enabled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(governance, "_ensure_watchdog", lambda _spec: "started")
    monkeypatch.setattr(
        governance,
        "inspect_service",
        lambda _spec: {"overall": "current", "listener_count": 1, "instances": []},
    )
    monkeypatch.setattr(governance, "_wait_for_service_current", lambda _spec: {"overall": "current"})
    result = governance.start_service(spec.service_name)
    assert result["action"] == "reused"
    assert result["watchdog"] == "started"


def test_start_refuses_foreign_without_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec()
    monkeypatch.setitem(governance.SERVICE_SPECS, spec.service_name, spec)
    monkeypatch.setattr(governance, "_start_lock", lambda _spec: nullcontext())
    monkeypatch.setattr(
        governance,
        "inspect_service",
        lambda _spec: {"overall": "foreign", "listener_count": 1, "instances": [{"classification": "foreign"}]},
    )
    monkeypatch.setattr(governance, "_stop_owned", lambda *_args: pytest.fail("不得停止 foreign"))
    with pytest.raises(governance.ServiceGovernanceError, match="foreign"):
        governance.start_service(spec.service_name)


def test_stop_owned_uses_precise_powershell_fallback_when_taskkill_does_not_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受限会话下 taskkill 无效时，只对已确认的项目 PID 做兜底结束。"""

    spec = _spec()
    process = _owned_process(spec, 20, 0)
    monkeypatch.setattr(governance, "process_snapshot", lambda: {20: process})
    monkeypatch.setattr(governance, "_audit", lambda *_args, **_kwargs: None)
    waits = iter([False, True])
    monkeypatch.setattr(governance, "_wait_for_port", lambda *_args: next(waits))
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(governance.subprocess, "run", fake_run)

    governance._stop_owned(
        spec,
        {"instances": [{"pid": 20, "classification": "stale"}], "metadata": None},
        "测试精确兜底",
    )

    assert any(call and call[0].lower() == "powershell.exe" for call in calls)
    assert not any(call[-1] == "/F" for call in calls)


def test_restart_order_is_stop_release_then_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    monkeypatch.setitem(governance.SERVICE_SPECS, spec.service_name, spec)
    monkeypatch.setattr(governance, "_start_lock", lambda _spec: nullcontext())
    monkeypatch.setattr(governance, "_set_watchdog_enabled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(governance, "_ensure_watchdog", lambda _spec: "reused")
    events: list[str] = []
    monkeypatch.setattr(
        governance,
        "inspect_service",
        lambda _spec: events.append("inspect") or {"overall": "stale", "listener_count": 1, "instances": []},
    )
    monkeypatch.setattr(governance, "_stop_owned", lambda *_args: events.append("stop"))
    monkeypatch.setattr(governance, "listener_pids", lambda _port: events.append("port_check") or [])
    monkeypatch.setattr(
        governance,
        "_start_locked",
        lambda _spec, action: events.append(f"start:{action}") or {"action": "restarted"},
    )
    result = governance.restart_service(spec.service_name)
    assert result["action"] == "restarted"
    assert events[:3] == ["inspect", "stop", "port_check"]
    assert events[-1] == "start:restart"


def test_restart_pauses_watchdog_until_new_service_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工重启不能让看门狗在停旧实例期间抢先拉起第二个实例。"""
    spec = _spec()
    monkeypatch.setitem(governance.SERVICE_SPECS, spec.service_name, spec)
    monkeypatch.setattr(governance, "_start_lock", lambda _spec: nullcontext())
    events: list[str] = []

    def set_watchdog(_spec, enabled: bool, *, reason: str) -> None:
        events.append(f"watchdog:{'on' if enabled else 'off'}")

    monkeypatch.setattr(governance, "_set_watchdog_enabled", set_watchdog)
    monkeypatch.setattr(
        governance,
        "inspect_service",
        lambda _spec: events.append("inspect") or {"overall": "stale", "listener_count": 1, "instances": []},
    )
    monkeypatch.setattr(governance, "_stop_owned", lambda *_args: events.append("stop"))
    monkeypatch.setattr(governance, "listener_pids", lambda _port: events.append("port_check") or [])
    monkeypatch.setattr(
        governance,
        "_start_locked",
        lambda _spec, action: events.append(f"start:{action}") or {"action": "restarted"},
    )
    monkeypatch.setattr(governance, "_ensure_watchdog", lambda _spec: events.append("ensure") or "started")

    result = governance.restart_service(spec.service_name)

    assert result["watchdog"] == "started"
    assert events == [
        "watchdog:off",
        "inspect",
        "stop",
        "port_check",
        "start:restart",
        "watchdog:on",
        "ensure",
    ]


def test_restart_failure_restores_watchdog_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    monkeypatch.setitem(governance.SERVICE_SPECS, spec.service_name, spec)
    monkeypatch.setattr(governance, "_start_lock", lambda _spec: nullcontext())
    events: list[str] = []

    def set_watchdog(_spec, enabled: bool, *, reason: str) -> None:
        events.append(f"watchdog:{'on' if enabled else 'off'}")

    monkeypatch.setattr(governance, "_set_watchdog_enabled", set_watchdog)
    monkeypatch.setattr(
        governance,
        "inspect_service",
        lambda _spec: events.append("inspect") or {"overall": "stale", "listener_count": 1, "instances": []},
    )
    monkeypatch.setattr(governance, "_stop_owned", lambda *_args: events.append("stop"))
    monkeypatch.setattr(governance, "listener_pids", lambda _port: events.append("port_check") or [])

    def fail_start(_spec, action):
        events.append(f"start:{action}")
        raise governance.ServiceGovernanceError("模拟启动失败")

    monkeypatch.setattr(governance, "_start_locked", fail_start)
    monkeypatch.setattr(governance, "_ensure_watchdog", lambda _spec: events.append("ensure") or "restored")

    with pytest.raises(governance.ServiceGovernanceError, match="模拟启动失败"):
        governance.restart_service(spec.service_name)

    assert events == [
        "watchdog:off",
        "inspect",
        "stop",
        "port_check",
        "start:restart",
        "watchdog:on",
        "ensure",
    ]


def test_watchdog_restarts_immediately_when_service_process_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    calls: list[str] = []
    monkeypatch.setattr(
        governance,
        "inspect_service",
        lambda _spec: {"overall": "dead", "listener_count": 0, "instances": []},
    )
    monkeypatch.setattr(
        governance,
        "_recover_service_from_watchdog",
        lambda _spec, *, reason: calls.append(reason) or True,
    )

    failures, restarts, state = governance._watchdog_tick(spec, 0, [], now=100.0)

    assert failures == 0
    assert restarts == [100.0]
    assert state == "restarted"
    assert calls == ["服务进程退出或健康检查连续失败"]


def test_watchdog_waits_for_second_unhealthy_response_before_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    calls: list[str] = []
    monkeypatch.setattr(
        governance,
        "inspect_service",
        lambda _spec: {"overall": "stale", "listener_count": 1, "instances": []},
    )
    monkeypatch.setattr(
        governance,
        "_recover_service_from_watchdog",
        lambda _spec, *, reason: calls.append(reason) or True,
    )

    failures, restarts, state = governance._watchdog_tick(spec, 0, [], now=100.0)
    assert (failures, restarts, state, calls) == (1, [], "waiting", [])

    failures, restarts, state = governance._watchdog_tick(spec, failures, restarts, now=105.0)
    assert failures == 0
    assert restarts == [105.0]
    assert state == "restarted"
    assert calls == ["服务进程退出或健康检查连续失败"]


def test_watchdog_does_not_restart_healthy_service_after_source_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源码变化由入口预检提示，不能让看门狗反复重启健康服务。"""

    spec = _spec()
    runtime = {
        "pid": 22316,
        "port": spec.port,
        "runtime_id": "runtime-a",
        "service": spec.service_name,
        "service_name": spec.service_name,
        "project_root": str(spec.project_root),
        "worktree_root": str(spec.worktree_root),
        "entry_path": str(spec.absolute_entry_path),
        "code_fingerprint": "running-source",
        "status": "ready",
    }
    info = {
        "overall": "stale",
        "listener_count": 1,
        "listener_pids": [22316],
        "metadata": dict(runtime),
        "health": dict(runtime),
        "instances": [{"pid": 22316, "classification": "stale"}],
    }
    monkeypatch.setattr(governance, "inspect_service", lambda _spec: info)
    monkeypatch.setattr(governance, "_code_fingerprint", lambda _spec: "current-source")
    monkeypatch.delenv(governance.WATCHDOG_CODE_CHANGE_ENV, raising=False)
    monkeypatch.setattr(
        governance,
        "_recover_service_from_watchdog",
        lambda *_args, **_kwargs: pytest.fail("健康服务的源码变化不得触发自动重启"),
    )

    failures, restarts, state = governance._watchdog_tick(spec, 0, [], now=100.0)

    assert (failures, restarts, state) == (0, [], "code_changed")


def test_watchdog_never_restarts_when_foreign_process_owns_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    monkeypatch.setattr(
        governance,
        "inspect_service",
        lambda _spec: {"overall": "foreign", "listener_count": 1, "instances": [{"classification": "foreign"}]},
    )
    monkeypatch.setattr(
        governance,
        "_recover_service_from_watchdog",
        lambda *_args, **_kwargs: pytest.fail("foreign 端口绝不能被自动处理"),
    )

    failures, restarts, state = governance._watchdog_tick(spec, 1, [], now=100.0)

    assert (failures, restarts, state) == (0, [], "foreign")


def test_watchdog_identity_uses_matching_lease_when_windows_hides_commandline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    metadata = {
        "service": spec.service_name,
        "pid": 22316,
        "runtime_id": "watchdog-a",
        "project_root": str(spec.project_root),
        "mode": "supervise",
    }
    lease = dict(metadata)
    monkeypatch.setattr(
        governance,
        "_json_load",
        lambda path: metadata if path.name == "watchdog_runtime.json" else lease,
    )
    monkeypatch.setattr(
        governance,
        "process_snapshot",
        lambda: {22316: {"pid": 22316, "name": "python", "executable_path": r"C:\Python\python.exe", "command_line": ""}},
    )

    assert governance._watchdog_is_current(spec)


def test_watchdog_throttles_crash_loop_without_restarting_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    monkeypatch.setattr(
        governance,
        "inspect_service",
        lambda _spec: {"overall": "dead", "listener_count": 0, "instances": []},
    )
    monkeypatch.setattr(
        governance,
        "_recover_service_from_watchdog",
        lambda *_args, **_kwargs: pytest.fail("超过上限后不得无限重启"),
    )
    prior = [96.0, 98.0, 99.0]

    failures, restarts, state = governance._watchdog_tick(spec, 0, prior, now=100.0)

    assert failures == governance.WATCHDOG_HEALTH_FAILURE_LIMIT
    assert restarts == prior
    assert state == "throttled"


def test_runtime_identity_contains_required_nonsecret_fields() -> None:
    identity = governance.runtime_identity(_spec())
    for key in (
        "service_name", "pid", "port", "project_root", "worktree_root",
        "started_at", "runtime_id", "launch_command",
    ):
        assert identity[key]
    assert "token" not in json.dumps(identity, ensure_ascii=False).lower()


def test_douyin_web_service_uses_the_real_cli_entry() -> None:
    spec = governance.SERVICE_SPECS["douyin_monitor_web"]
    assert spec.absolute_entry_path.name == "monitor.py"
    assert spec.launch_args == ("web",)


def test_v32_upgrade_lock_is_atomic_and_owner_checked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "v32-upgrade.lock"
    monkeypatch.setattr(governance, "V32_LOCK_PATH", lock)
    first = governance.acquire_v32_upgrade_lock("rev-a")
    assert first["owner"] == "codex-v32-upgrade" and lock.exists()
    with pytest.raises(governance.ServiceGovernanceError, match="拒绝并发升级"):
        governance.acquire_v32_upgrade_lock("rev-b")
    with pytest.raises(governance.ServiceGovernanceError, match="不属于当前进程"):
        governance.release_v32_upgrade_lock(pid=first["pid"] + 1)
    governance.release_v32_upgrade_lock(pid=first["pid"])
    assert not lock.exists()


def test_maintenance_marker_round_trip_is_scoped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "maintenance_requested.json"
    monkeypatch.setattr(governance, "MAINTENANCE_MARKER", marker)
    payload = governance.request_maintenance("v3.2 upgrade")
    assert governance.maintenance_status()["maintenance_requested"] is True
    assert governance.maintenance_status()["reason"] == payload["reason"]
    governance.clear_maintenance()
    assert governance.maintenance_status() == {}
