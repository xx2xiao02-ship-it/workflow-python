"""启动真实 Chrome 抖音登录窗口，并把登录状态同步给网页。"""

import asyncio
import json
import os
import struct
import time
from pathlib import Path

from playwright.async_api import async_playwright

from browser_runtime import browser_launch_options
from config_manager import load_config


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
# The collector reads this exact persistent profile.  Keep login and crawling
# on one profile; otherwise a successful QR login is never visible to crawler.
SESSION_DIR = BASE_DIR / "douyin_session"
STATUS_PATH = DATA_DIR / "douyin_login_status.json"
QR_PATH = DATA_DIR / "douyin_login_qr.png"
LOGIN_TIMEOUT_SECONDS = 300
# 这些 Cookie 只用于判断本次扫码是否真正建立了登录会话；绝不写入
# 状态文件，也不向网页返回 Cookie 内容。
AUTH_COOKIE_NAMES = frozenset(
    {
        "sessionid",
        "sessionid_ss",
        "sid_guard",
        "uid_tt",
        "uid_tt_ss",
        "passport_auth_status",
    }
)


def write_status(status: str, message: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "status": status,
            "message": message,
            "pid": os.getpid(),
            "updated_at": time.time(),
        },
        ensure_ascii=False,
    )
    temp_path = STATUS_PATH.with_suffix(".tmp")
    temp_path.write_text(payload, encoding="utf-8")
    temp_path.replace(STATUS_PATH)


async def save_login_qr(page) -> bool:
    """把当前登录页可见二维码截图成网页服务可读取的 PNG。"""

    selectors = (
        'img[src^="data:image/"]',
        'canvas',
        'svg',
        '[style*="data:image"]',
    )
    # 登录页会同时渲染 22px 左右的品牌图标、头像等 data-image 元素。
    # 旧实现按 DOM 顺序截图第一个可见元素，结果把图标当成二维码（无法扫码）。
    # 只接受足够大的方形候选，并按面积从大到小尝试；截图后再用像素
    # 密度筛掉“菜单/图标”这类看起来像二维码但实际不可扫码的节点。
    candidates = []
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = await locator.count()
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                # 保留对最小化测试桩/旧 Playwright 版本的兼容；真实页面元素
                # 都支持 bounding_box，只有无法取得尺寸时才走这个兜底。
                if not hasattr(candidate, "bounding_box"):
                    candidates.append((1.0, candidate, None))
                    continue
                box = await candidate.bounding_box()
                if not box:
                    continue
                width = float(box.get("width") or 0)
                height = float(box.get("height") or 0)
                if width < 120 or height < 120:
                    continue
                # 抖音页面会留下很窄的 svg/装饰节点；它们不是二维码。
                ratio = width / height if height else 0
                if ratio < 0.55 or ratio > 1.8:
                    continue
                area = width * height
                candidates.append((area, candidate, box))
            except Exception:
                continue
    if not candidates:
        return False
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _area, candidate, box in candidates:
      try:
        temp_path = QR_PATH.with_suffix(".tmp.png")
        # 对 SVG/canvas 优先使用页面裁剪截图，避免 Edge 对元素截图只输出
        # 2×15 之类的占位图；旧测试桩没有 bounding_box 时仍走元素截图。
        if hasattr(candidate, "bounding_box"):
            if box:
                await page.screenshot(
                    path=str(temp_path),
                    clip={
                        "x": max(0, float(box.get("x") or 0)),
                        "y": max(0, float(box.get("y") or 0)),
                        "width": float(box.get("width") or 0),
                        "height": float(box.get("height") or 0),
                    },
                )
            else:
                await candidate.screenshot(path=str(temp_path))
        else:
            await candidate.screenshot(path=str(temp_path))
        if temp_path.is_file() and temp_path.stat().st_size > 0:
            # 有真实尺寸时再校验一次，阻止无效的窄条图覆盖上一张二维码。
            dimensions = _png_dimensions(temp_path)
            if dimensions and (dimensions[0] < 120 or dimensions[1] < 120):
                temp_path.unlink(missing_ok=True)
                continue
            looks_like_qr = _looks_like_qr(temp_path)
            if looks_like_qr is False:
                temp_path.unlink(missing_ok=True)
                continue
            temp_path.replace(QR_PATH)
            return True
      except Exception:
        temp_path.unlink(missing_ok=True)
        continue
    return False


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    """读取 PNG 头部尺寸；失败时返回 None（兼容测试桩输出）。"""

    try:
        header = path.read_bytes()[:24]
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return struct.unpack(">II", header[16:24])
    except (OSError, struct.error):
        return None


