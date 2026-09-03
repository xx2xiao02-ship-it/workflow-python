"""Run an offline Daily_Update_Te node contract and optionally compare JSON.

This runner deliberately supports only deterministic Python nodes. Network,
model, task-query, and draft-writing nodes require an explicitly injected
transport in the project implementation and are not silently simulated here.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Callable

from project_root import find_project_root


NODE_NAMES = (
    "timeline_planning",
    "end_frame_extension",
    "prompt_generation",
    "video_infos",
    "scene_type_recognition",
    "host_task_assembly",
    "bgm_task_assembly",
    "video_data_aggregation",
    "keyframes_infos",
    "variable_merge",
)


def _find_project_root() -> Path:
    return find_project_root()


def _load_runner(name: str) -> Callable[[Any], Any]:
    if name == "timeline_planning":
        from workflow_1256.timeline_planning import run_timeline_planning

        return run_timeline_planning
    if name == "end_frame_extension":
        from workflow_1256.end_frame_extension import run_end_frame_extension

        return run_end_frame_extension
    if name == "prompt_generation":
        from workflow_1256.prompt_generation import run_prompt_generation

        return run_prompt_generation
    if name == "video_infos":
        from workflow_1256.video_infos import run_video_infos

        return run_video_infos
    if name == "scene_type_recognition":
        from workflow_1256.scene_type_recognition import run_scene_type_recognition

        return run_scene_type_recognition
    if name == "host_task_assembly":
        from workflow_1256.host_task_assembly import run_host_task_assembly

        return run_host_task_assembly
    if name == "bgm_task_assembly":
        from workflow_1256.bgm_task_assembly import run_bgm_task_assembly

        return run_bgm_task_assembly
    if name == "video_data_aggregation":
        from workflow_1256.video_data_aggregation import run_video_data_aggregation

        return run_video_data_aggregation
    if name == "keyframes_infos":
        from workflow_1256.keyframes_infos import run_keyframes_infos

        return run_keyframes_infos
    if name == "variable_merge":
        from workflow_1256.variable_merge import run_variable_merge

        return run_variable_merge
    raise ValueError(f"不支持的离线节点: {name}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"找不到 JSON 文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 格式错误: {path}: {exc}") from exc


def _run_maybe_async(function: Callable[[Any], Any], value: Any) -> Any:
    result = function(value)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _compare(expected: Any, actual: Any) -> dict[str, Any]:
    from audit.equivalence import compare_outputs

    mismatches = compare_outputs(expected, actual)
    return {"equal": not mismatches, "mismatches": mismatches}


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Daily_Update_Te 离线节点契约")
    parser.add_argument("--list-nodes", action="store_true", help="列出可运行节点")
    parser.add_argument("--node", choices=NODE_NAMES, help="节点名称")
    parser.add_argument("--input", type=Path, help="节点输入 JSON 文件")
    parser.add_argument("--expected", type=Path, help="期望输出 JSON 文件，启用逐字段比较")
    parser.add_argument("--output", type=Path, help="将实际输出写入 JSON 文件")
    args = parser.parse_args()

    if args.list_nodes:
        print("\n".join(NODE_NAMES))
        return 0
    if not args.node or not args.input:
        parser.error("运行节点时必须同时提供 --node 和 --input")

    root = _find_project_root()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "src"))
    runner = _load_runner(args.node)
    actual = _run_maybe_async(runner, _read_json(args.input))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    if args.expected:
        report = _compare(_read_json(args.expected), actual)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("equal") else 1

    print(json.dumps(actual, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
