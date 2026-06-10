"""
Extract emails from sampled business websites using the LangGraph agent.

Reads sample_sites.csv, runs the agent against each site to find
emails through navigation, and writes sample_sites_with_emails.csv.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import sys

from browser.manager import BrowserManager
from llm.client import LLMClient
from agent.graph import run_agent

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


async def extract_from_site(browser: BrowserManager, name: str, url: str) -> str | None:
    """Navigate a business site and extract an email using the agent."""
    try:
        llm = LLMClient()
        result = await run_agent(
            browser=browser,
            llm=llm,
            goal=(
                f"Find the email address for {name}.\n"
                f"Start at {url}.\n"
                "1. Look at the page. Click 'Contact', 'About', 'Get in Touch', or any nav link.\n"
                "2. On the contact page, find the email (mailto: link or visible text).\n"
                "3. Use extract to return it as {\"email\": \"...\"}.\n"
                "4. If no email after exploring, use extract with empty data."
            ),
            start_url=url,
            max_steps=20,
        )

        extracted = result.get("extracted_data", {})
        email = extracted.get("email", "") if isinstance(extracted, dict) else ""
        if email and "@" in email:
            return email
    except Exception as e:
        log.debug("Agent failed for %s: %s", url, e)
    return None


async def main():
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
        from llm.client import LLM_BASE_URL, LLM_API_KEY
        client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        await client.models.list(timeout=10)
        log.info("LLM connected: %s", LLM_BASE_URL)
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
                log.info("  [%d/%d] %s \u2014 no website", i + 1, len(sites), name[:50])
                writer.writerow({"Name": name, "Website": "", "Email": ""})
                fout.flush()
                continue

            log.info("  [%d/%d] %s", i + 1, len(sites), name[:50])
            log.info("         %s", url)

            email = await extract_from_site(browser, name, url)

            if email:
                log.info("         %s\u2709 %s%s", GREEN, email, RESET)
            else:
                log.info("         %s(no email)%s", YELLOW, RESET)

            writer.writerow({"Name": name, "Website": url, "Email": email or ""})
            fout.flush()

        fout.close()

        with open("sample_sites_with_emails.csv") as f:
            results = list(csv.DictReader(f))
        found = sum(1 for r in results if r.get("Email", "").strip())
        log.info("\nDone. %d/%d emails found \u2192 sample_sites_with_emails.csv", found, len(results))

    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
