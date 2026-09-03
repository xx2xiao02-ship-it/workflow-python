"""视频制作控制台入口登记与只读预检。

这里是“打开页面”时的唯一解析来源。生产页面必须先解析到主服务，
再通过健康接口确认服务归属，禁止根据目录名或页面标题猜测入口。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.local_service_governance import SERVICE_SPECS, _code_fingerprint  # noqa: E402


class ConsoleEntryError(RuntimeError):
    """入口登记或主服务预检失败。"""


@dataclass(frozen=True)
class ConsolePage:
    key: str
    title: str
    path: str
    url: str
    service_name: str
    port: int
    entry_path: str
    role: str = "main_production"


MAIN_SPEC = SERVICE_SPECS["video_production_console"]
MAIN_ENTRY_PATH = str(MAIN_SPEC.absolute_entry_path)
MAIN_BASE_URL = f"http://{MAIN_SPEC.host}:{MAIN_SPEC.port}"


PRODUCTION_PAGES: dict[str, ConsolePage] = {
    key: ConsolePage(
        key=key,
        title=title,
        path=path,
        url=f"{MAIN_BASE_URL}{path}",
        service_name=MAIN_SPEC.service_name,
        port=MAIN_SPEC.port,
        entry_path=MAIN_ENTRY_PATH,
    )
    for key, title, path in (
        ("topic-center", "选题中心", "/topic-center"),
        ("writing", "文案创作", "/writing"),
        ("director", "编导审核", "/director"),
        ("assets", "素材生产", "/assets"),
        ("editing", "剪辑交付", "/editing"),
    )
}


# 这些入口保留用于识别和治理，但不能被解析为生产页面。
NON_PRODUCTION_SURFACES: dict[str, dict[str, Any]] = {
    "site-video-production-console": {
        "status": "unmanaged_prototype",
        "production": False,
        "url": "http://localhost:3000",
        "entry_path": "site-video-production-console/app/page.tsx",
        "reason": "未注册到主服务；是独立 Vinext/Sites UI 原型，不能作为生产编导入口，也不得由生产入口自动打开。",
    },
    "video-production-console-dev": {
        "status": "dev_read_only",
        "production": False,
        "url": "http://127.0.0.1:8770",
        "entry_path": "tools/run_video_production_console_dev.py",
        "reason": "只读页面冒烟实例；不得占用主服务端口 8768。",
    },
}


def resolve_page(page: str) -> ConsolePage:
    """将业务页面别名解析为唯一生产入口。"""

    try:
        return PRODUCTION_PAGES[page.strip().lower()]
    except KeyError as exc:
        known = ", ".join(sorted(PRODUCTION_PAGES))
        raise ConsoleEntryError(f"未知生产页面 {page!r}；允许值：{known}") from exc


def _health_url() -> str:
    return f"{MAIN_BASE_URL}{MAIN_SPEC.health_path}"


def preflight(page: str, *, timeout: float = 3.0) -> dict[str, Any]:
    """只读核对服务健康状态和入口归属。"""

    entry = resolve_page(page)
    request = Request(_health_url(), headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            health = json.loads(raw)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ConsoleEntryError(f"主服务预检失败：{_health_url()}；{exc}") from exc

    if not isinstance(health, dict):
        raise ConsoleEntryError("主服务健康接口返回的不是 JSON 对象")
    if health.get("status") != "ready":
        raise ConsoleEntryError(f"主服务未就绪：status={health.get('status')!r}")
    if health.get("service_name") != entry.service_name:
        raise ConsoleEntryError(
            "服务归属不匹配："
            f"期望 {entry.service_name!r}，实际 {health.get('service_name')!r}"
        )
    if int(health.get("port", -1)) != entry.port:
        raise ConsoleEntryError(
            f"服务端口不匹配：期望 {entry.port}，实际 {health.get('port')!r}"
        )
    reported_entry = str(health.get("entry_path", ""))
    if not reported_entry or Path(reported_entry).resolve() != Path(entry.entry_path).resolve():
        raise ConsoleEntryError(
            "服务入口文件不匹配："
            f"期望 {entry.entry_path!r}，实际 {reported_entry!r}"
        )
    # 健康接口带有启动时源码指纹时，必须和当前工作区一致；否则页面可能
    # 仍由旧进程提供，不能把已修改但未重启的服务报告为 ready。旧版/测试
    # 响应未提供指纹时保留原有入口校验行为。
    reported_fingerprint = str(health.get("code_fingerprint") or "").strip()
    current_fingerprint = _code_fingerprint(MAIN_SPEC)
    if reported_fingerprint and current_fingerprint and reported_fingerprint != current_fingerprint:
        raise ConsoleEntryError(
            "主服务代码版本过旧：运行进程 code_fingerprint 与当前源码不一致；"
            "请使用单实例治理器重启 video_production_console"
        )

    return {
        "status": "ready",
        "page": asdict(entry),
        "health_url": _health_url(),
        "health": health,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析并预检视频制作控制台生产页面")
    parser.add_argument("page", choices=sorted(PRODUCTION_PAGES), help="页面别名")
    parser.add_argument("--check", action="store_true", help="额外检查主服务健康和入口归属")
    args = parser.parse_args(argv)

    try:
        result = preflight(args.page) if args.check else {"page": asdict(resolve_page(args.page))}
    except ConsoleEntryError as exc:
        print(json.dumps({"status": "blocked", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
