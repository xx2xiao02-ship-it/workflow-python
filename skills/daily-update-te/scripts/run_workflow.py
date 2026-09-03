"""运行 Daily_Update_Te 8364 当前已接入的工作流前缀。

默认只输出执行计划；传入 ``--execute`` 才会调用 CapCut Mate 创建草稿，随后
按本机环境配置调用当前附件版本的 directors_v2。到未接入分支会返回 blocked，
不会用空数据越过节点。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from project_root import find_project_root


PROJECT_ROOT = find_project_root()
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from workflow_1256.orchestrator import run_daily_update_te
from workflow_1256.capcut_mate_transport import CapCutMateClient


def _plan() -> dict:
    return {
        "workflow": "Daily_Update_Te",
        "archive_scope": {
            "top_level_nodes": 52,
            "internal_nodes": 9,
            "executable_nodes": 61,
            "top_level_edges": 56,
        },
        "mode": "bootstrap",
        "executable_prefix": [
            {"node_id": "182422", "name": "create_draft"},
            {"node_id": "129109", "name": "directors_v2", "transport": "current_attachment_plugin"},
        ],
        "blocked_after_prefix": [
            {"node_id": "102833", "reason": "全文视觉意图导演 LLM transport 未接入"},
            {"node_id": "159953", "reason": "speech_synthesis transport 未接入"},
            {"node_id": "197742", "reason": "镜头精细化内部 LLM transport 未接入"},
            {"node_id": "116616", "reason": "全局规划与首帧资料模型 transport 未接入"},
            {"node_id": "139488", "reason": "Host 镜头识别 LLM transport 未接入"},
        ],
        "injectable_prefix": [
            "102833", "159953", "165901", "197742", "116616", "139488",
        ],
        "known_code_pending": [
            {"node_id": "179757", "reason": "8364 导出仍是默认示例代码"},
            {"node_id": "191914", "reason": "YAML 无输入，但 audio_infos 接口要求 mp3_urls/timelines"},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Daily_Update_Te 8364 已接入工作流前缀")
    parser.add_argument("--input", type=Path, help="起始输入 JSON 文件")
    parser.add_argument("--execute", action="store_true", help="实际创建测试草稿并执行已配置 transport")
    parser.add_argument("--base-url", help="CapCut Mate 地址；也可使用 CAPCUT_MATE_BASE_URL")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    if not args.execute:
        payload = _plan()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.input is None:
        parser.error("--execute 必须同时提供 --input")

    try:
        params = json.loads(args.input.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"找不到输入 JSON：{args.input}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"输入 JSON 无效：{exc}") from exc

    client = CapCutMateClient(args.base_url) if args.base_url else None
    result = run_daily_update_te(params, capcut_client=client)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
