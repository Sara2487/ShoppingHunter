"""One Playwright persistent context, owned by exactly one process, with rate limiting.

* Dedicated profile directory + lock file (pid). A second owner fails fast.
* Global and per-domain semaphores, jittered inter-request delay per domain.
* Domain allowlist enforced on every navigation.
* Robot checks are detected and surfaced (``needs_human``); never solved here.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import random
import time
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from ..config import Settings, StartupError
from ..logging import log_event
from .selectors import ALLOWED_HOSTS, BASE


class RobotCheck(Exception):
    """Amazon served a captcha / robot-check page."""


class NeedsHuman(Exception):
    """A human did not clear the robot check within the timeout."""


class NotAllowed(Exception):
    """Navigation target outside the Amazon allowlist."""


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


class ProfileLock:
    def __init__(self, profile_dir: Path):
        self.path = profile_dir / ".shopping_hunter.lock"

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip() or 0)
            except ValueError:
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                raise StartupError(f"browser profile is in use by pid {pid} (lock {self.path}). "
                                   "Stop the other Shopping Hunter process (web app or login script) first.")
        self.path.write_text(str(os.getpid()))

    def release(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()


NeedsHumanCallback = Callable[[str, str, str | None], Awaitable[None]]  # (marketplace, reason, run_id)

ANCHOR_HTML = """<!doctype html><meta charset="utf-8"><title>Shopping Hunter</title>
<style>
 html{color-scheme:light dark}
 body{font:16px/1.6 system-ui,Segoe UI,sans-serif;margin:0;display:grid;place-items:center;height:100vh;
      background:#0f1115;color:#e7eaf0;text-align:center}
 .box{max-width:560px;padding:32px}
 h1{font-size:22px;margin:0 0 12px} p{margin:0 0 10px;color:#9aa4b2}
 b{color:#fbbf24}
</style>
<div class="box">
  <h1>🛒 Shopping Hunter is using this window</h1>
  <p><b>Please leave it open</b> while you use the app. Closing it stops the current hunt.</p>
  <p>Sign in to Amazon in this window so the hunter can see prices and shipping for your address.
     Tabs will open and close here on their own while a hunt runs.</p>
</div>"""


class BrowserSession:
    def __init__(self, settings: Settings, on_needs_human: NeedsHumanCallback | None = None):
        self.settings = settings
        self.on_needs_human = on_needs_human
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.channel: str | None = None  # which browser actually launched
        self._lock = ProfileLock(settings.profile_dir)
        self._global_sem = asyncio.Semaphore(settings.global_concurrency)
        self._domain_sem: dict[str, asyncio.Semaphore] = {}
        self._last_request: dict[str, float] = {}
        self._delay_lock: dict[str, asyncio.Lock] = {}
        self._restart_lock = asyncio.Lock()
        self._anchor: Page | None = None
        self._closed = True  # context-level: True until a launch succeeds
        self.relaunches = 0
        self.started = False

    # -- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        """Acquire the profile lock and launch the browser."""
        if self.started:
            return
        self._lock.acquire()
        try:
            await self._launch()
            self.started = True
        except BaseException:
            # Never leave the profile lock behind when startup fails.
            with contextlib.suppress(Exception):
                if self._pw:
                    await self._pw.stop()
            self._pw = None
            self._lock.release()
            raise

    async def _launch(self) -> None:
        """Launch the persistent context, picking a browser that this machine actually allows.

        Playwright's bundled Chromium lives under AppData and some managed machines refuse to
        execute it ("spawn UNKNOWN" / Permission denied). An installed Chrome or Edge is both
        permitted and a better fit for Amazon, so channels are tried before the bundled build.
        """
        if self._pw is None:
            self._pw = await async_playwright().start()
        kwargs = dict(
            user_data_dir=str(self.settings.profile_dir.resolve()),
            headless=self.settings.headless,
            locale="en-US",
            viewport={"width": 1280, "height": 900},
            accept_downloads=False,
        )
        candidates: list[str | None] = (
            [self.settings.browser_channel] if self.settings.browser_channel
            else ["chrome", "msedge", None]
        )
        errors: list[str] = []
        for channel in candidates:
            kw = dict(kwargs)
            if channel:
                kw["channel"] = channel
            try:
                self.context = await self._pw.chromium.launch_persistent_context(**kw)
            except Exception as e:  # noqa: BLE001 - try the next browser
                errors.append(f"{channel or 'bundled chromium'}: {str(e).splitlines()[0][:160]}")
                log_event("browser_channel_unavailable", channel=channel or "bundled", error=str(e)[:200])
                continue
            self.channel = channel
            break
        else:
            raise StartupError(
                "No usable browser. Tried: " + "; ".join(errors) +
                ". Install Google Chrome, or set HUNTER_BROWSER_CHANNEL, or run "
                "'python -m playwright install chromium'."
            )
        self.context.set_default_timeout(self.settings.page_timeout_s * 1000)
        self._closed = False
        self.context.on("close", self._on_context_close)
        await self._make_anchor()
        log_event("browser_started", headless=self.settings.headless, channel=self.channel or "bundled")

    def _on_context_close(self, _ctx: object) -> None:
        """Chrome exited (usually because someone closed its window). Recover on next use."""
        if not self._closed:
            self._closed = True
            log_event("browser_context_closed", hint="will relaunch on next use")

    async def _make_anchor(self) -> None:
        """Keep one visible tab that explains itself, so the window is less likely to be closed."""
        assert self.context
        try:
            self._anchor = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self._anchor.set_content(ANCHOR_HTML)
        except Exception as e:  # noqa: BLE001 - cosmetic only
            log_event("anchor_page_failed", error=str(e)[:120])
            self._anchor = None

    async def ensure_alive(self) -> BrowserContext:
        """Return a live context, relaunching if the browser was closed underneath us."""
        if not self.started:
            raise StartupError("browser session not started")
        if self.context is not None and not self._closed:
            return self.context
        async with self._restart_lock:
            if self.context is not None and not self._closed:
                return self.context
            self.relaunches += 1
            log_event("browser_relaunching", attempt=self.relaunches)
            with contextlib.suppress(Exception):
                if self.context:
                    await self.context.close()
            self.context = None
            await self._launch()
            log_event("browser_relaunched", attempt=self.relaunches)
            return self.context

    async def close(self) -> None:
        try:
            if self.context:
                await self.context.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self.context = None
            self._anchor = None
            self._pw = None
            self._closed = True
            self._lock.release()
            self.started = False
            log_event("browser_closed")

    # -- rate limiting ---------------------------------------------------------
    def _sem(self, domain: str) -> asyncio.Semaphore:
        if domain not in self._domain_sem:
            self._domain_sem[domain] = asyncio.Semaphore(self.settings.per_domain_concurrency)
            self._delay_lock[domain] = asyncio.Lock()
        return self._domain_sem[domain]

    async def _throttle(self, domain: str) -> None:
        self._sem(domain)
        async with self._delay_lock[domain]:
            last = self._last_request.get(domain, 0.0)
            gap = random.uniform(self.settings.min_domain_delay_s, self.settings.max_domain_delay_s)
            wait = last + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[domain] = time.monotonic()

    @contextlib.asynccontextmanager
    async def page(self, domain: str) -> AsyncIterator[Page]:
        async with self._global_sem, self._sem(domain):
            ctx = await self.ensure_alive()
            try:
                pg = await ctx.new_page()
            except Exception:  # noqa: BLE001 - browser died between the check and the call
                self._closed = True
                ctx = await self.ensure_alive()
                pg = await ctx.new_page()
            try:
                yield pg
            finally:
                with contextlib.suppress(Exception):
                    await pg.close()

    # -- navigation ------------------------------------------------------------
    @staticmethod
    def check_allowed(url: str) -> None:
        host = urlparse(url).hostname or ""
        if host not in ALLOWED_HOSTS:
            raise NotAllowed(f"navigation to {host!r} is not allowed")

    async def goto(self, page: Page, url: str, *, run_id: str | None = None) -> None:
        self.check_allowed(url)
        domain = urlparse(url).hostname or ""
        await self._throttle(domain)
        log_event("nav", run_id=run_id, host=domain, path=urlparse(url).path[:80])
        resp = await page.goto(url, wait_until="domcontentloaded")
        if resp is not None and resp.status in (503, 429):
            raise RobotCheck(f"http {resp.status}")
        if await self.is_robot_check(page):
            raise RobotCheck("captcha page")

    async def is_robot_check(self, page: Page) -> bool:
        for sel in BASE["robot_form"]:
            if await page.locator(sel).count():
                return True
        title = (await page.title() or "").lower()
        if "robot check" in title or "captcha" in title:
            return True
        try:
            txt = await page.locator("body").inner_text(timeout=2000)
        except Exception:  # noqa: BLE001
            return False
        low = txt[:3000].lower()
        return ("enter the characters you see below" in low
                or "we just need to make sure you're not a robot" in low
                or "to discuss automated access to amazon data" in low)

    async def wait_for_human(self, page: Page, marketplace: str, *, run_id: str | None, reason: str = "captcha") -> None:
        """Pause until the robot check clears (a person solves it in the headed window) or time out."""
        if self.settings.headless:
            raise NeedsHuman("robot check in headless mode")
        log_event("needs_human", run_id=run_id, marketplace=marketplace, reason=reason)
        if self.on_needs_human:
            await self.on_needs_human(marketplace, reason, run_id)
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        deadline = time.monotonic() + self.settings.needs_human_timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            if not await self.is_robot_check(page):
                log_event("needs_human_cleared", run_id=run_id, marketplace=marketplace)
                return
        raise NeedsHuman(f"robot check not cleared within {self.settings.needs_human_timeout_s}s")

    async def open_for_user(self, url: str) -> Page:
        """Open a page the app does not manage (login). Caller must not close the context."""
        self.check_allowed(url)
        ctx = await self.ensure_alive()
        try:
            pg = await ctx.new_page()
        except Exception:  # noqa: BLE001 - browser died between the check and the call
            self._closed = True
            ctx = await self.ensure_alive()
            pg = await ctx.new_page()
        with contextlib.suppress(Exception):
            await pg.goto(url, wait_until="domcontentloaded")
        with contextlib.suppress(Exception):
            await pg.bring_to_front()
        return pg
