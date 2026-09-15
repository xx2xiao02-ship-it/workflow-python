"""F0 服务治理：单实例维护锁和活动任务状态。"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import utc_now
from .errors import PersistenceError


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError):
        return False
    return True


class ServiceLock:
    """使用原子创建文件避免两个目标服务共用可写运行目录。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._owned = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                value = {}
            pid = int(value.get("pid") or 0) if isinstance(value, dict) else 0
            if _pid_running(pid):
                raise PersistenceError(
                    f"目标服务已有活动实例（PID {pid}），未强制结束；请先等待其自然退出并释放维护锁。",
                    code="maintenance_blocked",
                    http_status=423,
                )
            raise PersistenceError(
                "检测到未确认归属的遗留维护锁；为避免误删其他任务，未自动清理。",
                code="maintenance_lock_stale",
                http_status=423,
            )
        payload = {"pid": os.getpid(), "started_at": utc_now(), "service": "topic-center-migration"}
        try:
            descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            self._owned = True
        except FileExistsError as exc:
            raise PersistenceError("维护锁在启动期间被其他实例占用", code="maintenance_blocked", http_status=423) from exc
        except OSError as exc:
            raise PersistenceError("无法创建目标服务维护锁", code="maintenance_lock_error", http_status=503) from exc

    def release(self) -> None:
        if not self._owned:
            return
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        if isinstance(current, dict) and int(current.get("pid") or 0) == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._owned = False

    @property
    def owned(self) -> bool:
        return self._owned


@dataclass
class ServiceGovernance:
    lock: ServiceLock
    active_tasks: int = 0
    maintenance_requested: bool = False
    _guard: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def request_maintenance(self) -> dict[str, object]:
        with self._guard:
            self.maintenance_requested = True
            return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        with self._guard:
            return {
                "service": "topic-center-migration",
                "pid": os.getpid(),
                "active_tasks": int(self.active_tasks),
                "maintenance_requested": bool(self.maintenance_requested),
                "maintenance_lock": "held" if self.lock.owned else "released",
                "task_policy": "本批不自动启动采集、生成或定时任务",
            }
