"""Windows CLI：从基线草稿和用户修改后的草稿提取编辑风格包。

示例（已安装 capcut-cli）：
    python tools\\extract_editing_style_package.py
      --baseline "D:\\drafts\\baseline"
      --edited "D:\\drafts\\edited"
      --output "outputs\\editing_style_package.json"

如果只需要验证本地草稿结构，可显式加 ``--raw-draft``；这不会伪装成
capcut-cli 实际验收。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from workflow_1256.editing_style_package import (  # noqa: E402
    EditingStylePackageError,
    extract_editing_style_package,
    write_style_package,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 Windows 剪映草稿提取可复用编辑风格包")
    parser.add_argument("--baseline", help="基线草稿目录或 draft_content.json；可省略")
    parser.add_argument("--edited", required=True, help="用户修改后的草稿目录或 draft_content.json")
    parser.add_argument("--output", required=True, help="输出 editing_style_package.json")
    parser.add_argument("--name", default="", help="风格包名称")
    parser.add_argument("--capcut-command", default="", help="capcut-cli 命令，例如 capcut 或 npx.cmd --yes capcut-cli@latest")
    parser.add_argument("--timeout", type=float, default=60.0, help="单条 capcut-cli 命令超时秒数")
    parser.add_argument("--raw-draft", action="store_true", help="只读原始 draft_content.json，不调用 capcut-cli")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        package = extract_editing_style_package(
            args.baseline,
            args.edited,
            name=args.name,
            capcut_command=args.capcut_command or None,
            timeout=args.timeout,
            raw_draft_only=args.raw_draft,
        )
        output = write_style_package(package, args.output)
    except EditingStylePackageError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({
        "status": "succeeded",
        "output": str(output),
        "backend": package["source"]["backend"],
        "comparison": package["source"]["comparison"],
        "changed_section_count": package["changes"]["changed_section_count"],
        "warnings": package["warnings"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
