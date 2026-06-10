"""
Maps Scraper — search Google Maps, extract business emails via LLM agent.

Usage:
    uv run main.py "Business coaches in New York"
    uv run main.py "Plumbers in Chicago" --headless
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import sys

from browser.manager import BrowserManager
from maps_scraper import extract_leads, fetch_place_details
from extract_emails import extract_from_site

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("maps_scraper")
log.setLevel(logging.INFO)
log.handlers.clear()
h = logging.StreamHandler()
h.setFormatter(logging.Formatter("\033[36m%(asctime)s\033[0m %(message)s", datefmt="%H:%M:%S"))
log.addHandler(h)

GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

FIELDS = ["Name", "Phone", "Email", "Website", "Address", "Rating", "Category"]


async def main():
    if len(sys.argv) < 2:
        print("Usage: uv run main.py <search query> [--headless]")
        print("Example: uv run main.py \"Business coaches in New York\"")
        sys.exit(1)

    query = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
    headless = "--headless" in sys.argv
    safe = query.replace(" ", "_").lower()
    csv_path = f"{safe}.csv"

    # Check LLM connectivity
    try:
        from openai import AsyncOpenAI
        from llm.client import LLM_BASE_URL, LLM_API_KEY
        client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        await client.models.list(timeout=10)
        log.info("LLM connected: %s", LLM_BASE_URL)
    except Exception as e:
        log.error("LLM unavailable: %s", e)
        log.error("Set LLM_API_KEY / LLM_BASE_URL in environment")
        sys.exit(1)

    browser = BrowserManager(headless=headless)
    await browser.start()

    try:
        # ── Phase 1: Scrape Google Maps ──────────────────────────────────
        log.info("Searching Maps for: \033[1m%s\033[0m", query)
        leads = await extract_leads(browser, query)
        log.info("Found \033[1m%d\033[0m leads", len(leads))

        if not leads:
            log.warning("No leads found")
            return

        # ── Phase 2: Fetch details + extract emails ──────────────────────
        fout = open(csv_path, "w", newline="")
        writer = csv.DictWriter(fout, fieldnames=FIELDS)
        writer.writeheader()
        fout.flush()

        for i, lead in enumerate(leads):
            name = lead.get("Name", "?")
            log.info("  [%d/%d] %s", i + 1, len(leads), name)

            phone, website, address = await fetch_place_details(browser, lead["URL"])
            lead["Phone"] = phone
            lead["Website"] = website
            lead["Address"] = address

            email = ""
            if website:
                log.info("         %s", website)
                email = await extract_from_site(browser, name, website)
                if email:
                    log.info("         %s\u2709 %s%s", GREEN, email, RESET)

            lead["Email"] = email or ""
            writer.writerow({k: str(lead.get(k, "")) for k in FIELDS})
            fout.flush()

        fout.close()

        with open(csv_path) as f:
            results = list(csv.DictReader(f))
        with_email = sum(1 for r in results if r.get("Email", "").strip())
        log.info("\nDone. %d leads (%d with emails) \u2192 %s",
                 len(results), with_email, csv_path)

    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
