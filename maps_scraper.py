"""
Google Maps scraper — Playwright-based, CSS selectors for known structure.
"""

from __future__ import annotations

import asyncio
import logging

from browser.manager import BrowserManager

log = logging.getLogger("maps_scraper")


async def scroll_feed(browser: BrowserManager):
    for scroll_idx in range(50):
        await browser.page.evaluate(
            "document.querySelector('div[role=\"feed\"]')?.scrollBy(0, 1500)"
        )
        await asyncio.sleep(2.0)
        reached_end = await browser.page.evaluate('''() => {
            return !!Array.from(document.querySelectorAll('span, div')).find(
                e => e.textContent?.includes("You've reached the end of the list")
            );
        }''')
        if reached_end:
            log.debug("  ↳ feed end reached")
            break


async def extract_leads(browser: BrowserManager, query: str) -> list[dict]:
    """Search Google Maps and return raw listing data."""
    url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
    log.info("Navigating to Maps search: %s", url)
    await browser.navigate(url)
    await asyncio.sleep(3)

    try:
        await browser.page.wait_for_selector('div[role="feed"]', timeout=15000)
    except Exception:
        log.warning("Feed not found, trying to proceed anyway")

    await scroll_feed(browser)

    leads = []
    articles = await browser.page.query_selector_all('div[role="article"]')
    log.info("Found %d article elements", len(articles))

    for article in articles:
        link = await article.query_selector("a.hfpxzc")
        if not link:
            continue
        href = await link.get_attribute("href") or ""
        if "/maps/place/" not in href:
            continue

        name_el = await article.query_selector(".qBF1Pd")
        name = await name_el.inner_text() if name_el else ""
        rating_el = await article.query_selector(".MW4etd")
        rating = await rating_el.inner_text() if rating_el else ""
        cat_el = await article.query_selector(".W4Efsd span span:first-child")
        category = await cat_el.inner_text() if cat_el else ""

        leads.append({
            "Name": (name or "").strip(),
            "Rating": (rating or "").strip(),
            "Category": (category or "").strip(),
            "URL": href,
        })

    return leads


async def fetch_place_details(
    browser: BrowserManager, url: str
) -> tuple[str, str, str]:
    """Open a Maps place page and extract phone, website, address."""
    try:
        await browser.page.goto(url, timeout=15000, wait_until="domcontentloaded")
    except Exception:
        pass
    await browser.page.wait_for_timeout(2000)

    phone = ""
    website = ""
    address = ""

    try:
        btn = await browser.page.query_selector('button[data-item-id*="phone"]')
        if btn:
            aria = await btn.get_attribute("aria-label") or ""
            phone = aria.replace("Phone: ", "").strip()
    except Exception:
        pass

    try:
        el = await browser.page.query_selector('a[data-item-id*="authority"]')
        if el:
            website = (await el.get_attribute("href") or "").strip()
    except Exception:
        pass

    try:
        btn = await browser.page.query_selector('button[data-item-id="address"]')
        if btn:
            aria = await btn.get_attribute("aria-label") or ""
            address = aria.replace("Address: ", "").strip()
    except Exception:
        pass

    return phone, website, address
