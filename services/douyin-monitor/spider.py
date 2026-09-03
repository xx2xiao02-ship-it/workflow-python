"""抖音博主视频采集模块 - 滚动触发 + API 拦截 (可复用)"""
import logging
import asyncio
import random
import time
import os
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page

from browser_runtime import browser_launch_options

SESSION_DIR = Path(__file__).parent / "douyin_session"

logger = logging.getLogger(__name__)


class DouyinSessionBusyError(RuntimeError):
    """The shared logged-in browser profile is currently in use."""


@contextmanager
def _exclusive_session_lock(timeout_seconds: float = 20.0):
    """Serialize persistent-context users across worker processes.

    Chromium refuses a second persistent context for the same user-data-dir and
    reports the opaque ``TargetClosedError``.  A tiny OS file lock turns that
    race into a deterministic, resumable task state instead of corrupting the
    login profile.
    """
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SESSION_DIR / ".collector.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()
    handle.seek(0)
    acquired = False
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    try:
        while time.monotonic() < deadline:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - retained for local Linux tooling
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, BlockingIOError):
                time.sleep(0.25)
        if not acquired:
            raise DouyinSessionBusyError("抖音登录浏览器正在被其他采集任务使用，请稍后从断点继续")
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


@dataclass
class Profile:
    nickname: str
    avatar_url: str
    follower_count: int
    following_count: int
    total_likes: int
    bio: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Video:
    video_id: str
    title: str
    cover_url: str
    video_url: str
    duration_ms: int
    create_time: int
    like_count: int
    comment_count: int
    share_count: int
    view_count: int
    hashtags: list[str]
    fetched_at: str

    @property
    def public_url(self) -> str:
        """抖音公开播放链接 https://www.douyin.com/video/{video_id}"""
        if self.video_id:
            return f"https://www.douyin.com/video/{self.video_id}"
        return ""

    def to_dict(self) -> dict:
        return asdict(self)


