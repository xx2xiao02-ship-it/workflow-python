"""断点发布已经裁切完成的宫格首帧，禁止重新调用 Image2。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:  # ``python tools\\...py`` 时 tools 目录本身位于 sys.path 首位。
    from tools.run_token_assets_live import publish_existing_grid_images
except ModuleNotFoundError:  # pragma: no cover - 真实命令行入口分支
    from run_token_assets_live import publish_existing_grid_images


def main() -> int:
    parser = argparse.ArgumentParser(description="将已有 first_frame_shot_*.png 发布到 TOS")
    parser.add_argument("--images-dir", type=Path, required=True)
    parser.add_argument("--auth-document", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    paths = sorted(args.images_dir.glob("first_frame_shot_*.*"))
    if not paths:
        raise SystemExit("未找到已裁切首帧；拒绝重新生图")

    def progress(event: dict) -> None:
        print(json.dumps(event, ensure_ascii=False), flush=True)

    urls = publish_existing_grid_images(paths, args.auth_document, args.state, progress_reporter=progress)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(
        json.dumps({"image_paths": [str(path.resolve()) for path in paths], "image_urls": urls}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"status": "succeeded", "image_count": len(paths)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