def _looks_like_qr(path: Path) -> bool | None:
    """粗略检查黑白像素密度，过滤被误选的图标/菜单截图。"""

    try:
        from PIL import Image, ImageOps

        image = Image.open(path).convert("L")
        image.thumbnail((96, 96))
        pixels = list(ImageOps.grayscale(image).getdata())
        if not pixels:
            return False
        dark_ratio = sum(value < 100 for value in pixels) / len(pixels)
        # 真正二维码通常包含大量交错黑白模块；纯白面板或少量图标会远低于此值。
        return 0.12 <= dark_ratio <= 0.78
    except Exception:
        # 不因可选图像库缺失阻断旧环境/测试桩。
        return None


async def _auth_cookie_state(context) -> tuple[bool, tuple[tuple[str, str], ...]]:
    """Return whether the persistent browser has a strong Douyin auth cookie."""

    try:
        cookies = await context.cookies()
    except Exception:
        return False, ()
    relevant = tuple(
        sorted(
            (
                str(item.get("name") or ""),
                str(item.get("value") or ""),
            )
            for item in cookies
            if str(item.get("name") or "") in AUTH_COOKIE_NAMES
            and str(item.get("value") or "")
        )
    )
    return bool(relevant), relevant


async def _all_cookie_state(context) -> tuple[tuple[str, str, str], ...]:
    """Return a comparable cookie signature without retaining it in status files."""

    try:
        cookies = await context.cookies()
    except Exception:
        return ()
    return tuple(
        sorted(
            (
                str(item.get("domain") or ""),
                str(item.get("name") or ""),
                str(item.get("value") or ""),
            )
            for item in cookies
            if str(item.get("domain") or "").endswith("douyin.com")
            and str(item.get("name") or "")
        )
    )


async def _visible_login_button(page) -> bool:
    """Check the actual visible login button, not hidden modal markup."""

    try:
        locator = page.get_by_role("button", name="登录", exact=True)
        count = await locator.count()
    except Exception:
        return False
    for index in range(count):
        try:
            if await locator.nth(index).is_visible():
                return True
        except Exception:
            continue
    return False


async def _visible_text_count(page, text: str) -> int:
    """Count only visible text nodes.

    Douyin keeps the login dialog markup in the DOM after a successful scan,
    but hides it.  ``locator.count()`` therefore remains non-zero forever and
    the old loop never transitioned from waiting to success.
    """

    try:
        locator = page.get_by_text(text, exact=False)
        count = await locator.count()
    except Exception:
        return 0
    visible = 0
    for index in range(count):
        try:
            if await locator.nth(index).is_visible():
                visible += 1
        except Exception:
            continue
    return visible


