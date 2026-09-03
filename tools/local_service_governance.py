"""Windows 本地服务单实例治理。

只负责进程、端口、实例归属和运行时元数据，不触碰业务数据。
无法证明进程属于当前项目时，一律按 foreign 处理，禁止结束。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
# .codex 在部分桌面沙箱中由宿主账户拥有，业务进程无法稳定写入；
# 使用项目运行时目录保存等价门禁/锁，仍保持固定文件名并支持原子创建。
MAINTENANCE_MARKER = ROOT / ".runtime-governance-live" / "maintenance_requested.json"
V32_LOCK_PATH = ROOT / ".runtime-governance-live" / "locks" / "v32-upgrade.lock"


def maintenance_status() -> dict[str, Any]:
    """读取维护门禁；缺失或损坏时视为未请求。"""
    try:
        value = json.loads(MAINTENANCE_MARKER.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def request_maintenance(reason: str = "") -> dict[str, Any]:
    payload = {"maintenance_requested": True, "reason": reason, "requested_at": _now(), "owner": "codex-v32-upgrade"}
    _json_write(MAINTENANCE_MARKER, payload, exclusive=False)
    return payload


def clear_maintenance() -> None:
    _unlink(MAINTENANCE_MARKER)


def acquire_v32_upgrade_lock(source_revision: str = "") -> dict[str, Any]:
    """以 O_EXCL 原子创建 v3.2 升级锁，已有锁绝不覆盖。"""
    payload = {"owner": "codex-v32-upgrade", "pid": os.getpid(), "purpose": "v3.2 编导包与音效模块升级", "created_at": _now(), "source_revision": source_revision}
    try:
        _json_write(V32_LOCK_PATH, payload, exclusive=True)
    except FileExistsError as exc:
        raise ServiceGovernanceError("已有 v32-upgrade.lock，拒绝并发升级") from exc
    return payload


def release_v32_upgrade_lock(pid: int | None = None) -> None:
    current = _json_load(V32_LOCK_PATH)
    if current and pid is not None and int(current.get("pid", -1)) != int(pid):
        raise ServiceGovernanceError("升级锁不属于当前进程，拒绝释放")
    _unlink(V32_LOCK_PATH)
DEFAULT_PYTHON = ROOT / "services" / "douyin-monitor" / ".venv" / "Scripts" / "python.exe"
# 所有启动入口共享同一份项目服务治理元数据；否则 Administrator 与
# CodexSandboxOnline 各自写 AppData/工作区 runtime，会互相把对方判成 foreign。
DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get("LOCAL_SERVICE_RUNTIME_ROOT", str(ROOT / ".runtime-governance-live" / "service-control"))
).expanduser()

# 自动恢复只看本地服务的端口和健康接口，不读取、更改或重新提交业务任务。
# 两次连续健康失败才重启，避免一次短暂请求超时导致无谓中断；服务进程已经
# 退出（端口不再监听）时则立即恢复。重启窗口用于阻止源码/环境错误造成无限
# 拉起循环，窗口结束后仍会再次尝试。
WATCHDOG_INTERVAL_SECONDS = 5.0
WATCHDOG_HEALTH_FAILURE_LIMIT = 2
WATCHDOG_RESTART_WINDOW_SECONDS = 300.0
WATCHDOG_MAX_RESTARTS_IN_WINDOW = 3
# 源码指纹变化只表示当前进程尚未加载最新代码，不等于服务不健康。
# 默认由入口预检提示人工受控重启，避免开发期间每次保存文件都触发
# 停止/启动和任务中断；需要自动换版本时再显式设置为 1。
WATCHDOG_CODE_CHANGE_ENV = "LOCAL_SERVICE_WATCHDOG_RESTART_ON_CODE_CHANGE"
# 当前 Windows 会话若无法读取 Win32_Process，后续检查直接走轻量回退，
# 避免每 5 秒重复启动 PowerShell 并等待 CIM 超时。
_PROCESS_SNAPSHOT_MODE = "unknown"


@dataclass(frozen=True)
class ServiceSpec:
    service_name: str
    port: int
    entry_path: Path
    health_path: str
    runtime_dir_name: str
    python_path: Path = DEFAULT_PYTHON
    host: str = "127.0.0.1"
    launch_args: tuple[str, ...] = ()

    @property
    def project_root(self) -> Path:
        return ROOT.resolve()

    @property
    def worktree_root(self) -> Path:
        return _worktree_root(self.project_root)

    @property
    def absolute_entry_path(self) -> Path:
        return (self.project_root / self.entry_path).resolve()

    @property
    def runtime_dir(self) -> Path:
        override = os.environ.get("LOCAL_SERVICE_RUNTIME_ROOT", "").strip()
        return (Path(override) if override else DEFAULT_RUNTIME_ROOT) / self.runtime_dir_name


SERVICE_SPECS: dict[str, ServiceSpec] = {
    "video_production_console": ServiceSpec(
        "video_production_console", 8768, Path("tools/video_production_console.py"),
        "/api/executor/health", "video_production_console"
    ),
    "capcut_capability_center": ServiceSpec(
        "capcut_capability_center", 8766, Path("tools/capcut_capability_center.py"),
        "/api/service/health", "capcut_capability_center"
    ),
    "douyin_monitor_web": ServiceSpec(
        "douyin_monitor_web", 8080, Path("services/douyin-monitor/monitor.py"),
        "/api/service/health", "douyin_monitor_web", launch_args=("web",)
    ),
}


class ServiceGovernanceError(RuntimeError):
    """可安全展示给用户的治理错误。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: str | Path | None) -> str:
    if value is None:
        return ""
    try:
        return str(Path(value).resolve(strict=False)).rstrip("\\/").lower()
    except (OSError, RuntimeError, TypeError):
        return str(value).rstrip("\\/").lower()


