"""
Playwright browser manager — screenshot, click, type, scroll, extract.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from playwright.async_api import async_playwright, Page, BrowserContext

log = logging.getLogger("maps_scraper.browser")


@dataclass
class PageSnapshot:
    url: str
    title: str
    visible_text: str
    screenshot_bytes: bytes
    interactive_elements: list[dict] = field(default_factory=list)


class BrowserManager:
    """Wraps a Playwright browser context for vision-based automation."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-component-update",
                "--disable-sync",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-breakpad",
                "--disable-client-side-phishing-detection",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-ipc-flooding-protection",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-renderer-backgrounding",
                "--enable-features=NetworkService,NetworkServiceInProcess",
                "--force-color-profile=srgb",
                "--hide-scrollbars",
                "--metrics-recording-only",
                "--mute-audio",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            permissions=["geolocation"],
            geolocation={"latitude": 40.7128, "longitude": -74.0060},
            ignore_https_errors=True,
        )
        self._page = await self._context.new_page()

        # ── Block protocol-based links that launch OS apps ──────────────────
        await self._page.route(
            lambda url: any(url.startswith(p) for p in ("mailto:", "tel:", "sms:", "facetime:", "itunes:", "maps:")),
            lambda route: route.abort(),
        )

        # ── Stealth init script — hides automation traces ───────────────────
        await self._page.add_init_script("""
            // Hide webdriver
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // Override chrome.runtime (headless doesn't have it)
            // Ensure navigator.plugins looks real
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });

            // Override permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) => (
                p.name === 'notifications'
                    ? Promise.resolve({ state: 'denied' })
                    : originalQuery(p)
            );

            // Remove headless Chrome flags from User-Agent in WebGL/V8
            // Spoof device memory / hardware concurrency
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        """)

        log.info("Browser started (headless=%s)", self.headless)

    @property
    def page(self) -> Page:
        assert self._page is not None, "call start() first"
        return self._page

    async def navigate(self, url: str, timeout: int = 30000) -> PageSnapshot:
        try:
            await self.page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        except Exception:
            pass

        # Wait for JS rendering: network idle + extra settle time
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await self.page.wait_for_timeout(3000)

        # Detect and wait out Cloudflare challenge pages
        try:
            title = await self.page.title()
            cf = await self.page.query_selector("#challenge-form, .cf-browser-verification, #cf-challenge-running")
            if "just a moment" in title.lower() or cf:
                log.debug("  \u26a1 Cloudflare challenge detected, waiting 10s ...")
                await self.page.wait_for_timeout(10000)
        except Exception:
            pass

        return await self.snapshot()

    async def snapshot(self) -> PageSnapshot:
        # Wait for network to settle — crucial for SPAs (React/Next.js)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(1)
        url = self.page.url
        title = await self.page.title()
        visible_text = await self.page.inner_text("body")
        screenshot_bytes = await self.page.screenshot(full_page=False)

        elements = []
        try:
            # SPA-friendly selector: covers React/Next.js apps with event handlers
            selector = (
                "button, a, input, textarea, select, "
                "[role='button'], [role='link'], [role='tab'], [role='menuitem'], "
                "[role='option'], [role='checkbox'], [role='switch'], "
                "[tabindex], [onclick], [href]"
            )
            handles = await self.page.query_selector_all(selector)
            for el in handles[:150]:
                try:
                    box = await el.bounding_box()
                    if not box or box["width"] <= 5 or box["height"] <= 5:
                        continue
                    tag = await el.evaluate("el => el.tagName.toLowerCase()")
                    text = await el.inner_text()
                    href = await el.get_attribute("href") or ""
                    onclick = await el.get_attribute("onclick") or ""
                    role = await el.get_attribute("role") or ""
                    aria_label = await el.get_attribute("aria-label") or ""

                    elements.append({
                        "tag": tag,
                        "text": (text or "").strip()[:80],
                        "href": href[:200],
                        "onclick": bool(onclick),
                        "role": role[:30],
                        "aria_label": aria_label[:60],
                        "x": box["x"],
                        "y": box["y"],
                        "w": box["width"],
                        "h": box["height"],
                        "center_x": box["x"] + box["width"] / 2,
                        "center_y": box["y"] + box["height"] / 2,
                    })
                except Exception:
                    pass
        except Exception:
            pass

        return PageSnapshot(
            url=url,
            title=title,
            visible_text=(visible_text or "")[:4000],
            screenshot_bytes=screenshot_bytes,
            interactive_elements=elements,
        )

    async def click_selector(self, selector: str) -> bool:
        try:
            el = await self.page.wait_for_selector(selector, timeout=5000)
            if el:
                await el.click()
                await asyncio.sleep(0.5)
                return True
        except Exception:
            pass
        return False

    async def click_coords(self, x: int, y: int) -> bool:
        try:
            await self.page.mouse.click(x, y)
            await asyncio.sleep(0.5)
            return True
        except Exception:
            return False

    async def click_element(self, el: dict) -> bool:
        """Click an element. Waits briefly for navigation, but always returns True if click fired."""
        try:
            x, y = int(el["center_x"]), int(el["center_y"])
            href = el.get("href", "")
            tag = el.get("tag", "")
            if tag == "a" and href and not href.startswith(("mailto:", "tel:", "#", "javascript:", "file:")):
                try:
                    async with self.page.expect_navigation(timeout=3000):
                        await self.page.mouse.click(x, y)
                except Exception:
                    pass
            else:
                await self.page.mouse.click(x, y)
            await asyncio.sleep(0.8)
            return True
        except Exception:
            return False

    async def type_text(self, selector: str, text: str) -> bool:
        try:
            el = await self.page.wait_for_selector(selector, timeout=5000)
            if el:
                await el.fill("")
                await el.type(text, delay=20)
                return True
        except Exception:
            pass
        return False

    async def scroll(self, direction: str = "down", amount: int = 600) -> bool:
        try:
            if direction == "down":
                await self.page.evaluate(f"window.scrollBy(0, {amount})")
            elif direction == "up":
                await self.page.evaluate(f"window.scrollBy(0, -{amount})")
            else:
                await self.page.evaluate(f"window.scrollBy(0, {amount})")
            await asyncio.sleep(0.5)
            return True
        except Exception:
            return False

    async def get_html(self) -> str:
        return await self.page.content()

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log.info("Browser closed")
