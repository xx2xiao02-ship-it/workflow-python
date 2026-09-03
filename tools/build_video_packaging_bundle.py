"""从多个 Windows 剪映母版草稿构建视频包装包。

示例：
    python tools\\build_video_packaging_bundle.py \\
      --output outputs\\video_packaging_bundles\\daily-v1 \\
      --name 日更视频包装包 --version 1.0 \\
      --opening "D:\\drafts\\片头" \\
      --image "D:\\drafts\\图片" \\
      --digital-human "D:\\drafts\\数字人" \\
      --aigc "D:\\drafts\\AIGC" \\
      --explanation "D:\\drafts\\说明镜头" \\
      --mixed-explanation "D:\\drafts\\混合说明镜头" \\
      --ending "D:\\drafts\\片尾"
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

from workflow_1256.video_packaging_bundle import (  # noqa: E402
    VideoPackagingBundleError,
    build_video_packaging_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建可复用的视频包装包")
    parser.add_argument("--output", required=True, help="新包装包目录，不能已存在")
    parser.add_argument("--name", default="", help="包装包名称")
    parser.add_argument("--version", default="1.0", help="包装包版本")
    parser.add_argument("--opening", help="片头母版草稿目录")
    parser.add_argument("--image", help="图片母版草稿目录")
    parser.add_argument("--digital-human", dest="digital_human", help="数字人母版草稿目录")
    parser.add_argument("--aigc", help="AIGC 母版草稿目录")
    parser.add_argument("--explanation", help="说明镜头母版草稿目录")
    parser.add_argument("--mixed-explanation", dest="mixed_explanation", help="混合说明镜头母版草稿目录")
    parser.add_argument("--ending", help="片尾母版草稿目录")
    parser.add_argument("--allow-missing", action="store_true", help="允许暂时缺少分类，仅用于提取测试")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sources = {
        category: value
        for category, value in {
            "opening": args.opening,
            "image": args.image,
            "digital_human": args.digital_human,
            "aigc": args.aigc,
            "explanation": args.explanation,
            "mixed_explanation": args.mixed_explanation,
            "ending": args.ending,
        }.items()
        if value
    }
    try:
        manifest = build_video_packaging_bundle(
            sources,
            args.output,
            bundle_name=args.name,
            version=args.version,
            require_all_categories=not args.allow_missing,
        )
    except VideoPackagingBundleError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({
        "status": "succeeded",
        "output_dir": manifest["output_dir"],
        "bundle_name": manifest["bundle_name"],
        "version": manifest["version"],
        "category_count": manifest["validation"]["category_count"],
        "missing_categories": manifest["validation"]["missing_categories"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