class DouyinSpider:
    API_PATTERN = "/aweme/v1/web/aweme/post/"
    PROFILE_PATTERNS = [
        "/user/profile/other/",
        "/user/info/",
        "/aweme/v1/web/user/profile/",
        "/web/api/v2/user/info/",
        "/aweme/v1/web/im/user/info/",
    ]

    def __init__(self, headless: bool | None = None, max_scrolls: int | None = None,
                 page_load_wait: int | None = None, idle_limit: int | None = None):
        from config_manager import load_config
        cfg = load_config()
        s = cfg["spider"]
        self.headless = headless if headless is not None else s["headless"]
        self.max_scrolls = max_scrolls if max_scrolls is not None else s["max_scrolls"]
        self.page_load_wait = page_load_wait if page_load_wait is not None else s["page_load_wait"]
        self.idle_limit = idle_limit if idle_limit is not None else s["scroll_idle_limit"]
        self.videos: list[Video] = []
        self.profile: Profile | None = None
        self._seen_ids: set[str] = set()
        self._scroll_count = 0
        self._last_hit_scroll = 0
        self._stopped = False
        self._error: str | None = None
        self._browser_executable_path: str | None = None
        # Pagination diagnostics used by the account evidence collector.  The
        # web page is still the source of truth, but persisting the last cursor
        # lets a resumed run prove whether the homepage actually advanced.
        self._next_cursor: str = ""
        self._has_more: bool | None = None
        self._api_pages: int = 0
        self._last_api_url: str = ""

    @staticmethod
    def _parse_aweme(aweme: dict) -> Video:
        stats = aweme.get("statistics", {})
        vi = aweme.get("video", {})
        cover_list = vi.get("cover", {}).get("url_list", [])
        play_addr = vi.get("play_addr", {})
        hashtags = [e.get("hashtag_name", "") for e in aweme.get("text_extra", []) if e.get("hashtag_name")]
        return Video(
            video_id=str(aweme.get("aweme_id", "")),
            title=aweme.get("desc", ""),
            cover_url=cover_list[-1] if cover_list else "",
            video_url=play_addr.get("url_list", [""])[0] if play_addr else "",
            duration_ms=vi.get("duration", 0),
            create_time=aweme.get("create_time", 0),
            like_count=stats.get("digg_count", 0),
            comment_count=stats.get("comment_count", 0),
            share_count=stats.get("share_count", 0),
            view_count=stats.get("play_count", 0),
            hashtags=hashtags,
            fetched_at=datetime.now().isoformat(),
        )

    async def _on_response(self, response):
        if self.API_PATTERN not in response.url:
            return
        self._last_api_url = response.url
        try:
            data = await response.json()
        except Exception:
            return

        aweme_list = data.get("aweme_list", [])
        self._api_pages += 1
        self._has_more = bool(data.get("has_more", False))
        # Douyin has used both max_cursor and cursor across web API versions.
        # Keep the value as text so it can safely cross the JSON checkpoint.
        for key in ("max_cursor", "cursor", "min_cursor"):
            value = data.get(key)
            if value not in (None, ""):
                self._next_cursor = str(value)
                break
        new = 0
        for aweme in aweme_list:
            # Profile fallback: extract from first video's author
            if self.profile is None:
                author = aweme.get("author", {})
                if author:
                    avatar_list = (
                        author.get("avatar_medium", {}).get("url_list")
                        or author.get("avatar_thumb", {}).get("url_list")
                        or []
                    )
                    self.profile = Profile(
                        nickname=author.get("nickname", ""),
                        avatar_url=avatar_list[0] if avatar_list else "",
                        follower_count=author.get("follower_count", 0),
                        following_count=author.get("following_count", 0),
                        total_likes=author.get("total_favorited", 0),
                        bio=author.get("signature", ""),
                    )
                    logger.info("  [主页(fallback)] %s  粉丝:%s",
                                self.profile.nickname,
                                f"{self.profile.follower_count:,}")

            vid = str(aweme.get("aweme_id", ""))
            if vid and vid not in self._seen_ids:
                self._seen_ids.add(vid)
                self.videos.append(self._parse_aweme(aweme))
                new += 1

        gap = self._scroll_count - self._last_hit_scroll
        self._last_hit_scroll = self._scroll_count
        has_more = bool(data.get("has_more", False))
        logger.info("  [API] +%d/%d 条, 累计 %d, gap=%d %s",
                    new, len(aweme_list), len(self.videos), gap,
                    "(last page)" if not has_more else "")
        if not has_more and len(aweme_list) > 0:
            self._stopped = True

    async def _on_profile_response(self, response):
        if self.profile is not None:
            return  # already captured
        url = response.url
        if not any(p in url for p in self.PROFILE_PATTERNS):
            return
        try:
            data = await response.json()
        except Exception:
            return
        user = data.get("user", {})
        if not user:
            return
        avatar_list = user.get("avatar_medium", {}).get("url_list") or user.get("avatar_thumb", {}).get("url_list") or []
        self.profile = Profile(
            nickname=user.get("nickname", ""),
            avatar_url=avatar_list[0] if avatar_list else "",
            follower_count=user.get("follower_count", 0),
            following_count=user.get("following_count", 0),
            total_likes=user.get("total_favorited", 0),
            bio=user.get("signature", ""),
        )
        logger.info("  [主页] %s  粉丝:%s",
                    self.profile.nickname,
                    f"{self.profile.follower_count:,}")

    async def _scroll_naturally(self, page: Page):
        """Scroll the actual profile list container, not only ``window``.

        Douyin's current desktop page keeps the document at a fixed 1080px
        viewport and puts the作品列表 in a nested ``route-scroll-container``.
        Scrolling ``window`` therefore never changes ``max_cursor`` and the
        collector repeatedly sees the first page (the old implementation's
        21-item ceiling).  Select the largest scrollable element and advance
        it in small human-like steps; the final ``scrollTop`` assignment is a
        deterministic fallback for virtualized lists.
        """
        vp = page.viewport_size or {"width": 1920, "height": 1080}
        scroll_info = await page.evaluate(
            """() => {
                const candidates = [...document.querySelectorAll('*')]
                    .filter(el => el.scrollHeight > el.clientHeight + 24)
                    .map(el => ({el, delta: el.scrollHeight - el.clientHeight}))
                    .sort((a, b) => b.delta - a.delta);
                const item = candidates[0];
                if (!item) return {found: false};
                const el = item.el;
                const rect = el.getBoundingClientRect();
                const step = Math.max(450, Math.min(950, Math.round(el.clientHeight * 0.8)));
                el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + step);
                el.dispatchEvent(new Event('scroll', {bubbles: true}));
                return {
                    found: true,
                    tag: el.tagName,
                    className: String(el.className || ''),
                    scrollTop: el.scrollTop,
                    scrollHeight: el.scrollHeight,
                    clientHeight: el.clientHeight,
                    x: Math.max(1, Math.round(rect.left + rect.width / 2)),
                    y: Math.max(1, Math.round(rect.top + Math.min(rect.height / 2, 700))),
                };
            }"""
        )
        if isinstance(scroll_info, dict) and scroll_info.get("found"):
            await page.mouse.move(float(scroll_info.get("x") or vp["width"] // 2), float(scroll_info.get("y") or vp["height"] - 200))
            for _ in range(random.randint(1, 2)):
                await page.mouse.wheel(0, random.randint(450, 900))
                await page.wait_for_timeout(random.randint(250, 500))
            await page.evaluate(
                """() => {
                    const candidates = [...document.querySelectorAll('*')]
                        .filter(el => el.scrollHeight > el.clientHeight + 24)
                        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                    const el = candidates[0];
                    if (el) { el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll', {bubbles: true})); }
                }"""
            )
        else:
            # Keep compatibility with simple pages/tests that only expose the
            # document scrolling surface.
            await page.mouse.move(vp["width"] // 2, vp["height"] - 200)
            await page.mouse.wheel(0, random.randint(500, 900))
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(random.randint(700, 1300))

    async def fetch(self, sec_uid: str, max_retries: int = 3) -> list[Video]:
        """获取博主视频列表，支持自动重试（指数退避 + 抖动）。"""
        # Hold the profile lock for the complete fetch/retry lifecycle.  This
        # prevents two independent style workers from opening the same Edge
        # user-data-dir at the same time (which otherwise yields TargetClosed).
        with _exclusive_session_lock():
            for attempt in range(max_retries):
                try:
                    return await self._fetch_attempt(sec_uid)
                except DouyinSessionBusyError:
                    raise
                except Exception as e:
                    delay = (2 ** attempt) + random.uniform(0.5, 2.0)
                    logger.warning("fetch 第 %d/%d 次失败: %s, %.1fs 后重试",
                                   attempt + 1, max_retries, e, delay)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(delay)
                    else:
                        self._error = f"fetch 失败（已重试 {max_retries} 次）: {e}"
                        logger.error(self._error)
                        return []

    async def _fetch_attempt(self, sec_uid: str) -> list[Video]:
        self.videos = []
        self.profile = None
        self._seen_ids = set()
        self._scroll_count = 0
        self._last_hit_scroll = 0
        self._stopped = False
        self._error = None
        self._next_cursor = ""
        self._has_more = None
        self._api_pages = 0
        self._last_api_url = ""

        async with async_playwright() as p:
            from config_manager import load_config
            cfg = load_config()
            s = cfg["spider"]
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            launch_options = {
                "user_data_dir": str(SESSION_DIR),
                "headless": self.headless,
                "viewport": {"width": s["viewport_width"], "height": s["viewport_height"]},
                "user_agent": s["user_agent"],
                "locale": s["locale"],
            }
            launch_options.update(browser_launch_options(cfg))
            self._browser_executable_path = launch_options.get("executable_path")
            if self._browser_executable_path:
                logger.info("  [浏览器] 使用本机浏览器: %s", self._browser_executable_path)
            else:
                logger.warning("  [浏览器] 未找到本机 Edge/Chrome，将尝试 Playwright 自带浏览器")
            context = await p.chromium.launch_persistent_context(
                **launch_options,
            )
            page = await context.new_page()
            page.on("response", self._on_response)
            page.on("response", self._on_profile_response)

            url = f"https://www.douyin.com/user/{sec_uid}"
            logger.info("  [加载] %s", url)
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(self.page_load_wait)

            if not self.videos:
                self._error = "未收到首页数据(可能未登录或触发风控)"
                await context.close()
                return []

            for i in range(self.max_scrolls):
                if self._stopped:
                    break
                self._scroll_count += 1
                await self._scroll_naturally(page)
                idle = self._scroll_count - self._last_hit_scroll
                if idle >= self.idle_limit:
                    break

            await context.close()

        return self.videos
