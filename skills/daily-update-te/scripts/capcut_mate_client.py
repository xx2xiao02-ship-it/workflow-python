"""Call a CapCut Mate v1 endpoint without changing the request contract.

The client is intentionally transport-only: it does not invent defaults,
reshape workflow fields, or claim that a successful editing API call means the
whole Daily_Update_Te workflow succeeded.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


POST_ENDPOINTS = {
    "create_draft",
    "save_draft",
    "add_videos",
    "add_audios",
    "add_images",
    "add_sticker",
    "add_keyframes",
    "add_captions",
    "add_effects",
    "add_filters",
    "add_masks",
    "add_text_style",
    "easy_create_material",
    "gen_video",
    "gen_video_status",
    "get_audio_duration",
    "timelines",
    "audio_timelines",
    "audio_infos",
    "imgs_infos",
    "caption_infos",
    "effect_infos",
    "filter_infos",
    "keyframes_infos",
    "video_infos",
    "search_sticker",
    "get_url",
    "str_list_to_objs",
    "str_to_list",
    "objs_to_str_list",
}
GET_ENDPOINTS = {"get_draft", "gen_video_active_count"}
ENDPOINTS = POST_ENDPOINTS | GET_ENDPOINTS
API_SUFFIX = "/openapi/capcut-mate/v1/"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"找不到 JSON 文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 格式错误: {path}: {exc}") from exc


def _base_url(value: str | None) -> str:
    raw = (value or os.environ.get("CAPCUT_MATE_BASE_URL") or "").strip()
    if not raw:
        raise SystemExit(
            "必须提供 --base-url，或设置 CAPCUT_MATE_BASE_URL；客户端不会默认调用公网服务。"
        )
    return raw.rstrip("/") + "/"


def _endpoint_url(base: str, endpoint: str) -> str:
    if endpoint not in ENDPOINTS:
        raise SystemExit(f"不支持的 CapCut Mate endpoint: {endpoint}")
    if base.rstrip("/").endswith("/openapi/capcut-mate/v1"):
        return base.rstrip("/") + "/" + endpoint
    return urljoin(base, API_SUFFIX + endpoint)


def _write_result(result: Any, output: Path | None) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


def _request(method: str, url: str, payload: Any, timeout: float) -> Any:
    if method == "GET":
        if not isinstance(payload, dict):
            raise SystemExit("GET endpoint 的输入必须是 JSON 对象")
        query = urlencode(
            {
                key: value if isinstance(value, (str, int, float, bool)) else json.dumps(value, ensure_ascii=False)
                for key, value in payload.items()
            }
        )
        request_url = f"{url}?{query}" if query else url
        request = Request(request_url, method="GET")
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"status": response.status, "raw_body": text}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = raw
        return {"error_type": "http", "status": exc.code, "body": body}
    except URLError as exc:
        return {"error_type": "network", "reason": str(exc.reason)}
    except TimeoutError as exc:
        return {"error_type": "timeout", "reason": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="调用 CapCut Mate 剪映插件 HTTP API")
    parser.add_argument("--endpoint", choices=sorted(ENDPOINTS), required=True)
    parser.add_argument("--input", type=Path, required=True, help="请求 JSON 文件")
    parser.add_argument("--base-url", help="服务地址；也可以使用 CAPCUT_MATE_BASE_URL")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--dry-run", action="store_true", help="只显示请求，不发送网络请求")
    parser.add_argument("--output", type=Path, help="保存响应 JSON")
    args = parser.parse_args()

    payload = _load_json(args.input)
    base = _base_url(args.base_url)
    method = "POST" if args.endpoint in POST_ENDPOINTS else "GET"
    url = _endpoint_url(base, args.endpoint)

    if args.dry_run:
        _write_result({"method": method, "url": url, "payload": payload}, args.output)
        return 0

    result = _request(method, url, payload, args.timeout)
    _write_result(result, args.output)
    return 0 if not isinstance(result, dict) or "error_type" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
