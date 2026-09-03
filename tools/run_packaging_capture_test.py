r"""在隔离目录执行 A/B 剪映草稿包装参数抓取。

示例（PowerShell）：

    $env:PYTHONPATH = "src"
    python tools\run_packaging_capture_test.py `
      --baseline "D:\jianying\JianyingPro Drafts\本期视频_无包装" `
      --packaged "D:\jianying\JianyingPro Drafts\本期视频_人工包装" `
      --output "outputs\packaging_capture_test\本期视频_001" `
      --replay `
      --track-role 2=transition_sfx

该命令只读取 A/B 草稿，并在指定隔离目录写入候选结果；不会修改剪映草稿、
正式包装包或当前视频制作控制台运行时。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_1256.packaging_capture import (  # noqa: E402
    PackagingCaptureError,
    capture_packaging_pair,
    write_capture_artifact,
)
from workflow_1256.packaging_replay import write_replay_artifact  # noqa: E402


def _parse_track_roles(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise argparse.ArgumentTypeError("轨道角色格式必须是 track_index=role，例如 2=transition_sfx")
        index, role = raw.split("=", 1)
        index = index.strip()
        role = role.strip()
        if not index.isdigit() or not role:
            raise argparse.ArgumentTypeError("轨道角色格式必须是 track_index=role，例如 2=transition_sfx")
        result[index] = role
    return result


def _default_output() -> Path:
    return ROOT / "outputs" / "packaging_capture_test" / time.strftime("capture_%Y%m%d_%H%M%S")


def main() -> int:
    parser = argparse.ArgumentParser(description="隔离执行 A/B 剪映包装参数抓取")
    parser.add_argument("--baseline", required=True, help="A：无包装基线草稿目录或 draft_content.json")
    parser.add_argument("--packaged", required=True, help="B：人工包装后的草稿目录或 draft_content.json")
    parser.add_argument("--output", default=str(_default_output()), help="隔离输出目录，不能已存在")
    parser.add_argument("--track-role", action="append", default=[], help="覆盖音频轨角色，例如 2=transition_sfx")
    parser.add_argument("--raw-diff-limit", type=int, default=20_000, help="原始差异最大条数")
    parser.add_argument("--replay", action="store_true", help="抓取后在输出目录生成并复核隔离回放草稿 C")
    args = parser.parse_args()
    try:
        roles = _parse_track_roles(args.track_role)
        result = capture_packaging_pair(
            args.baseline,
            args.packaged,
            track_role_overrides=roles,
            raw_diff_limit=args.raw_diff_limit,
        )
        artifact = write_capture_artifact(result, args.output)
        replay = None
        if args.replay:
            replay = write_replay_artifact(args.baseline, args.packaged, result, args.output)
    except (PackagingCaptureError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 2

    summary = {
        "status": result.get("status"),
        "output_dir": artifact["output_dir"],
        "raw_change_count": result.get("raw_diff", {}).get("change_count", 0),
        "raw_diff_truncated": result.get("raw_diff", {}).get("truncated", False),
        "semantic_change_count": result.get("semantic_diff", {}).get("changed_count", 0),
        "resource_review_count": len(result.get("semantic_diff", {}).get("resource_review") or []),
        "track_roles_need_confirmation": bool(result.get("track_roles", {}).get("needs_confirmation")),
        "compatibility": result.get("compatibility", {}),
        "replay": replay,
        "files": artifact["files"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    replay_ok = replay is None or replay.get("status") == "verified"
    return 0 if result.get("status") in {"ready_for_review", "pending_review"} and replay_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
