"""Check the project version, revision and working-tree state before execution."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATH = ROOT / "VERSION"
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def read_version(path: Path = VERSION_PATH) -> str:
    version = path.read_text(encoding="utf-8").strip()
    if not SEMVER_RE.fullmatch(version):
        raise ValueError(f"VERSION 不是合法 SemVer：{version!r}")
    return version


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT}", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} 失败：{detail}")
    return result.stdout.strip()


def current_revision() -> str:
    return _git("rev-parse", "--verify", "HEAD")


def working_tree_entries() -> list[str]:
    output = _git("status", "--porcelain=v1", "--untracked-files=all")
    return [line for line in output.splitlines() if line.strip()]


def _relative_status_path(line: str) -> str:
    value = line[3:] if len(line) >= 3 else line
    return value.replace("\\", "/").strip()


def check(
    *,
    expected_sha: str | None = None,
    require_clean: bool = False,
    allowed_paths: tuple[str, ...] = (),
) -> dict[str, object]:
    errors: list[str] = []
    try:
        version = read_version()
    except (OSError, ValueError) as exc:
        version = ""
        errors.append(str(exc))

    try:
        revision = current_revision()
        entries = working_tree_entries()
    except RuntimeError as exc:
        revision = ""
        entries = []
        errors.append(str(exc))

    expected = str(expected_sha or "").strip().lower()
    if expected and revision.lower() != expected:
        errors.append(f"当前 commit {revision or '<unknown>'} 不等于期望 SHA {expected}")

    normalized_allowed = {path.replace("\\", "/").strip("/") for path in allowed_paths}
    unexpected_entries = [
        line
        for line in entries
        if _relative_status_path(line) not in normalized_allowed
    ]
    if require_clean and unexpected_entries:
        errors.append(
            "工作树不干净：" + ", ".join(_relative_status_path(line) for line in unexpected_entries[:20])
        )

    return {
        "status": "passed" if not errors else "failed",
        "version": version,
        "commit_sha": revision,
        "dirty": bool(unexpected_entries),
        "dirty_entries": unexpected_entries,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查项目版本、commit SHA 和工作树漂移")
    parser.add_argument("--expected-sha", default=None, help="要求当前 HEAD 等于指定 commit SHA")
    parser.add_argument("--require-clean", action="store_true", help="要求工作树没有未提交或未跟踪文件")
    parser.add_argument("--allow-path", action="append", default=[], help="允许存在的单个工作树路径，可重复传入")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出检查结果")
    args = parser.parse_args(argv)
    result = check(
        expected_sha=args.expected_sha,
        require_clean=args.require_clean,
        allowed_paths=tuple(args.allow_path),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"version={result['version']}")
        print(f"commit_sha={result['commit_sha']}")
        print(f"dirty={result['dirty']}")
        if result["errors"]:
            for error in result["errors"]:
                print(f"ERROR: {error}")
        else:
            print("Version guard: PASSED")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