def _worktree_root(project_root: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return project_root.resolve()


def _git_commit(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _code_fingerprint(spec: ServiceSpec) -> str:
    digest = hashlib.sha256()
    for path in (spec.absolute_entry_path, Path(__file__).resolve()):
        try:
            digest.update(str(path).encode("utf-8"))
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return ""
    return digest.hexdigest()


def _json_load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _json_write(path: Path, payload: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        pass


def _parse_process_timestamp(value: object) -> datetime | None:
    """将锁文件和 Windows 进程快照中的时间统一为 UTC。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # Win32_Process.CreationDate 在部分 Windows 环境中是
        # ``yyyyMMddHHmmss.ffffff+|||``，这里只取稳定的 14 位主体。
        match = re.match(r"^(\d{14})", text)
        if not match:
            return None
        try:
            parsed = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _start_lock_owner_is_live(
    existing: dict[str, Any], owner_pid: int, processes: dict[int, dict[str, Any]],
) -> bool:
    """判断启动锁是否仍由同一代进程持有，避免 Windows PID 复用误阻塞。"""
    owner = processes.get(owner_pid)
    if not owner:
        return False
    lock_created = _parse_process_timestamp(existing.get("created_at"))
    process_created = _parse_process_timestamp(owner.get("creation_date"))
    if lock_created is not None and process_created is not None:
        # 同一 PID 但进程是在锁之后才创建的，说明是 PID 复用，旧锁可回收。
        if process_created > lock_created:
            return False
    # 无法取得可比较时间时保持原来的保守策略：只要 PID 仍存在就阻塞。
    return True


def _paths(spec: ServiceSpec) -> dict[str, Path]:
    directory = spec.runtime_dir
    return {
        "directory": directory,
        "metadata": directory / "service_runtime.json",
        "lease": directory / "service_runtime.lock",
        "start_lock": directory / "service_start.lock",
        "watchdog_control": directory / "watchdog_control.json",
        "watchdog_metadata": directory / "watchdog_runtime.json",
        "watchdog_lease": directory / "watchdog_runtime.lock",
        "watchdog_log": directory / "watchdog.log",
        "log": directory / "service_governance.log",
    }


def _audit(spec: ServiceSpec, action: str, result: str, reason: str, **extra: Any) -> None:
    paths = _paths(spec)
    paths["directory"].mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "timestamp": _now(), "action": action, "service": spec.service_name,
        "pid": os.getpid(), "port": spec.port, "project_root": str(spec.project_root),
        "worktree_root": str(spec.worktree_root), "result": result, "reason": reason,
    }
    record.update(extra)
    try:
        with paths["log"].open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _watchdog_control_enabled(spec: ServiceSpec) -> bool:
    """默认关闭；只有受控 ``start/restart`` 才会显式开启自动恢复。"""
    control = _json_load(_paths(spec)["watchdog_control"])
    return bool(control and control.get("enabled") is True)


def _set_watchdog_enabled(spec: ServiceSpec, enabled: bool, *, reason: str) -> None:
    _json_write(_paths(spec)["watchdog_control"], {
        "service": spec.service_name,
        "project_root": str(spec.project_root),
        "enabled": enabled,
        "updated_at": _now(),
        "reason": reason,
    })
    _audit(
        spec,
        "watchdog_control",
        "enabled" if enabled else "disabled",
        reason,
    )


def _watchdog_identity(spec: ServiceSpec) -> dict[str, Any]:
    return {
        "service": spec.service_name,
        "pid": os.getpid(),
        "started_at": _now(),
        "runtime_id": uuid.uuid4().hex,
        "project_root": str(spec.project_root),
        "entry_path": str(Path(__file__).resolve()),
        "port": spec.port,
        "mode": "supervise",
    }


def _watchdog_is_current(spec: ServiceSpec) -> bool:
    """仅将能证明属于本项目的守护进程视为活动，避免重复启动。"""
    metadata = _json_load(_paths(spec)["watchdog_metadata"])
    if not metadata or str(metadata.get("service") or "") != spec.service_name:
        return False
    try:
        pid = int(metadata.get("pid", 0))
    except (TypeError, ValueError):
        return False
    process = process_snapshot().get(pid)
    if not _process_is_python(process):
        return False
    lease = _json_load(_paths(spec)["watchdog_lease"])
    try:
        lease_pid = int((lease or {}).get("pid", 0) or 0)
    except (TypeError, ValueError):
        lease_pid = 0
    metadata_identity_matches = bool(
        lease
        and str(lease.get("runtime_id") or "") == str(metadata.get("runtime_id") or "")
        and str(lease.get("service") or "") == spec.service_name
        and _canonical(lease.get("project_root")) == _canonical(spec.project_root)
        and lease_pid == pid
    )
    command = str((process or {}).get("command_line") or "").lower().replace("/", "\\")
    root = _canonical(spec.project_root).replace("/", "\\")
    governor = _canonical(Path(__file__).resolve()).replace("/", "\\")
    if command:
        return (
            metadata_identity_matches
            and root in command
            and governor in command
            and "supervise" in command
            and spec.service_name.lower() in command
        )
    # 某些受限 Windows 会退化为 Get-Process，无法读取命令行。此时只有
    # 租约和运行时元数据完全一致才认可，避免反复启动多个守护进程。
    return metadata_identity_matches and str(metadata.get("mode") or "") == "supervise"


def _powershell_json(script: str, *, timeout: float = 12) -> Any:
    command = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); " + script
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def process_snapshot() -> dict[int, dict[str, Any]]:
    """读取 Windows 进程信息；读取失败时返回空，调用方必须保守处理。"""
    global _PROCESS_SNAPSHOT_MODE
    # 在受限 Windows 会话中，枚举 Win32_Process 可能长时间卡住并最终
    # Access Denied；只在首次检查时尝试一次。后续直接使用 Get-Process，
    # 避免看门狗每轮重复等待 CIM 超时。权限恢复时可通过重启治理进程
    # 重新探测，无需修改任何业务数据。
    raw: Any = []
    if _PROCESS_SNAPSHOT_MODE != "fallback":
        raw = _powershell_json(r"""
$items = @(
  Get-CimInstance Win32_Process | ForEach-Object {
    [pscustomobject]@{
      pid = [int]$_.ProcessId
      parent_pid = [int]$_.ParentProcessId
      name = [string]$_.Name
      executable_path = [string]$_.ExecutablePath
      command_line = [string]$_.CommandLine
      creation_date = [string]$_.CreationDate
    }
  }
)
if ($items.Count -eq 0) { '[]' } else { $items | ConvertTo-Json -Compress -Depth 4 }
""", timeout=3)
        _PROCESS_SNAPSHOT_MODE = "cim" if raw else "fallback"
    if _PROCESS_SNAPSHOT_MODE == "fallback":
        # 受限用户/沙箱可能不能读取 Win32_Process。只读取 PID 和进程名，
        # 避免逐个访问 Path/StartTime 导致进程表再次卡住；命令行、路径、
        # 父 PID 缺失时，归属必须依赖本服务自己的 runtime metadata，不能
        # 凭 Python 猜测。
        raw = _powershell_json(r"""
$items = @(
  Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{
      pid = [int]$_.Id
      parent_pid = 0
      name = [string]$_.ProcessName
      executable_path = ''
      command_line = ''
      creation_date = ''
    }
  }
)
if ($items.Count -eq 0) { '[]' } else { $items | ConvertTo-Json -Compress -Depth 4 }
""", timeout=3)
    values = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    result: dict[int, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            pid = int(value.get("pid", 0))
        except (TypeError, ValueError):
            continue
        if pid > 0:
            result[pid] = value
    return result


def listener_pids(port: int) -> list[int]:
    """返回 TCP LISTENING 状态下目标端口的唯一 PID。"""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
            encoding="mbcs", errors="replace", timeout=8, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pattern = re.compile(
        r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$",
        re.IGNORECASE,
    )
    values: set[int] = set()
    for line in result.stdout.splitlines():
        match = pattern.match(line)
        if match and int(match.group(1)) == port:
            values.add(int(match.group(2)))
    return sorted(values)


def _is_project_process(process: dict[str, Any] | None, spec: ServiceSpec) -> bool:
    if not process:
        return False
    if not _process_is_python(process):
        return False
    command = str(process.get("command_line") or "").lower().replace("/", "\\")
    executable = str(process.get("executable_path") or "").lower().replace("/", "\\")
    root = _canonical(spec.project_root).replace("/", "\\")
    entry_name = spec.absolute_entry_path.name.lower()
    entry_path = _canonical(spec.absolute_entry_path).replace("/", "\\")
    return entry_name in command and (root in command or root in executable or entry_path in command)


def _process_is_python(process: dict[str, Any] | None) -> bool:
    if not process:
        return False
    name = str(process.get("name") or "").lower()
    executable = str(process.get("executable_path") or "").lower()
    return name.startswith("python") or executable.endswith("\\python.exe") or "pythoncore" in executable


def _metadata_owns_identity(
    spec: ServiceSpec,
    metadata: dict[str, Any] | None,
    health: dict[str, Any] | None,
    pid: int,
) -> bool:
    """受限权限下只接受服务自己写入的完整身份链。"""
    if not metadata or not health:
        return False
    try:
        if int(metadata.get("pid", 0)) != pid or int(health.get("pid", 0)) != pid:
            return False
        if int(metadata.get("port", 0)) != spec.port or int(health.get("port", 0)) != spec.port:
            return False
    except (TypeError, ValueError):
        return False
    if str(metadata.get("runtime_id") or "") != str(health.get("runtime_id") or ""):
        return False
    if not str(metadata.get("runtime_id") or "").strip():
        return False
    if str(health.get("service") or health.get("service_name")) != spec.service_name:
        return False
    if _canonical(metadata.get("project_root")) != _canonical(spec.project_root):
        return False
    if _canonical(metadata.get("worktree_root")) != _canonical(spec.worktree_root):
        return False
    if _canonical(health.get("project_root")) != _canonical(spec.project_root):
        return False
    if _canonical(health.get("worktree_root")) != _canonical(spec.worktree_root):
        return False
    if _canonical(metadata.get("entry_path")) != _canonical(spec.absolute_entry_path):
        return False
    # 这里仅判断“进程是否属于当前项目”。代码版本是否过期由
    # _identity_matches() 单独判断，否则旧版本会被误判为 foreign，
    # 受控 restart 无法替换当前项目自己的旧实例。
    return True


def _metadata_parent_is_owned(
    spec: ServiceSpec,
    metadata: dict[str, Any] | None,
    process: dict[str, Any] | None,
    pid: int,
) -> bool:
    if not metadata or not process:
        return False
    try:
        parent_pid = int(metadata.get("parent_pid", 0))
    except (TypeError, ValueError):
        return False
    if parent_pid != pid:
        return False
    executable = str(process.get("executable_path") or "")
    command = str(process.get("command_line") or "")
    return _canonical(spec.project_root) in _canonical(executable) or _canonical(spec.project_root) in _canonical(command)


def _health(spec: ServiceSpec) -> dict[str, Any] | None:
    try:
        request = Request(f"http://{spec.host}:{spec.port}{spec.health_path}", method="GET")
        with urlopen(request, timeout=2.5) as response:
            if int(getattr(response, "status", 200)) != 200:
                return None
            value = json.loads(response.read().decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None


def runtime_identity(
    spec: ServiceSpec, *, runtime_id: str | None = None, started_at: str | None = None,
    launch_command: str | None = None,
) -> dict[str, Any]:
    configured_launch_command = (
        launch_command or os.environ.get("LOCAL_SERVICE_LAUNCH_COMMAND") or ""
    ).strip()
    # 直接导入/测试时不要把 sys.argv（可能包含测试路径、临时参数甚至
    # 凭据）写进运行时身份。正式受控启动会通过
    # LOCAL_SERVICE_LAUNCH_COMMAND 传入完整且已脱敏的启动命令。
    safe_launch_command = configured_launch_command or (
        f'"{sys.executable}" "{spec.absolute_entry_path}"'
    )
    return {
        "status": "ready", "service": spec.service_name, "service_name": spec.service_name,
        "pid": os.getpid(), "parent_pid": os.getppid(),
        "started_at": started_at or os.environ.get("LOCAL_SERVICE_STARTED_AT") or _now(),
        "runtime_id": runtime_id or os.environ.get("LOCAL_SERVICE_RUNTIME_ID") or uuid.uuid4().hex,
        "project_root": str(spec.project_root), "worktree_root": str(spec.worktree_root),
        "entry_path": str(spec.absolute_entry_path), "port": spec.port,
        "git_commit": os.environ.get("LOCAL_SERVICE_GIT_COMMIT") or _git_commit(spec.project_root),
        "code_fingerprint": _code_fingerprint(spec),
        "launch_command": safe_launch_command,
    }


def _identity_matches(
    spec: ServiceSpec,
    health: dict[str, Any] | None,
    process: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    pid: int,
) -> bool:
    if not health:
        return False
    # Windows 权限隔离时，监听 PID 可能存在，但当前会话无法读取该进程
    # 的详细信息（process=None）。只要 health 与本服务自己写入的 metadata
    # 在 PID、端口、runtime_id、工作目录和入口上完全一致，即可证明它是
    # 当前实例；如果拿到了进程信息却发现不是 Python，则仍然严格拒绝。
    if process is not None and not _process_is_python(process):
        return False
    metadata_identity = _metadata_owns_identity(spec, metadata, health, pid)
    if process is None and not metadata_identity:
        return False
    if not _is_project_process(process, spec) and not metadata_identity:
        return False
    if str(health.get("service") or health.get("service_name")) != spec.service_name:
        return False
    try:
        if int(health.get("port")) != spec.port or int(health.get("pid")) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if _canonical(health.get("project_root")) != _canonical(spec.project_root):
        return False
    if _canonical(health.get("worktree_root")) != _canonical(spec.worktree_root):
        return False
    current_commit = _git_commit(spec.project_root)
    health_commit = str(health.get("git_commit") or "")
    if current_commit and health_commit and current_commit != health_commit:
        return False
    current_fingerprint = _code_fingerprint(spec)
    health_fingerprint = str(health.get("code_fingerprint") or "")
    if current_fingerprint and health_fingerprint and current_fingerprint != health_fingerprint:
        return False
    return bool(str(health.get("runtime_id") or "").strip())


def _watchdog_restart_on_code_change_enabled() -> bool:
    """是否允许看门狗因源码指纹变化自动重启服务。"""

    return os.environ.get(WATCHDOG_CODE_CHANGE_ENV, "").strip() == "1"


def _healthy_service_with_code_drift(
    spec: ServiceSpec, info: dict[str, Any]
) -> bool:
    """判断“服务仍健康，仅运行代码落后于当前工作区”。

    该判断只接受单一监听者，并要求健康接口和服务自己写入的运行时元数据
    在 PID、端口、runtime_id、工作目录及入口上完全一致。这样不会把 foreign
    进程或多实例端口占用误当成可忽略的代码变化。
    """

    listeners = info.get("listener_pids") or []
    if len(listeners) != 1:
        return False
    health = info.get("health")
    metadata = info.get("metadata")
    if not isinstance(health, dict) or not isinstance(metadata, dict):
        return False
    if str(health.get("status") or "ready") != "ready":
        return False
    try:
        pid = int(listeners[0])
    except (TypeError, ValueError):
        return False
    if not _metadata_owns_identity(spec, metadata, health, pid):
        return False
    running_fingerprint = str(health.get("code_fingerprint") or "").strip()
    current_fingerprint = _code_fingerprint(spec)
    return bool(
        running_fingerprint
        and current_fingerprint
        and running_fingerprint != current_fingerprint
    )


def _pid_chain(pid: int, processes: dict[int, dict[str, Any]]) -> list[int]:
    chain: list[int] = []
    seen: set[int] = set()
    current = pid
    while current > 0 and current not in seen:
        seen.add(current)
        chain.append(current)
        process = processes.get(current)
        if not process:
            break
        try:
            current = int(process.get("parent_pid", 0))
        except (TypeError, ValueError):
            break
    return chain


def inspect_service(spec: ServiceSpec) -> dict[str, Any]:
    processes = process_snapshot()
    listeners = listener_pids(spec.port)
    metadata = _json_load(_paths(spec)["metadata"])
    health = _health(spec) if listeners else None
    instances: list[dict[str, Any]] = []
    seen: set[int] = set()
    for pid in listeners:
        process = processes.get(pid)
        owned = _is_project_process(process, spec) or _metadata_owns_identity(spec, metadata, health, pid)
        classification = (
            "current" if owned and _identity_matches(spec, health, process, metadata, pid)
            else "stale" if owned else "foreign"
        )
        instances.append({
            "pid": pid,
            "parent_pid": process.get("parent_pid") if process else None,
            "name": process.get("name") if process else None,
            "executable_path": process.get("executable_path") if process else None,
            "command_line": process.get("command_line") if process else None,
            "creation_date": process.get("creation_date") if process else None,
            "worktree": str(spec.worktree_root) if owned else None,
            "classification": classification,
            "pid_tree": _pid_chain(pid, processes),
        })
        seen.add(pid)
    if metadata:
        try:
            metadata_pid = int(metadata.get("pid", 0))
        except (TypeError, ValueError):
            metadata_pid = 0
        if metadata_pid and metadata_pid not in seen and metadata_pid in processes:
            process = processes.get(metadata_pid)
            owned = _is_project_process(process, spec)
            instances.append({
                "pid": metadata_pid,
                "parent_pid": process.get("parent_pid") if process else None,
                "name": process.get("name") if process else None,
                "executable_path": process.get("executable_path") if process else None,
                "command_line": process.get("command_line") if process else None,
                "creation_date": process.get("creation_date") if process else None,
                "worktree": str(spec.worktree_root) if owned else None,
                "classification": "stale" if owned else "foreign",
                "pid_tree": _pid_chain(metadata_pid, processes),
            })
    classifications = {str(item["classification"]) for item in instances}
    overall = (
        "foreign" if "foreign" in classifications else
        "current" if "current" in classifications else
        "stale" if "stale" in classifications else
        "dead" if metadata else "idle"
    )
    return {
        "service_name": spec.service_name, "host": spec.host, "port": spec.port,
        "project_root": str(spec.project_root), "worktree_root": str(spec.worktree_root),
        "entry_path": str(spec.absolute_entry_path), "health_path": spec.health_path,
        "metadata": metadata, "health": health, "listener_count": len(listeners),
        "listener_pids": listeners, "instances": instances, "overall": overall,
        "runtime_dir": str(spec.runtime_dir),
    }


def _dead_runtime_cleanup(spec: ServiceSpec, info: dict[str, Any]) -> None:
    if info.get("listener_count") or not info.get("metadata"):
        return
    metadata = info["metadata"]
    try:
        pid = int(metadata.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    if pid and pid in process_snapshot():
        return
    paths = _paths(spec)
    _unlink(paths["metadata"])
    _unlink(paths["lease"])
    _audit(spec, "cleanup_dead_runtime", "succeeded", "PID 已死亡且端口已释放", dead_pid=pid)


@contextmanager
def _start_lock(spec: ServiceSpec) -> Iterator[None]:
    path = _paths(spec)["start_lock"]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": os.getpid(), "created_at": _now(), "service": spec.service_name}
    try:
        _json_write(path, payload, exclusive=True)
    except FileExistsError:
        existing = _json_load(path) or {}
        try:
            owner_pid = int(existing.get("pid", 0))
        except (TypeError, ValueError):
            owner_pid = 0
        processes = process_snapshot()
        if owner_pid and _start_lock_owner_is_live(existing, owner_pid, processes):
            raise ServiceGovernanceError(f"服务 {spec.service_name} 正在被另一个治理操作占用，未重复启动。")
        _unlink(path)
        _json_write(path, payload, exclusive=True)
    try:
        yield
    finally:
        current = _json_load(path)
        if current and int(current.get("pid", 0) or 0) == os.getpid():
            _unlink(path)


def _owned_top_targets(spec: ServiceSpec, info: dict[str, Any], processes: dict[int, dict[str, Any]]) -> list[int]:
    targets: set[int] = set()
    for item in info.get("instances", []):
        if item.get("classification") not in {"current", "stale"}:
            continue
        try:
            pid = int(item.get("pid", 0))
        except (TypeError, ValueError):
            continue
        chain = _pid_chain(pid, processes)
        top = pid
        for ancestor in chain[1:]:
            if _is_project_process(processes.get(ancestor), spec) or _metadata_parent_is_owned(
                spec, info.get("metadata"), processes.get(ancestor), ancestor
            ):
                top = ancestor
            else:
                break
        metadata = info.get("metadata") or {}
        try:
            parent_pid = int(metadata.get("parent_pid", 0))
        except (TypeError, ValueError):
            parent_pid = 0
        # 只有监听进程的实时父子关系仍然指向 metadata 父进程时，才可以
        # 结束父进程树。CIM 不可读时会退化为 parent_pid=0；此时如果仍然
        # 盲信旧 metadata，会结束一个已脱离服务的终端，而 8768 监听者仍在。
        live_process = processes.get(pid) or {}
        try:
            live_parent_pid = int(live_process.get("parent_pid", 0) or 0)
        except (TypeError, ValueError):
            live_parent_pid = 0
        if parent_pid and parent_pid == live_parent_pid and parent_pid in processes and _metadata_parent_is_owned(
            spec, metadata, processes.get(parent_pid), parent_pid
        ):
            top = parent_pid
        elif (
            # 在受限 Windows 会话中，进程快照会退化为 parent_pid=0、空 command_line，
            # 但服务启动时已经把经过身份校验的完整 pid_tree 写入 runtime metadata。
            # 仅在这份由服务自身写入的树包含 metadata.parent_pid，且父进程路径仍属于
            # 当前项目时，才使用该树回收父进程；普通缺少 pid_tree 的旧 metadata
            # 继续按保守策略只结束监听进程，避免误杀 PID 复用的外部终端。
            parent_pid
            and not live_parent_pid
            and parent_pid in processes
            and parent_pid in {
                int(value)
                for value in (metadata.get("pid_tree") or [])
                if str(value).strip().isdigit()
            }
            and _metadata_parent_is_owned(spec, metadata, processes.get(parent_pid), parent_pid)
        ):
            top = parent_pid
        targets.add(top)
    return sorted(targets)


def _wait_for_port(spec: ServiceSpec, expected_listening: bool, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bool(listener_pids(spec.port)) == expected_listening:
            return True
        time.sleep(0.25)
    return bool(listener_pids(spec.port)) == expected_listening


def _stop_owned(spec: ServiceSpec, info: dict[str, Any], reason: str) -> None:
    processes = process_snapshot()
    targets = _owned_top_targets(spec, info, processes)
    if not targets:
        _dead_runtime_cleanup(spec, info)
        return
    _audit(spec, "stop", "started", reason, targets=targets, instances=info.get("instances", []))
    for target in targets:
        subprocess.run(["taskkill", "/PID", str(target), "/T"], capture_output=True, text=True,
                       encoding="mbcs", errors="replace", timeout=8, check=False)
    if _wait_for_port(spec, False, 5):
        _unlink(_paths(spec)["lease"])
        _unlink(_paths(spec)["metadata"])
        _audit(spec, "stop", "succeeded", reason, targets=targets, forced=False)
        return
    # 某些受限 Windows 会话中 taskkill 返回但没有真正结束同一用户的
    # Python 服务（历史上会让 8768 看门狗反复卡在旧实例上）。目标已经
    # 通过项目身份链校验，这里用 PowerShell Stop-Process 做一次精确兜底，
    # 不按端口盲杀，也不扩大到其它 Python 进程。
    for target in targets:
        stop_script = (
            f"Stop-Process -Id {int(target)} -Force -ErrorAction SilentlyContinue"
        )
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", stop_script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False,
        )
    if _wait_for_port(spec, False, 5):
        _unlink(_paths(spec)["lease"])
        _unlink(_paths(spec)["metadata"])
        _audit(spec, "stop", "succeeded", reason, targets=targets, forced=True,
               method="powershell_stop_process")
        return
    for target in targets:
        subprocess.run(["taskkill", "/PID", str(target), "/T", "/F"], capture_output=True,
                       text=True, encoding="mbcs", errors="replace", timeout=8, check=False)
    if not _wait_for_port(spec, False, 10):
        latest = inspect_service(spec)
        _audit(spec, "stop", "failed", "端口仍被占用", targets=targets, latest=latest)
        raise ServiceGovernanceError(f"{spec.service_name} 停止失败：端口 {spec.port} 仍被占用。")
    _unlink(_paths(spec)["lease"])
    _unlink(_paths(spec)["metadata"])
    _audit(spec, "stop", "succeeded", reason, targets=targets, forced=True)


def _launch_environment(spec: ServiceSpec) -> dict[str, str]:
    started_at = _now()
    runtime_id = uuid.uuid4().hex
    args = " ".join(f'"{arg}"' for arg in spec.launch_args)
    command = f'"{spec.python_path}" "{spec.absolute_entry_path}"{(" " + args) if args else ""}'
    env = os.environ.copy()
    env.update({
        "LOCAL_SERVICE_RUNTIME_ID": runtime_id, "LOCAL_SERVICE_STARTED_AT": started_at,
        "LOCAL_SERVICE_PROJECT_ROOT": str(spec.project_root),
        "LOCAL_SERVICE_WORKTREE_ROOT": str(spec.worktree_root),
        "LOCAL_SERVICE_NAME": spec.service_name, "LOCAL_SERVICE_PORT": str(spec.port),
        "LOCAL_SERVICE_GIT_COMMIT": _git_commit(spec.project_root),
        "LOCAL_SERVICE_LAUNCH_COMMAND": command,
    })
    return env


def _wait_for_current(spec: ServiceSpec, runtime_id: str, timeout: float = 35.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = inspect_service(spec)
        health = info.get("health")
        if info.get("overall") == "current" and health and str(health.get("runtime_id")) == runtime_id:
            return info
        time.sleep(0.5)
    return None


def _write_started_metadata(spec: ServiceSpec, info: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    health = dict(info.get("health") or {})
    health.update({
        "service_name": spec.service_name, "port": spec.port,
        "project_root": str(spec.project_root), "worktree_root": str(spec.worktree_root),
        "entry_path": str(spec.absolute_entry_path),
        "launch_command": env["LOCAL_SERVICE_LAUNCH_COMMAND"],
        "pid_tree": (info.get("instances") or [{}])[0].get("pid_tree", []),
        "health_checked_at": _now(),
    })
    _json_write(_paths(spec)["metadata"], health)
    return health


def _spawn(spec: ServiceSpec, env: dict[str, str]) -> None:
    log_path = _paths(spec)["directory"] / "executor_startup.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        try:
            subprocess.Popen(
                [str(spec.python_path), str(spec.absolute_entry_path), *spec.launch_args],
                cwd=str(spec.project_root), env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ServiceGovernanceError(f"启动 {spec.service_name} 失败：{exc}") from exc


class WatchdogLease:
    """守护进程的独占租约；与服务进程租约分离，避免彼此误删。"""

    def __init__(self, spec: ServiceSpec, identity: dict[str, Any], path: Path) -> None:
        self.spec, self.identity, self.path = spec, identity, path
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        current = _json_load(self.path)
        if current and current.get("runtime_id") == self.identity.get("runtime_id") and int(
            current.get("pid", 0) or 0
        ) == os.getpid():
            _unlink(self.path)
            _unlink(_paths(self.spec)["watchdog_metadata"])
            _audit(
                self.spec,
                "watchdog_lease_release",
                "succeeded",
                "守护进程正常退出",
                runtime_id=self.identity.get("runtime_id"),
            )
        self.released = True


def _acquire_watchdog_lease(spec: ServiceSpec) -> WatchdogLease | None:
    """守护进程单实例化。返回 ``None`` 表示已有活动守护进程。"""
    paths = _paths(spec)
    paths["directory"].mkdir(parents=True, exist_ok=True)
    existing = _json_load(paths["watchdog_lease"])
    if existing:
        try:
            old_pid = int(existing.get("pid", 0))
        except (TypeError, ValueError):
            old_pid = 0
        if old_pid and old_pid in process_snapshot():
            return None
        _unlink(paths["watchdog_lease"])
        _unlink(paths["watchdog_metadata"])
    identity = _watchdog_identity(spec)
    try:
        _json_write(paths["watchdog_lease"], identity, exclusive=True)
    except FileExistsError:
        return None
    _json_write(paths["watchdog_metadata"], identity)
    _audit(
        spec,
        "watchdog_lease_acquire",
        "succeeded",
        "守护进程已取得自动恢复租约",
        runtime_id=identity["runtime_id"],
    )
    return WatchdogLease(spec, identity, paths["watchdog_lease"])


def _spawn_watchdog(spec: ServiceSpec) -> None:
    """启动与业务服务隔离的后台守护进程，不触碰任何任务数据。"""
    log_path = _paths(spec)["watchdog_log"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LOCAL_SERVICE_WATCHDOG"] = "1"
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        try:
            subprocess.Popen(
                [str(spec.python_path), str(Path(__file__).resolve()), "supervise", "--service", spec.service_name],
                cwd=str(spec.project_root),
                env=env,
                stdout=log,
                stderr=log,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ServiceGovernanceError(f"启动 {spec.service_name} 自动恢复守护进程失败：{exc}") from exc


def _ensure_watchdog(spec: ServiceSpec) -> str:
    """确保服务已有一个受控守护进程；无需重启当前健康服务。"""
    if _watchdog_is_current(spec):
        _audit(spec, "watchdog_start", "reused", "已有活动自动恢复守护进程")
        return "reused"
    _spawn_watchdog(spec)
    _audit(spec, "watchdog_start", "started", "已启动自动恢复守护进程")
    return "started"


def _wait_for_service_current(spec: ServiceSpec, timeout: float = 35.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = inspect_service(spec)
        if info.get("overall") == "current":
            return info
        time.sleep(0.5)
    return None


def _recover_service_from_watchdog(spec: ServiceSpec, *, reason: str) -> bool:
    """在同一单实例锁内恢复服务，绝不创建或重跑任何业务任务。"""
    with _start_lock(spec):
        # stop_service() 可能恰好发生在守护进程等待这把锁期间；取得锁后
        # 必须再次读取开关，确保手动停止绝不会被自动拉起覆盖。
        if not _watchdog_control_enabled(spec):
            _audit(spec, "auto_restart", "cancelled", "服务已被手动停止，取消自动恢复")
            return False
        info = inspect_service(spec)
        if info.get("overall") == "current":
            return True
        if info.get("overall") == "foreign":
            _audit(spec, "auto_restart", "blocked", "foreign 进程占用端口", inspection=info)
            return False
        if info.get("overall") == "stale":
            _stop_owned(spec, info, f"自动恢复前停止无健康响应的旧实例：{reason}")
        else:
            _dead_runtime_cleanup(spec, info)
        if listener_pids(spec.port):
            _audit(spec, "auto_restart", "blocked", "端口未释放", inspection=inspect_service(spec))
            return False
        _start_locked(spec, action="auto_restart")
        _audit(spec, "auto_restart", "succeeded", reason)
        return True


def _watchdog_tick(
    spec: ServiceSpec,
    consecutive_failures: int,
    restart_timestamps: list[float],
    *,
    now: float | None = None,
) -> tuple[int, list[float], str]:
    """执行一次只读健康检查，必要时走受控服务恢复。"""
    current_time = time.monotonic() if now is None else now
    recent_restarts = [
        stamp for stamp in restart_timestamps
        if current_time - stamp < WATCHDOG_RESTART_WINDOW_SECONDS
    ]
    info = inspect_service(spec)
    overall = str(info.get("overall") or "idle")
    if overall == "current":
        return 0, recent_restarts, "healthy"
    if (
        overall == "stale"
        and _healthy_service_with_code_drift(spec, info)
        and not _watchdog_restart_on_code_change_enabled()
    ):
        _audit(
            spec,
            "auto_restart_check",
            "code_changed",
            "运行服务健康但源码指纹已变化，等待入口预检后的受控重启",
            running_fingerprint=str((info.get("health") or {}).get("code_fingerprint") or ""),
            current_fingerprint=_code_fingerprint(spec),
        )
        return 0, recent_restarts, "code_changed"
    if overall == "foreign":
        _audit(spec, "auto_restart", "blocked", "foreign 进程占用端口", inspection=info)
        return 0, recent_restarts, "foreign"

    # 端口仍在而实例不健康时，先连续确认两次；进程/端口已经消失时可立即拉起。
    failures = consecutive_failures + 1 if int(info.get("listener_count") or 0) else WATCHDOG_HEALTH_FAILURE_LIMIT
    if failures < WATCHDOG_HEALTH_FAILURE_LIMIT:
        _audit(
            spec,
            "auto_restart_check",
            "waiting",
            "健康接口暂不可用，等待下一次确认",
            consecutive_failures=failures,
            inspection=info,
        )
        return failures, recent_restarts, "waiting"
    if len(recent_restarts) >= WATCHDOG_MAX_RESTARTS_IN_WINDOW:
        _audit(
            spec,
            "auto_restart",
            "throttled",
            "重启次数达到安全上限，等待窗口结束后再尝试",
            restart_count=len(recent_restarts),
            inspection=info,
        )
        return failures, recent_restarts, "throttled"
    try:
        recovered = _recover_service_from_watchdog(
            spec,
            reason="服务进程退出或健康检查连续失败",
        )
    except ServiceGovernanceError as exc:
        _audit(spec, "auto_restart", "failed", str(exc), inspection=inspect_service(spec))
        return failures, recent_restarts, "failed"
    if not recovered:
        return failures, recent_restarts, "blocked"
    recent_restarts.append(current_time)
    return 0, recent_restarts, "restarted"


def supervise_service(service_name: str) -> int:
    """守护模式入口：只在 ``start/restart`` 显式开启后持续运行。"""
    spec = SERVICE_SPECS[service_name]
    if not _watchdog_control_enabled(spec):
        _audit(spec, "watchdog", "disabled", "未启用自动恢复，不启动守护循环")
        return 0
    lease = _acquire_watchdog_lease(spec)
    if lease is None:
        _audit(spec, "watchdog", "reused", "已有活动自动恢复守护进程")
        return 0
    consecutive_failures = 0
    restart_timestamps: list[float] = []
    try:
        while _watchdog_control_enabled(spec):
            try:
                consecutive_failures, restart_timestamps, _state = _watchdog_tick(
                    spec,
                    consecutive_failures,
                    restart_timestamps,
                )
            except Exception as exc:  # 守护进程不能因一次状态读取异常而退出。
                _audit(spec, "watchdog", "failed", f"守护检查异常：{type(exc).__name__}")
            time.sleep(WATCHDOG_INTERVAL_SECONDS)
    finally:
        lease.release()
    return 0


def _start_locked(spec: ServiceSpec, *, action: str) -> dict[str, Any]:
    info = inspect_service(spec)
    if info["overall"] == "current":
        _audit(spec, action, "reused", "当前实例身份校验通过")
        return {"action": "reused", "service": spec.service_name, "inspection": info}
    if info["overall"] == "foreign":
        _audit(spec, action, "blocked", "foreign 进程占用端口", inspection=info)
        raise ServiceGovernanceError(f"端口 {spec.port} 被 foreign 进程占用，已阻止启动。")
    if info["overall"] == "stale":
        _stop_owned(spec, info, "发现当前项目旧实例，先停止并释放端口")
    else:
        _dead_runtime_cleanup(spec, info)
    if listener_pids(spec.port):
        raise ServiceGovernanceError(f"端口 {spec.port} 清理后仍被占用，未启动第二实例。")
    env = _launch_environment(spec)
    _spawn(spec, env)
    info = _wait_for_current(spec, env["LOCAL_SERVICE_RUNTIME_ID"])
    if info is None:
        latest = inspect_service(spec)
        if latest.get("overall") == "stale":
            _stop_owned(spec, latest, "新进程未通过身份校验，回收错误实例")
        _audit(spec, action, "failed", "PID、端口、health 或工作树校验未通过", inspection=latest)
        raise ServiceGovernanceError(f"{spec.service_name} 启动后未通过完整校验，请查看 executor_startup.log。")
    metadata = _write_started_metadata(spec, info, env)
    _audit(spec, action, "succeeded", "新实例通过完整身份校验", new_instance=metadata)
    return {"action": "started" if action == "start" else "restarted", "service": spec.service_name,
            "inspection": info}


def start_service(service_name: str) -> dict[str, Any]:
    spec = SERVICE_SPECS[service_name]
    with _start_lock(spec):
        before = inspect_service(spec)
        if before.get("overall") == "foreign":
            _audit(spec, "start", "blocked", "foreign 进程占用端口", inspection=before)
            raise ServiceGovernanceError(f"端口 {spec.port} 被 foreign 进程占用，已阻止启动。")
        _set_watchdog_enabled(spec, True, reason="受控启动时启用后台失败自动恢复")
        watchdog = _ensure_watchdog(spec)
    info = _wait_for_service_current(spec)
    if info is None:
        latest = inspect_service(spec)
        # 进程快照/权限隔离可能让轮询耗尽等待窗口，但最后一次检查已
        # 证明新实例通过完整身份校验。此时不能把已成功启动报告成失败，
        # 否则调用方会重复点击并制造无谓的治理竞争。
        if latest.get("overall") == "current":
            info = latest
        else:
            _audit(spec, "start", "failed", "守护进程启动后服务未通过完整校验", inspection=latest)
            raise ServiceGovernanceError(f"{spec.service_name} 未能在自动恢复守护进程下完成启动，请查看 watchdog.log 和 executor_startup.log。")
    action = "reused" if before.get("overall") == "current" else "started"
    _audit(spec, "start", "succeeded", "服务已通过守护进程健康校验", watchdog=watchdog)
    return {"action": action, "service": spec.service_name, "watchdog": watchdog, "inspection": info}


def stop_service(service_name: str) -> dict[str, Any]:
    spec = SERVICE_SPECS[service_name]
    with _start_lock(spec):
        _set_watchdog_enabled(spec, False, reason="受控停止时关闭后台失败自动恢复")
        info = inspect_service(spec)
        if info["overall"] == "foreign":
            _audit(spec, "stop", "blocked", "foreign 监听者存在", inspection=info)
            raise ServiceGovernanceError(f"端口 {spec.port} 有 foreign 进程，未执行停止。")
        if info["overall"] in {"current", "stale"}:
            _stop_owned(spec, info, "执行受控停止")
            return {"action": "stopped", "service": service_name, "inspection": inspect_service(spec)}
        _dead_runtime_cleanup(spec, info)
        _audit(spec, "stop", "noop", "没有发现当前项目监听实例")
        return {"action": "noop", "service": service_name, "inspection": inspect_service(spec)}


def restart_service(service_name: str) -> dict[str, Any]:
    spec = SERVICE_SPECS[service_name]
    with _start_lock(spec):
        # 重启期间必须先关闭看门狗。旧顺序是“先启用/启动看门狗，再停旧服务”，
        # 当停止因 Windows 权限或端口释放延迟而变慢时，看门狗会与人工重启
        # 同时争抢同一端口，表现为重启卡住、旧进程反复拉起甚至假死。
        # 这里保持启动锁覆盖整个停启过程，成功启动后再恢复自动恢复。
        _set_watchdog_enabled(spec, False, reason="受控重启期间暂停后台自动恢复，避免与停启过程竞争")
        try:
            info = inspect_service(spec)
            if info["overall"] == "foreign":
                _audit(spec, "restart", "blocked", "foreign 进程占用端口", inspection=info)
                raise ServiceGovernanceError(f"端口 {spec.port} 有 foreign 进程，未执行重启。")
            if info["overall"] in {"current", "stale"}:
                _stop_owned(spec, info, "重启前停止旧实例")
            else:
                _dead_runtime_cleanup(spec, info)
            if listener_pids(spec.port):
                raise ServiceGovernanceError(f"端口 {spec.port} 未释放，未启动新实例。")
            result = _start_locked(spec, action="restart")
        except Exception:
            # 停止/启动任一步骤失败时，不能把服务留在“无看门狗”状态。
            # 恢复动作只写控制文件、取得看门狗租约，不重新提交业务任务；
            # 原始异常继续向调用方抛出，便于定位真正的端口或权限问题。
            try:
                _set_watchdog_enabled(spec, True, reason="受控重启失败后恢复后台自动恢复")
                _ensure_watchdog(spec)
            except Exception as restore_exc:
                _audit(spec, "watchdog_restore", "failed", f"重启失败后的看门狗恢复失败：{type(restore_exc).__name__}")
            raise
        _set_watchdog_enabled(spec, True, reason="受控重启完成后恢复后台失败自动恢复")
        watchdog = _ensure_watchdog(spec)
        result["watchdog"] = watchdog
        return result


def cleanup_service(service_name: str) -> dict[str, Any]:
    spec = SERVICE_SPECS[service_name]
    with _start_lock(spec):
        info = inspect_service(spec)
        if info["overall"] == "foreign":
            _audit(spec, "cleanup", "blocked", "foreign 监听者存在", inspection=info)
            raise ServiceGovernanceError(f"端口 {spec.port} 有 foreign 进程，未执行清理。")
        if info["overall"] == "stale":
            _stop_owned(spec, info, "清理当前项目旧实例")
        else:
            _dead_runtime_cleanup(spec, info)
        _audit(spec, "cleanup", "succeeded", "只处理当前项目实例和死亡运行时文件")
        return {"action": "cleaned", "service": service_name, "inspection": inspect_service(spec)}


class ServerLease:
    def __init__(self, spec: ServiceSpec, identity: dict[str, Any], path: Path) -> None:
        self.spec, self.identity, self.path = spec, identity, path
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        current = _json_load(self.path)
        if current and current.get("runtime_id") == self.identity.get("runtime_id") and int(
            current.get("pid", 0) or 0
        ) == os.getpid():
            _unlink(self.path)
            _audit(self.spec, "server_lease_release", "succeeded", "服务进程正常退出",
                   runtime_id=self.identity.get("runtime_id"))
        self.released = True


def acquire_server_lease(spec: ServiceSpec, identity: dict[str, Any] | None = None) -> ServerLease:
    """服务真正 bind 端口前调用，防止绕过 CLI 直接重复启动。"""
    paths = _paths(spec)
    paths["directory"].mkdir(parents=True, exist_ok=True)
    processes = process_snapshot()
    existing = _json_load(paths["lease"])
    if existing:
        try:
            old_pid = int(existing.get("pid", 0))
        except (TypeError, ValueError):
            old_pid = 0
        if old_pid and old_pid in processes:
            if _is_project_process(processes.get(old_pid), spec):
                raise ServiceGovernanceError(f"{spec.service_name} 已有活动租约 PID={old_pid}，拒绝重复启动。")
            raise ServiceGovernanceError(f"{spec.service_name} 的租约 PID={old_pid} 不属于当前项目，拒绝覆盖。")
        _unlink(paths["lease"])
    listeners = listener_pids(spec.port)
    if listeners:
        raise ServiceGovernanceError(f"端口 {spec.port} 已有监听者 {listeners}，服务进程不会猜测或结束它们。")
    actual = identity or runtime_identity(spec)
    try:
        _json_write(paths["lease"], actual, exclusive=True)
    except FileExistsError as exc:
        raise ServiceGovernanceError(f"{spec.service_name} 启动租约竞争，拒绝重复启动。") from exc
    _json_write(paths["metadata"], actual)
    _audit(spec, "server_lease_acquire", "succeeded", "服务 bind 前取得单实例租约",
           runtime_id=actual.get("runtime_id"))
    return ServerLease(spec, actual, paths["lease"])


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Windows 本地服务单实例治理")
    parser.add_argument("action", choices=("status", "start", "stop", "restart", "cleanup", "supervise"))
    parser.add_argument("--service", default="video_production_console", choices=tuple(SERVICE_SPECS))
    args = parser.parse_args()
    spec = SERVICE_SPECS[args.service]
    if args.action == "supervise":
        return supervise_service(args.service)
    try:
        if args.action == "status":
            result = inspect_service(spec)
        elif args.action == "start":
            result = start_service(args.service)
        elif args.action == "stop":
            result = stop_service(args.service)
        elif args.action == "restart":
            result = restart_service(args.service)
        else:
            result = cleanup_service(args.service)
    except ServiceGovernanceError as exc:
        result = {"action": args.action, "service": args.service, "status": "blocked",
                  "message": str(exc), "inspection": inspect_service(spec)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
