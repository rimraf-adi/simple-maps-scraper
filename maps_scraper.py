"""
Google Maps scraper — Playwright-based, CSS selectors for known structure.

Upgraded with human_delay, anti-bot pacing, mouse simulation,
and block detection.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from browser.manager import BrowserManager, human_delay, simulate_mouse_movement

if TYPE_CHECKING:
    from ui.dashboard import Dashboard

log = logging.getLogger("maps_scraper")


def _load_chain_blocklist() -> set[str]:
    """Load chain brand names from config/chain_blocklist.txt."""
    path = Path("config/chain_blocklist.txt")
    if not path.exists():
        return set()
    names = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(line.lower())
    return names


async def scroll_feed(
    browser: BrowserManager,
    dashboard: Dashboard | None = None,
    max_results: int = 0,
) -> None:
    """Scroll the Maps results feed to load all listings."""
    for scroll_idx in range(50):
        await browser.page.evaluate(
            "document.querySelector('div[role=\"feed\"]')?.scrollBy(0, 1500)"
        )
        await human_delay(1.5, 3.5)

        if dashboard:
            dashboard.update_scroll_progress(scroll_idx + 1)

        # Check for block
        block = await browser.check_blocked()
        if block:
            log.warning("Block detected during scroll: %s", block)
            if dashboard:
                dashboard.log(f"Block detected: {block}", "WARNING")
            break

        reached_end = await browser.page.evaluate('''() => {
            return !!Array.from(document.querySelectorAll('span, div')).find(
                e => e.textContent?.includes("You've reached the end of the list")
            );
        }''')
        if reached_end:
            log.debug("  ↳ feed end reached")
            if dashboard:
                dashboard.update_scroll_progress(50)
            break

        # Check if we have enough results already
        if max_results > 0:
            count = await browser.page.evaluate(
                "document.querySelectorAll('div[role=\"article\"]').length"
            )
            if count >= max_results:
                log.info("Reached max-results cap (%d)", max_results)
                break


async def extract_leads(
    browser: BrowserManager,
    query: str,
    dashboard: Dashboard | None = None,
    max_results: int = 0,
    min_rating: float = 0.0,
    category_filter: str = "",
    exclude_chains: bool = False,
) -> list[dict]:
    """Search Google Maps and return raw listing data."""
    url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
    log.info("Navigating to Maps search: %s", url)
    await browser.navigate(url)
    await human_delay(2.0, 4.0)

    # Check for block/CAPTCHA on Maps
    block = await browser.check_blocked()
    if block:
        log.warning("Block detected on Maps: %s", block)
        if dashboard:
            dashboard.log(f"Block on Maps: {block} — trying to continue", "WARNING")

    try:
        await browser.page.wait_for_selector('div[role="feed"]', timeout=15000)
    except Exception:
        log.warning("Feed not found, trying to proceed anyway")

    await scroll_feed(browser, dashboard=dashboard, max_results=max_results)

    # Load chain blocklist if needed
    chains = _load_chain_blocklist() if exclude_chains else set()

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

        name_clean = (name or "").strip()
        rating_clean = (rating or "").strip()
        category_clean = (category or "").strip()

        # Apply min-rating filter
        if min_rating > 0 and rating_clean:
            try:
                if float(rating_clean) < min_rating:
                    if dashboard:
                        dashboard.log(f"Skipped {name_clean} (rating {rating_clean} < {min_rating})", "SKIP")
                    continue
            except ValueError:
                pass

        # Apply category filter
        if category_filter and category_filter.lower() not in category_clean.lower():
            if dashboard:
                dashboard.log(f"Skipped {name_clean} (category mismatch)", "SKIP")
            continue

        # Apply chain blocklist
        if exclude_chains and chains:
            name_lower = name_clean.lower()
            if any(chain in name_lower for chain in chains):
                if dashboard:
                    dashboard.log(f"Skipped chain: {name_clean}", "SKIP")
                continue

        leads.append({
            "Name": name_clean,
            "Rating": rating_clean,
            "Category": category_clean,
            "URL": href,
        })

        # Enforce max-results
        if max_results > 0 and len(leads) >= max_results:
            break

    return leads


async def fetch_place_details(
    browser: BrowserManager, url: str
) -> tuple[str, str, str]:
    """Open a Maps place page and extract phone, website, address."""
    try:
        await browser.page.goto(url, timeout=15000, wait_until="domcontentloaded")
    except Exception:
        pass
    await human_delay(2.0, 4.0)

    # Simulate human interaction
    await simulate_mouse_movement(browser.page)

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
