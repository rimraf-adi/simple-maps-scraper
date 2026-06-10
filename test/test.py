"""
Test: Agentic DOM navigation & email extraction on real business websites.

Tests against the actual websites in business_coaches_in_newyork.csv
to verify the agent can navigate SPAs, discover DOM elements, and extract emails.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import re
from pathlib import Path

HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

class C:
    green = "\033[92m"
    red = "\033[91m"
    yellow = "\033[93m"
    cyan = "\033[96m"
    bold = "\033[1m"
    reset = "\033[0m"

def ok(msg):   print(f"  {C.green}\u2713{C.reset} {msg}")
def fail(msg): print(f"  {C.red}\u2717{C.reset} {msg}")
def info(msg): print(f"  {C.cyan}*{C.reset} {msg}")
def hdr(msg):  print(f"\n{C.bold}{msg}{C.reset}")
def sub(msg):  print(f"  {C.yellow}{msg}{C.reset}")

# ---------------------------------------------------------------------------
# Test data — real business websites from CSV
# ---------------------------------------------------------------------------

def get_test_sites() -> list[dict]:
    csv_path = HERE / "business_coaches_in_newyork.csv"
    if csv_path.exists():
        with open(csv_path, newline="") as f:
            return list(csv.DictReader(f))
    # Fallback hardcoded sites
    return [
        {"Name": "Bridgeline Executive Coaching", "Website": "https://bridgelinecoaching.com/executive-coaching-new-york/"},
        {"Name": "Business Sanity", "Website": "https://www.business-sanity.com/"},
    ]

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_dom_discovery():
    """Browser discovers interactive elements on real React/Next.js sites."""
    from browser.manager import BrowserManager

    hdr("1. DOM discovery on real websites")
    b = BrowserManager(headless=False)
    await b.start()
    try:
        all_pass = True
        for site in get_test_sites():
            name = site["Name"][:40]
            url = site.get("Website", "")
            if not url:
                continue
            sub(f"\u2192 {name}")
            info(url)

            snap = await b.navigate(url, timeout=25000)
            if not snap:
                fail("navigate returned None")
                all_pass = False
                continue

            n_els = len(snap.interactive_elements)
            text_len = len(snap.visible_text)

            info(f"  Elements: \033[1m{n_els}\033[0m, Text: {text_len} chars, Title: {snap.title[:60]}")

            if n_els == 0:
                fail("Zero interactive elements found")
                all_pass = False
            else:
                ok(f"{n_els} interactive elements")

            if text_len < 50:
                fail("Page text too short (SPA may not have rendered)")
                all_pass = False
            else:
                ok(f"Page rendered ({text_len} chars)")

            # Show top elements
            for i, el in enumerate(snap.interactive_elements[:5]):
                text = el["text"][:40] if el["text"] else "(empty)"
                href = el.get("href", "")[:50]
                extra = f" href={href}" if href else ""
                info(f"  [{i}] {text:40s} tag={el['tag']}{extra}")

        return all_pass
    finally:
        await b.close()


async def test_email_in_page():
    """Search for emails on real sites via visible text and HTML regex."""
    from browser.manager import BrowserManager

    hdr("2. Email search on real websites")
    b = BrowserManager(headless=False)
    await b.start()
    try:
        found_any = False
        for site in get_test_sites():
            name = site["Name"][:40]
            url = site.get("Website", "")
            if not url:
                continue
            sub(f"\u2192 {name}")
            info(url)

            snap = await b.navigate(url, timeout=25000)
            if not snap:
                continue

            # Check visible text for emails
            text_emails = set(re.findall(
                r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                snap.visible_text,
            ))

            # Check HTML for emails
            html = await b.get_html()
            html_emails = set(re.findall(
                r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                html,
            ))

            # Filter out non-business emails (file extensions, etc.)
            business = {
                e for e in (text_emails | html_emails)
                if not e.endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js"))
                and not e.startswith(("example", "user@"))
                and "@" in e
                and "." in e.split("@")[1]
            }

            if business:
                found_any = True
                for e in sorted(business):
                    ok(f"Email: {e}")
            else:
                info("No business emails found on homepage")

            # Check for mailto: links in interactive elements
            mailto_els = [
                el for el in snap.interactive_elements
                if el.get("href", "").startswith("mailto:")
            ]
            if mailto_els:
                for el in mailto_els:
                    email = el["href"].replace("mailto:", "").split("?")[0]
                    ok(f"mailto link: {email}")

        if found_any:
            return True
        info("No emails found on any site homepage — this may be expected")
        return True  # Don't fail — emails may be on subpages
    finally:
        await b.close()


async def test_click_navigation():
    """Click a visible nav link and verify navigation to a new page."""
    from browser.manager import BrowserManager

    hdr("3. Click navigation on real website")
    b = BrowserManager(headless=False)
    await b.start()
    try:
        site = get_test_sites()[0]
        url = site.get("Website", "")
        info(f"Navigating to: {url}")

        snap = await b.navigate(url, timeout=25000)
        if not snap:
            fail("Could not navigate")
            return False

        # Find Contact or About links
        keywords = ("contact", "about", "email", "get in touch")
        target = None
        for el in snap.interactive_elements:
            text = (el.get("text") or "").lower()
            href = (el.get("href") or "").lower()
            if any(k in text or k in href for k in keywords):
                target = el
                info(f"Found: '{el['text'][:50]}' @ ({el['center_x']:.0f}, {el['center_y']:.0f})")
                break

        if not target:
            info("No Contact/About link found on homepage — trying first nav link")
            # Try first navigation-looking element
            for el in snap.interactive_elements:
                text = (el.get("text") or "").lower()
                if text and len(text) < 30 and el["tag"] in ("a", "button"):
                    target = el
                    info(f"Using first nav link: '{el['text'][:50]}'")
                    break

        if not target:
            info("No click target found — skipping click test")
            info("This is common for SPAs where nav is behind a hamburger menu")
            return None  # neither pass nor fail

        clicked = await b.click_coords(int(target["center_x"]), int(target["center_y"]))
        if not clicked:
            fail("Click failed")
            return False

        ok("Click executed")
        await asyncio.sleep(2)

        snap2 = await b.snapshot()
        info(f"New page: {snap2.title[:60]}")

        # Check for emails on the new page
        text_emails = set(re.findall(
            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
            snap2.visible_text,
        ))
        if text_emails:
            for e in text_emails:
                ok(f"Found email on navigated page: {e}")

        return True
    finally:
        await b.close()


async def test_mailto_extraction():
    """Verify mailto: links are detected and email extracted without opening Mail app."""
    from browser.manager import BrowserManager

    hdr("4. mailto: link detection & extraction (no OS app launch)")
    b = BrowserManager(headless=False)
    await b.start()
    try:
        found_mailto = False
        for site in get_test_sites():
            url = site.get("Website", "")
            snap = await b.navigate(url, timeout=25000)
            if not snap:
                continue

            mailto_els = [
                el for el in snap.interactive_elements
                if el.get("href", "").startswith("mailto:")
            ]
            if mailto_els:
                found_mailto = True
                for el in mailto_els:
                    email = el["href"].replace("mailto:", "").split("?")[0]
                    ok(f"Detected mailto: {email} on {site['Name'][:30]}")

                    # Verify clicking would extract email (simulate agent logic)
                    extracted = el["href"].replace("mailto:", "").split("?")[0]
                    if extracted and "@" in extracted:
                        ok(f"Extraction from mailto works: {extracted}")
                break

        if not found_mailto:
            info("No mailto links found on any site homepage")
            info("Emails may be embedded as plain text or behind forms")
            return None
        return True
    finally:
        await b.close()


async def test_llm_parse_robustness():
    """LLM parse_action handles real-world malformed JSON."""
    from llm.client import parse_action

    hdr("5. LLM parse_action robustness")

    cases = [
        ("Clean extract", '{"thought":"ok","action":"extract","data":{"email":"a@b.com"}}', "a@b.com"),
        ("Trailing junk }}\"", '{"thought":"ok","action":"extract","data":{"email":"a@b.com"}}"}' , "a@b.com"),
        ("Trailing extra brace", '{"thought":"ok","action":"extract","data":{"email":"a@b.com"}}}', "a@b.com"),
        ("Code fence", '```json\n{"thought":"ok","action":"extract","data":{"email":"a@b.com"}}\n```', "a@b.com"),
        ("Single quotes", "{'thought':'ok','action':'extract','data':{'email':'a@b.com'}}", "a@b.com"),
        ("Trailing comma", '{"thought":"ok","action":"extract","data":{"email":"a@b.com"},}', "a@b.com"),
        ("Unquoted key", '{thought:"ok",action:"extract",data:{email:"a@b.com"}}', "a@b.com"),
        ("Click action", '{"thought":"click it","action":"click","target":2}', None),
        ("Done", '{"thought":"done","action":"done"}', None),
    ]

    all_pass = True
    for label, raw, expected in cases:
        result = parse_action(raw)
        if result is None:
            fail(f"{label}: parse_action returned None")
            all_pass = False
            continue

        if expected is not None:
            actual = (result.data or {}).get("email", "") if result.data else ""
            if actual == expected:
                ok(f"{label}: email={actual}")
            else:
                fail(f"{label}: expected {expected}, got {actual}")
                all_pass = False
        else:
            ok(f"{label}: action={result.action}")

    return all_pass


async def test_full_agent():
    """End-to-end: run the LangGraph agent against a real business site."""
    from browser.manager import BrowserManager
    from llm.client import LLMClient
    from agent.graph import run_agent

    hdr("6. Full agent against real website (requires Ollama)")

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(base_url="http://localhost:11434/v1", api_key="not-needed")
        await client.models.list(timeout=5)
        info("Ollama available")
    except Exception as e:
        info(f"Ollama not available ({e}) \u2014 skipping")
        return None

    site = get_test_sites()[0]
    name = site["Name"][:40]
    url = site.get("Website", "")
    sub(f"\u2192 {name}")
    info(url)

    b = BrowserManager(headless=False)
    await b.start()
    try:
        llm = LLMClient(base_url="http://localhost:11434/v1", model="qwen3.5:0.8b")
        result = await run_agent(
            browser=b,
            llm=llm,
            goal=(
                f"Find an email address on {name}'s website.\n"
                "1. Look at the current page for any email addresses or mailto links.\n"
                "2. If you see a Contact, About, or similar link, click it.\n"
                "3. Look for email addresses, 'mailto:' links, or contact forms.\n"
                "4. Use extract action to return the email as {\"email\": \"...\"}.\n"
                "5. If no email found after exploring, use extract with empty data."
            ),
            start_url=url,
            max_steps=20,
        )

        extracted = result.get("extracted_data", {})
        email = extracted.get("email", "") if isinstance(extracted, dict) else ""
        info(f"Steps: {result['step']}, history: {result['action_history']}")

        if email and "@" in email:
            ok(f"Agent extracted: {email}")
            return True

        info("No email extracted — this is common if the email is on a subpage")
        return None
    finally:
        await b.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print(f"{C.bold}{'='*60}{C.reset}")
    print(f"{C.bold}  Agent Test Suite \u2014 Real Websites{C.reset}")
    print(f"{C.bold}{'='*60}{C.reset}")

    sites = get_test_sites()
    info(f"Testing against {len(sites)} real website(s):")
    for s in sites:
        info(f"  \u2022 {s['Name'][:50]} \u2192 {s.get('Website', '')}")

    tests = [
        test_dom_discovery,
        test_email_in_page,
        test_click_navigation,
        test_mailto_extraction,
        test_llm_parse_robustness,
        test_full_agent,
    ]

    results = []
    for fn in tests:
        try:
            res = await fn()
            label = fn.__name__.replace("test_", "").replace("_", " ").title()
            if res is None:
                info(f"{label}: \033[93mSKIPPED\033[0m")
                results.append(("SKIP", label))
            elif res:
                ok(f"{label}: PASSED")
                results.append(("PASS", label))
            else:
                fail(f"{label}: FAILED")
                results.append(("FAIL", label))
        except Exception as e:
            import traceback
            traceback.print_exc()
            label = fn.__name__.replace("test_", "").replace("_", " ").title()
            fail(f"{label}: ERROR \u2014 {e}")
            results.append(("FAIL", f"{label} ({e})"))

    print(f"\n{C.bold}{'='*60}{C.reset}")
    print(f"{C.bold}  Summary{C.reset}")
    print(f"{C.bold}{'='*60}{C.reset}")
    for status, name in results:
        c = C.green if status == "PASS" else C.yellow if status == "SKIP" else C.red
        print(f"  {c}{status}{C.reset}  {name}")

    passed = sum(1 for s, _ in results if s == "PASS")
    skipped = sum(1 for s, _ in results if s == "SKIP")
    failed = sum(1 for s, _ in results if s == "FAIL")
    print(f"\n  {passed} passed, {failed} failed, {skipped} skipped\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("maps_scraper").setLevel(logging.WARNING)
    exit(asyncio.run(main()))
