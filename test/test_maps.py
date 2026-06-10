"""
Google Maps scraper — populates a CSV with company names and website URLs.

Usage:
    uv run test_maps.py "Business coaches in New York"
"""

from __future__ import annotations

import asyncio
import csv
import logging
import sys

from browser.manager import BrowserManager
from maps_scraper import extract_leads, fetch_place_details

# ── Quiet logging ──────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("maps_scraper")
log.setLevel(logging.INFO)
log.handlers.clear()
h = logging.StreamHandler()
h.setFormatter(logging.Formatter("\033[36m%(asctime)s\033[0m %(message)s", datefmt="%H:%M:%S"))
log.addHandler(h)


async def main():
    if len(sys.argv) < 2:
        print("Usage: uv run test_maps.py <search query>")
        print("Example: uv run test_maps.py Business coaches in New York")
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    safe = query.replace(" ", "_").lower()
    csv_path = f"{safe}.csv"

    browser = BrowserManager(headless=False)
    await browser.start()

    try:
        # ── Phase 1: Scrape Google Maps for business listings ──────────────
        log.info("Searching Maps for: \033[1m%s\033[0m", query)
        leads = await extract_leads(browser, query)
        log.info("Found \033[1m%d\033[0m leads", len(leads))

        if not leads:
            log.warning("No leads found — check the search query or Maps selectors")
            return

        # ── Phase 2: Open each place page and extract website URL ──────────
        fields = ["Name", "Website"]
        fout = open(csv_path, "w", newline="")
        writer = csv.DictWriter(fout, fieldnames=fields)
        writer.writeheader()
        fout.flush()

        for i, lead in enumerate(leads):
            name = lead.get("Name", "?")
            log.info("  [%d/%d] %s", i + 1, len(leads), name)

            _, website, _ = await fetch_place_details(browser, lead["URL"])

            writer.writerow({
                "Name": name,
                "Website": website or "",
            })
            fout.flush()

            if website:
                log.info("         \033[92m\u2192\033[0m %s", website)
            else:
                log.info("         \033[90m(no website found)\033[0m")

        fout.close()
        log.info("\nDone. \033[1m%d\033[0m leads written to %s", len(leads), csv_path)

    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
