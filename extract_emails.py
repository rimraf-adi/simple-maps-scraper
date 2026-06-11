"""
Extract emails from business websites using the LangGraph agent.

Upgraded to use KeyPool-backed LLM client and dashboard integration.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import sys
from typing import TYPE_CHECKING

from browser.manager import BrowserManager, human_delay
from llm.client import LLMClient
from agent.graph import run_agent

if TYPE_CHECKING:
    from ui.dashboard import Dashboard
    from llm.key_pool import KeyPool

log = logging.getLogger("maps_scraper")


async def extract_from_site(
    browser: BrowserManager,
    name: str,
    url: str,
    key_pool: "KeyPool | None" = None,
    dashboard: "Dashboard | None" = None,
) -> str | None:
    """Navigate a business site and extract an email using the agent."""
    try:
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

        result = await run_agent(
            browser=browser,
            llm=llm,
            goal=f"Find email for {name}. Click Contact/About links, look for mailto: or email text, then extract it.",
            start_url=url,
            max_steps=8,
        )

        extracted = result.get("extracted_data", {})
        email = extracted.get("email", "") if isinstance(extracted, dict) else ""
        if email and "@" in email:
            if dashboard:
                dashboard.update_lead(email=email, status="FOUND")
            return email
        else:
            if dashboard:
                dashboard.update_lead(status="SKIPPED")
    except Exception as e:
        log.debug("Agent failed for %s: %s", url, e)
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