async def run_login():
    cfg = load_config()["spider"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    QR_PATH.unlink(missing_ok=True)
    write_status("starting", "正在打开 Chrome 抖音登录窗口...")

    playwright = None
    context = None
    try:
        playwright = await async_playwright().start()
        launch_options = {
            "user_data_dir": str(SESSION_DIR),
            "headless": False,
            "args": ["--new-window"],
            "viewport": {
                "width": cfg["viewport_width"],
                "height": cfg["viewport_height"],
            },
            "user_agent": cfg["user_agent"],
            "locale": cfg["locale"],
        }
        launch_options.update(browser_launch_options(cfg))
        write_status("starting", "正在启动抖音登录浏览器…")
        context = await playwright.chromium.launch_persistent_context(**launch_options)

        login_page = await context.new_page()
        write_status("starting", "浏览器已启动，正在打开抖音登录页…")
        try:
            await login_page.goto(
                "https://www.douyin.com/",
                # 抖音首页会持续加载大量资源；等待 DOMContentLoaded 会让
                # 二维码迟迟不进入轮询。commit 到达即可开始识别登录层。
                wait_until="commit",
                timeout=15000,
            )
        except Exception:
            # 抖音首页资源较多，只要窗口已打开，就继续等待用户操作。
            pass

        await login_page.wait_for_timeout(4000)
        login_button = login_page.get_by_role("button", name="登录", exact=True)
        if await login_button.count():
            try:
                await login_button.first.click(force=True, timeout=5000)
            except Exception:
                pass

        # ``updated_at`` 代表二维码生成/进入等待扫码的时间，而不是进程
        # 心跳。此前在轮询中每秒写一次 waiting，会让网页误以为旧二维码
        # 永远新鲜，实际扫码时却已被抖音判定失效。
        write_status("starting", "请在弹出的 Chrome 抖音窗口中准备二维码")
        baseline_auth, baseline_cookie_state = await _auth_cookie_state(context)
        baseline_all_cookie_state = await _all_cookie_state(context)
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        login_surface_seen = False
        qr_status_written = False
        scanned_status_written = False
        qr_missing_rounds = 0
        login_button_hidden_rounds = 0

        while time.monotonic() < deadline:
            try:
                # 扫码成功后抖音可能直接关闭登录页；只要此前已经看到
                # 登录界面/二维码，这应视为登录完成，而不是“窗口关闭失败”。
                auth_present, cookie_state = await _auth_cookie_state(context)
                # 同一账号重新扫码时，抖音可能复用已有强登录 Cookie，值并不
                # 变化；此时仅比较 Cookie 会一直停在 waiting。只要已经看见
                # 登录界面、检测到扫码状态，且可见“登录”按钮消失，即可确认
                # 手机端二次确认已完成。
                login_button_hidden_after_scan = False
                if login_surface_seen and scanned_status_written:
                    login_button_hidden_after_scan = not await _visible_login_button(login_page)
                    login_button_hidden_rounds = (
                        login_button_hidden_rounds + 1
                        if login_button_hidden_after_scan
                        else 0
                    )
                else:
                    login_button_hidden_rounds = 0
                login_confirmed_by_page = login_button_hidden_rounds >= 2
                if login_confirmed_by_page or (
                    auth_present
                    and (
                        not baseline_auth
                        or cookie_state != baseline_cookie_state
                    )
                ):
                    QR_PATH.unlink(missing_ok=True)
                    write_status("success", "抖音登录已确认，会话已保存")
                    return
                all_cookie_state = await _all_cookie_state(context)
                if login_surface_seen and all_cookie_state != baseline_all_cookie_state:
                    # 某些版本不使用 sessionid 命名，但扫码后会更新会话 Cookie；
                    # 只有登录按钮已隐藏时才接受该变化，避免把匿名 Cookie 轮换误判为成功。
                    if not await _visible_login_button(login_page):
                        QR_PATH.unlink(missing_ok=True)
                        write_status("success", "抖音登录已确认，会话已保存")
                        return
                if login_page.is_closed():
                    if login_surface_seen and auth_present:
                        QR_PATH.unlink(missing_ok=True)
                        write_status("success", "抖音登录已确认，会话已保存")
                    else:
                        write_status("error", "Chrome 登录窗口已关闭，尚未确认扫码登录")
                    return
                # 成功/已扫码提示有时出现在新开的页面或 iframe 外层，
                # 因此遍历当前持久化上下文里的所有页面，而不是只盯首个 tab。
                pages = [
                    page
                    for page in context.pages
                    if not page.is_closed()
                ]
                if login_page not in pages:
                    pages.insert(0, login_page)
                modal_count = 0
                success_marker_count = 0
                scanned_marker_count = 0
                for page in pages:
                    modal_count += await _visible_text_count(
                        page, "登录后免费畅享高清视频"
                    )
                    for marker in ("登录成功", "扫码成功", "已登录", "登录完成"):
                        success_marker_count += await _visible_text_count(page, marker)
                    for marker in ("已扫码", "请在手机上确认", "确认登录"):
                        scanned_marker_count += await _visible_text_count(page, marker)
                qr_saved = await save_login_qr(login_page)
                qr_missing_rounds = 0 if qr_saved else qr_missing_rounds + 1
                if success_marker_count > 0:
                    QR_PATH.unlink(missing_ok=True)
                    write_status("success", "抖音登录已确认，会话已保存")
                    return
                if modal_count > 0 or qr_saved:
                    login_surface_seen = True
                    if qr_saved and not qr_status_written:
                        write_status("waiting", "请使用抖音扫码，二维码已准备好")
                        qr_status_written = True
                    if scanned_marker_count > 0 and not scanned_status_written:
                        write_status("waiting", "已检测到扫码，请在手机上确认登录")
                        scanned_status_written = True
                    elif qr_missing_rounds >= 2 and not scanned_status_written:
                        write_status("waiting", "二维码状态已变化，请在手机上确认登录")
                        scanned_status_written = True
                # 登录弹窗隐藏本身不代表扫码成功（用户也可能手动关闭弹窗）；
                # 只有强登录 Cookie 或明确成功标记才切换 success。
            except Exception as exc:
                QR_PATH.unlink(missing_ok=True)
                # 保留简短异常类型，便于网页/日志区分“用户关闭窗口”和
                # Playwright/浏览器启动故障；不写入任何 Cookie 或页面正文。
                write_status(
                    "error",
                    f"Chrome 登录窗口已关闭（{type(exc).__name__}: {exc}），请重新点击网页登录",
                )
                return
            await asyncio.sleep(1)

        QR_PATH.unlink(missing_ok=True)
        write_status("timeout", "登录等待超时，请重新点击网页登录")
    except Exception:
        QR_PATH.unlink(missing_ok=True)
        write_status("error", "Chrome 登录窗口启动失败，请关闭已有抖音窗口后重试")
        raise
    finally:
        if QR_PATH.exists() and _status_is_terminal():
            QR_PATH.unlink(missing_ok=True)
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass


def _status_is_terminal() -> bool:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8")).get("status") in {
            "success", "error", "timeout"
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


if __name__ == "__main__":
    asyncio.run(run_login())
