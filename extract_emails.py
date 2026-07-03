"""
Extract emails from business websites using a hybrid approach.

Tier 0 — Regex scan (FREE, instant)
Tier 1 — HTML route discovery + regex (FREE, fast)
Tier 2 — LLM one-shot extraction (cheap, moderate)
Tier 3 — Agent loop fallback (expensive, slow — LAST RESORT)

Upgraded to use KeyPool-backed LLM client and dashboard integration.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import re
import sys
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from browser.manager import BrowserManager, human_delay
from llm.client import LLMClient
from agent.graph import run_agent

if TYPE_CHECKING:
    from ui.dashboard import Dashboard
    from llm.key_pool import KeyPool

log = logging.getLogger("maps_scraper")

# ── Regex for email detection ─────────────────────────────────────────────
EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
REJECT_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.css', '.js', '.ico', '.webp', '.woff', '.woff2')
REJECT_PREFIXES = ('noreply@', 'no-reply@', 'no_reply@', 'donotreply@', 'do-not-reply@',
                   'mailer-daemon@', 'postmaster@')
REJECT_DOMAINS = ('example.com', 'example.org', 'test.com', 'sentry.io',
                  'wixpress.com', 'googleapis.com', 'google.com', 'w3.org',
                  'schema.org', 'facebook.com', 'twitter.com', 'instagram.com')

# Routes to try for contact page discovery
CONTACT_KEYWORDS = ('contact', 'about', 'team', 'connect', 'get-in-touch',
                    'reach-us', 'info', 'support')

# Total timeout for processing a single site (prevents 30-minute hangs)
SITE_TIMEOUT_S = 90


def _validate_email(email: str) -> bool:
    """Quick validation: reject assets, noreply, known-bad domains."""
    email = email.lower().rstrip('.')
    if not EMAIL_RE.fullmatch(email):
        return False
    if any(email.endswith(ext) for ext in REJECT_EXTS):
        return False
    if any(email.startswith(p) for p in REJECT_PREFIXES):
        return False
    domain = email.split('@', 1)[1] if '@' in email else ''
    if domain in REJECT_DOMAINS:
        return False
    return True


def _best_email(emails: list[str]) -> str | None:
    """Pick the best email from a list of candidates.
    Prefers info@, contact@, hello@ over generic ones."""
    valid = [e for e in emails if _validate_email(e)]
    if not valid:
        return None
    # Prefer business-y prefixes
    for prefix in ('info@', 'contact@', 'hello@', 'office@', 'admin@', 'inquir'):
        for e in valid:
            if e.lower().startswith(prefix):
                return e
    return valid[0]


async def _regex_scan(browser: BrowserManager) -> list[str]:
    """Tier 0: Scan rendered HTML for email patterns. Free and instant."""
    return await browser.scan_emails_from_html()


async def _discover_contact_routes(browser: BrowserManager, base_url: str) -> list[str]:
    """Tier 1: Parse all <a> tags from the page to find contact/about links.
    Returns a list of URLs to visit, discovered from actual page navigation."""
    links = await browser.get_all_links()
    base_domain = urlparse(base_url).netloc.lower()
    found: list[str] = []
    seen: set[str] = set()

    for link in links:
        href = link.get("href", "").strip()
        text = link.get("text", "").lower().strip()
        if not href:
            continue

        # Only follow same-domain links
        parsed = urlparse(href)
        link_domain = parsed.netloc.lower()
        if link_domain and link_domain != base_domain and not link_domain.endswith('.' + base_domain):
            continue

        # Check if the link looks like a contact/about page
        href_lower = href.lower()
        path_lower = parsed.path.lower()
        is_contact_route = (
            any(kw in path_lower for kw in CONTACT_KEYWORDS)
            or any(kw in text for kw in CONTACT_KEYWORDS)
            or 'mailto:' in href_lower
        )
        if not is_contact_route:
            continue

        # Skip mailto: links (we'll extract the email directly)
        if href.startswith('mailto:'):
            email = href.replace('mailto:', '').split('?')[0]
            if _validate_email(email):
                found.insert(0, f'mailto:{email}')  # Highest priority
            continue

        # Normalise and dedupe
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        found.append(full_url)

    return found[:8]  # Cap at 8 routes to avoid rabbit holes


async def _try_extract_llm(
    browser: BrowserManager,
    llm: LLMClient,
    name: str,
    url: str,
    dashboard: "Dashboard | None" = None,
    label: str = "",
) -> str | None:
    """Tier 2: Send rendered page content to LLM for direct email extraction."""
    try:
        page_text = await browser.page.inner_text("body")
    except Exception:
        page_text = ""

    try:
        html = await browser.page.content()
    except Exception:
        html = ""

    email = await llm.extract_email(name, url, page_text or "", html or "")
    if email:
        if dashboard:
            dashboard.log(f"[{label}] ✉ LLM extracted: {email}", "SUCCESS")
        log.info("  ✉ LLM extract [%s]: %s", label, email)
        return email
    return None


async def _extract_with_timeout(
    browser: BrowserManager,
    name: str,
    url: str,
    key_pool: "KeyPool | None" = None,
    dashboard: "Dashboard | None" = None,
) -> str | None:
    """Core extraction logic with the 4-tier hybrid pipeline."""
    if key_pool:
        llm = LLMClient(
            base_url=key_pool.current_base_url(),
            model=key_pool.current_model(),
            key_pool=key_pool,
            dashboard=dashboard,
        )
    else:
        llm = LLMClient(dashboard=dashboard)

    if dashboard:
        dashboard.update_lead(status="NAVIGATING", website=url)

    # Navigate to the business website
    await browser.navigate_with_timeout(url, total_timeout=30)

    # ── Tier 0: Regex scan on home page (FREE, instant) ──────────────────
    emails = await _regex_scan(browser)
    best = _best_email(emails)
    if best:
        if dashboard:
            dashboard.log(f"[T0:regex] ✉ Found: {best}", "SUCCESS")
            dashboard.update_lead(email=best, status="FOUND")
        log.info("  ✉ Tier 0 regex found: %s", best)
        return best

    # ── Tier 1: Discover contact routes from HTML + regex scan ────────────
    routes = await _discover_contact_routes(browser, url)
    if dashboard and routes:
        route_names = [r.split('/')[-1][:30] for r in routes[:5]]
        dashboard.log(f"[T1:routes] Discovered {len(routes)} routes: {', '.join(route_names)}", "INFO")

    for route in routes:
        # Handle mailto: links found in navigation
        if route.startswith('mailto:'):
            email = route.replace('mailto:', '').split('?')[0]
            if _validate_email(email):
                if dashboard:
                    dashboard.log(f"[T1:mailto] ✉ Found: {email}", "SUCCESS")
                    dashboard.update_lead(email=email, status="FOUND")
                return email
            continue

        try:
            await browser.navigate_with_timeout(route, total_timeout=20)
        except Exception:
            continue

        emails = await _regex_scan(browser)
        best = _best_email(emails)
        if best:
            if dashboard:
                dashboard.log(f"[T1:route] ✉ Found on {route.split('/')[-1][:30]}: {best}", "SUCCESS")
                dashboard.update_lead(email=best, status="FOUND")
            log.info("  ✉ Tier 1 route found: %s on %s", best, route[:60])
            return best

    # ── Tier 2: LLM one-shot extraction (try home + best contact page) ───
    # Navigate back to home page for LLM
    await browser.navigate_with_timeout(url, total_timeout=20)
    email = await _try_extract_llm(browser, llm, name, url, dashboard, "home")
    if email:
        if dashboard:
            dashboard.update_lead(email=email, status="FOUND")
        return email

    # Try the first discovered contact route with LLM
    for route in routes[:2]:
        if route.startswith('mailto:'):
            continue
        try:
            await browser.navigate_with_timeout(route, total_timeout=20)
        except Exception:
            continue
        email = await _try_extract_llm(browser, llm, name, route, dashboard, route.split('/')[-1][:20])
        if email:
            if dashboard:
                dashboard.update_lead(email=email, status="FOUND")
            return email

    # ── Tier 3: Agent loop (LAST RESORT, reduced steps) ──────────────────
    if dashboard:
        dashboard.log("[T3:agent] Last resort: starting agent loop", "INFO")

    # Navigate back to home for agent
    await browser.navigate_with_timeout(url, total_timeout=20)

    result = await run_agent(
        browser=browser,
        llm=llm,
        goal=f"Find email for {name}. Click Contact/About links, look for mailto: or email text, then extract it.",
        start_url=None,  # already on the site
        max_steps=3,  # Reduced from 5 — this is last resort only
    )

    extracted = result.get("extracted_data", {})
    email = extracted.get("email", "") if isinstance(extracted, dict) else ""
    if email and "@" in email and _validate_email(email):
        if dashboard:
            dashboard.update_lead(email=email, status="FOUND")
        return email
    else:
        if dashboard:
            dashboard.update_lead(status="SKIPPED")

    return None


async def extract_from_site(
    browser: BrowserManager,
    name: str,
    url: str,
    key_pool: "KeyPool | None" = None,
    dashboard: "Dashboard | None" = None,
) -> str | None:
    """Navigate a business site and extract an email.

    Wraps the hybrid pipeline in a total timeout to prevent one site
    from stalling the entire scraping run.
    """
    try:
        return await asyncio.wait_for(
            _extract_with_timeout(browser, name, url, key_pool, dashboard),
            timeout=SITE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.warning("  ⏰ Total site timeout (%ds) for %s", SITE_TIMEOUT_S, url[:60])
        if dashboard:
            dashboard.log(f"⏰ Site timeout ({SITE_TIMEOUT_S}s) — moving on", "WARNING")
            dashboard.update_lead(status="TIMEOUT")
        return None
    except Exception as e:
        log.debug("Extraction failed for %s: %s", url, e)
        if dashboard:
            dashboard.update_lead(status="FAILED")
        return None


async def main() -> None:
    """Standalone email extraction from sample_sites.csv (legacy mode)."""
    import os.path
    csv_path = "sample_sites.csv"
    if not os.path.exists(csv_path):
        csv_path = os.path.join("test", "sample_sites.csv")
    try:
        with open(csv_path, newline="") as f:
            sites = list(csv.DictReader(f))
    except FileNotFoundError:
        log.error("sample_sites.csv not found (looked in . and test/)")
        sys.exit(1)

    log.info("Loaded %d sites from sample_sites.csv", len(sites))

    # Check LLM connectivity
    try:
        from openai import AsyncOpenAI
        from llm.key_pool import KeyPool
        pool = KeyPool.from_env()
        client = AsyncOpenAI(base_url=pool.base_url, api_key=pool.current_key())
        await client.models.list(timeout=10)
        log.info("LLM connected: %s", pool.base_url)
    except Exception as e:
        log.error("LLM unavailable: %s", e)
        sys.exit(1)

    browser = BrowserManager(headless=False)
    await browser.start()

    fields = ["Name", "Website", "Email"]
    try:
        fout = open("sample_sites_with_emails.csv", "w", newline="")
        writer = csv.DictWriter(fout, fieldnames=fields)
        writer.writeheader()
        fout.flush()

        for i, site in enumerate(sites):
            name = site.get("Name", "?")
            url = site.get("Website", "").strip()
            if not url:
                log.info("  [%d/%d] %s — no website", i + 1, len(sites), name[:50])
                writer.writerow({"Name": name, "Website": "", "Email": ""})
                fout.flush()
                continue

            log.info("  [%d/%d] %s", i + 1, len(sites), name[:50])
            log.info("         %s", url)

            email = await extract_from_site(browser, name, url, key_pool=pool)

            if email:
                log.info("         ✉ %s", email)
            else:
                log.info("         (no email)")

            writer.writerow({"Name": name, "Website": url, "Email": email or ""})
            fout.flush()

        fout.close()

        with open("sample_sites_with_emails.csv") as f:
            results = list(csv.DictReader(f))
        found = sum(1 for r in results if r.get("Email", "").strip())
        log.info("\nDone. %d/%d emails found → sample_sites_with_emails.csv", found, len(results))

    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
