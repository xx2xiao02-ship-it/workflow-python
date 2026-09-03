"""风格包采集/转写/蒸馏独立 worker。

8768 只负责创建任务和读取快照。本进程持有采集器、Whisper 和蒸馏模型的
运行时资源，因此控制台重启不会自动中断已启动的风格包任务。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _write_heartbeat(console, task_id: str, *, state: str, **extra: object) -> None:
    target = console.STYLE_WORKER_HEARTBEAT_DIR / f"{task_id}.json"
    existing = {}
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    payload = {
        "kind": "style_collection_worker",
        "task_id": task_id,
        "pid": os.getpid(),
        "state": state,
        "project_root": str(ROOT.resolve()),
        "worktree_root": str(console.SERVICE_SPEC.worktree_root),
        "heartbeat_unix": time.time(),
        **extra,
    }
    if isinstance(existing, dict) and existing.get("launcher_pid") and "launcher_pid" not in payload:
        payload["launcher_pid"] = existing["launcher_pid"]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    except OSError:
        # 任务快照仍是主状态；心跳写入失败不能中断采集。
        pass


def _heartbeat_loop(console, task_id: str, stop: threading.Event) -> None:
    while not stop.is_set():
        _write_heartbeat(console, task_id, state="running")
        stop.wait(10)


def _worker_metadata(console, task_id: str, *, state: str, **extra: object) -> dict[str, object]:
    task = console._task_snapshot(task_id) or {}
    worker = dict(task.get("worker") or {}) if isinstance(task, dict) else {}
    marker = console._read_style_worker_heartbeat(task_id)
    if isinstance(marker, dict) and marker.get("launcher_pid") and not worker.get("launcher_pid"):
        worker["launcher_pid"] = marker["launcher_pid"]
    worker.update({
        "kind": "style_collection_worker",
        "task_id": task_id,
        "pid": __import__("os").getpid(),
        "state": state,
        "entry_path": str(Path(__file__).resolve()),
        "project_root": str(ROOT.resolve()),
        "worktree_root": str(console.SERVICE_SPEC.worktree_root),
    })
    worker.update(extra)
    return worker


def run(task_id: str) -> int:
    import tools.video_production_console as console

    # Write the real child PID before any task work.  On Windows the venv
    # launcher PID returned by Popen may differ from this interpreter PID.
    _write_heartbeat(console, task_id, state="starting")
    task = console._task_snapshot(task_id)
    if not isinstance(task, dict) or task.get("task_type") != "style_collection":
        return 2
    if task.get("state") not in {"queued", "running"}:
        return 0

    console._set_task(task_id, worker=_worker_metadata(console, task_id, state="running"))
    payload = dict(task.get("input") or {})
    checkpoint = console._style_task_checkpoint(task)
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(target=_heartbeat_loop, args=(console, task_id, heartbeat_stop), daemon=True)
    heartbeat_thread.start()
    try:
        console._run_style_task(task_id, payload, checkpoint or None)
        return 0
    except BaseException as exc:  # pragma: no cover - defensive process boundary.
        console.TASK_LOGGER.exception("style worker %s crashed", task_id)
        console._set_task(
            task_id,
            state="failed",
            stage="style_worker_crashed",
            message=f"蒸馏独立进程异常：{type(exc).__name__}",
        )
        return 1
    finally:
        snapshot = console._task_snapshot(task_id) or {}
        console._set_task(
            task_id,
            worker=_worker_metadata(
                console,
                task_id,
                state="exited",
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                final_state=str(snapshot.get("state") or "unknown"),
            ),
        )
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        _write_heartbeat(
            console,
            task_id,
            state="exited",
            final_state=str(snapshot.get("state") or "unknown"),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one independent style collection worker")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    return run(str(args.task_id).strip())


if __name__ == "__main__":
    raise SystemExit(main())
