"""
Agentic contact detail extraction — LLM navigates a business website using
the DOM-element-map approach (antigravity-style), picking elements by index.
"""

from __future__ import annotations

import logging

from browser.manager import BrowserManager
from llm.client import LLMClient
from agent.graph import run_agent

log = logging.getLogger("maps_scraper.contact")

# ── Ollama config (embed directly, no env vars) ─────────────────────────────

OLLAMA_URL = "http://localhost:11434/v1"
OLLAMA_MODEL = "qwen3.5:0.8b"


EMAIL_CRAWL_PROMPT = """\
You are extracting an email address from a business website.

## Instructions
1. Look at the current page.
2. If you see a contact/about link, click it.
3. Look for email addresses, contact forms, or "mailto:" links.
4. If you find an email, use the extract action to return it in "data" with key "email".
5. If no email is visible after checking contact page, use extract with empty email.

The email is usually on a /contact or /about page, or in the page footer.
"""


async def crawl_email(browser: BrowserManager, website_url: str) -> str:
    """Use the vision agent to find an email on a business website."""
    log.debug("  crawling %s for email (DOM agent)", website_url)
    llm = LLMClient(base_url=OLLAMA_URL, model=OLLAMA_MODEL)

    result = await run_agent(
        browser=browser,
        llm=llm,
        goal=EMAIL_CRAWL_PROMPT,
        start_url=website_url,
        max_steps=24,
    )
    data = result.get("extracted_data", {})
    if isinstance(data, dict):
        return data.get("email", "")
    return ""
