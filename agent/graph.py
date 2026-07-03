"""
LangGraph agent: Perceive → Plan → Act loop.

The LLM sees a numbered list of interactive DOM elements and picks one by index.
Emails are auto-extracted from every page snapshot before asking the LLM.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from functools import partial
from typing import TypedDict, Literal

from langgraph.graph import StateGraph, END

from browser.manager import BrowserManager
from llm.client import LLMClient, Action

log = logging.getLogger("maps_scraper.agent")

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


MAX_FAILURES = 5  # consecutive failures before giving up on a site
SITE_TIMEOUT_S = 60  # max seconds to spend in the agent loop per site

class AgentState(TypedDict):
    goal: str
    max_steps: int
    step: int
    extracted_data: dict
    action_history: list[str]
    done: bool
    dead_clicks: list[str]   # elements that were clicked but page didn't change
    prev_fingerprint: str    # "(url)|(title)|(count)" from last snapshot
    consecutive_failures: int
    start_time: float        # time.time() when agent started on this site


def _route_after_plan(state: AgentState) -> Literal["perceive", "__end__"]:
    # Force end if done OR if we reached the maximum allowed steps to prevent infinite looping
    if state["done"] or state["step"] >= state["max_steps"]:
        return "__end__"
    return "perceive"


def _scan_for_emails(text: str, elements: list[dict]) -> str | None:
    """Scan visible text and interactive elements for any email address."""
    # Check visible text
    for m in EMAIL_RE.finditer(text):
        email = m.group(0)
        if not email.endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js")):
            return email

    # Check mailto hrefs in elements
    for el in elements:
        href = el.get("href", "")
        if href.startswith("mailto:"):
            email = href.replace("mailto:", "").split("?")[0]
            if email and "@" in email:
                return email

    return None


async def _perceive(state: AgentState, browser: BrowserManager, llm: LLMClient):
    """No-op perceive — the snapshot is taken inside _plan."""
    return state


async def _plan(state: AgentState, browser: BrowserManager, llm: LLMClient):
    dash = getattr(llm, "dashboard", None)
    step_label = f"Step {state['step']}/{state['max_steps']}"
    log.info("  %s", step_label)

    # ── Time-based bailout ────────────────────────────────────────────────
    elapsed = time.time() - state.get("start_time", time.time())
    if elapsed > SITE_TIMEOUT_S:
        log.warning("  Site timeout (%.0fs > %ds) — giving up", elapsed, SITE_TIMEOUT_S)
        if dash:
            dash.log(f"[AGENT] Timeout after {int(elapsed)}s — giving up on this site", "SKIP")
        state["done"] = True
        state["action_history"] = state.get("action_history", []) + ["timeout"]
        return state

    snap = await browser.snapshot(dismiss=True)
    history = state.get("action_history", [])[:50]
    dead_clicks = state.get("dead_clicks", [])
    prev_fingerprint = state.get("prev_fingerprint", "")

    # ── Filter ads, popups, cookie banners ────────────────────────────────
    # IMPORTANT: Use exact text matches or word-boundary checks, NOT substring.
    # The old substring filter (e.g. "ad") killed elements containing "read",
    # "header", "address", "loading", etc. — causing "0 elements" on every page.
    EXACT_TEXT_BLOCK = {
        "accept", "accept all", "allow", "allow all", "allow cookies",
        "got it", "i agree", "dismiss", "no thanks", "not now",
        "close", "subscribe", "sign up",
    }
    HREF_BLOCK = (
        "calendly.com", "acuityscheduling.com", "booksy.com",
        "#cookie", "cookie-policy",
    )
    elements = []
    skipped = 0
    for el in snap.interactive_elements:
        href = el.get("href", "").lower()
        text = el.get("text", "").lower().strip()
        aria = el.get("aria_label", "").lower()
        # Skip tiny elements (ads/badges) unless they have useful links
        w = el.get("w", 0) or 0
        h = el.get("h", 0) or 0
        is_tiny = (w * h) < 800  # e.g. 28x28 or smaller
        is_useful_link = href and any(k in href for k in ("contact", "about", "email", "team", "mailto"))
        if is_tiny and not is_useful_link:
            skipped += 1
            continue
        # Block by exact button/link text
        if text in EXACT_TEXT_BLOCK:
            skipped += 1
            continue
        # Block by href domain/path
        if any(domain in href for domain in HREF_BLOCK):
            skipped += 1
            continue
        elements.append(el)
    if skipped:
        log.info("  \u23f0 filtered %d popup/cookie elements", skipped)

    # ── Compute page fingerprint ────────────────────────────────────────────
    fingerprint = f"{snap.url}|{snap.title}|{len(elements)}"
    # For SPAs, track URL separately to detect client-side route changes
    prev_url = prev_fingerprint.split("|")[0] if prev_fingerprint else ""

    # Detect dead clicks: last action was a click but page didn't change
    if history and history[-1].startswith("click("):
        url_changed = snap.url != prev_url
        page_changed = fingerprint != prev_fingerprint
        if not url_changed and not page_changed:
            m = re.search(r"click\((\d+)\)", history[-1])
            if m and m.group(1) not in dead_clicks:
                dead_clicks.append(m.group(1))
                if dash:
                    dash.log(f"\u2716 Element {m.group(1)} didn't change page — marked dead", "RETRY")

    state["dead_clicks"] = dead_clicks
    state["prev_fingerprint"] = fingerprint

    # ── Remove dead elements entirely (LLM ignores [DEAD] tags) ──────────
    dead_set = set(dead_clicks)
    live_elements = [el for i, el in enumerate(elements) if str(i) not in dead_set]

    # ── Auto-extract: scan every page for emails without asking LLM ───────
    auto_email = _scan_for_emails(snap.visible_text, live_elements)
    if auto_email:
        log.info("  \u2709 Auto-extracted: %s", auto_email)
        if dash:
            dash.log(f"\u2709 Auto-extracted email: {auto_email}", "SUCCESS")
        state["extracted_data"] = {"email": auto_email}
        state["done"] = True
        history.append(f"auto-extract({auto_email})")
        state["action_history"] = history
        return state

    # If all elements are dead, bail out early
    if not live_elements and elements:
        log.warning("  All %d elements are dead — giving up", len(elements))
        if dash:
            dash.log(f"[AGENT] All {len(elements)} elements are dead — giving up", "SKIP")
        state["done"] = True
        state["action_history"] = history + ["all-dead"]
        return state

    # Log page summary
    n_els = len(live_elements)
    page_title = snap.title[:50]
    log.info("  \u2192 %s | %d elements (%d dead removed)", page_title, n_els, len(elements) - n_els)
    if dash:
        dash.log(f"[AGENT] {page_title} — {n_els} live elements", "INFO")

    # ── Ask LLM what to do next ─────────────────────────────────────────────
    # Send only live elements (dead ones are removed, not tagged)
    page_info = _format_page(snap, live_elements)
    action = await llm.decide(state["goal"], page_info, snap.screenshot_bytes)

    # ── Track consecutive failures ───────────────────────────────────────
    failures = state.get("consecutive_failures", 0)

    # ── If LLM fails, auto-scroll ──────────────────────────────────────────
    if action is None:
        failures += 1
        if failures >= MAX_FAILURES:
            log.warning("  %d consecutive failures — giving up", failures)
            if dash:
                dash.log(f"[AGENT] {failures}x no action — giving up on this site", "SKIP")
            state["done"] = True
            state["action_history"] = history + ["gave-up"]
            return state
        log.warning("  LLM no action — scrolling down")
        if dash:
            dash.log(f"[AGENT] LLM returned nothing — scrolling ({failures}/{MAX_FAILURES})", "RETRY")
        history.append("auto-scroll(down)")
        state["action_history"] = history
        state["consecutive_failures"] = failures
        await browser.scroll("down", amount=800)
        return state

    action_desc = f"{action.action} {action.target or action.text or ''}"
    log.info("  %s \u2192 %s", action_desc, action.thought[:80])
    if dash:
        dash.log(f"[AGENT] {action_desc} — {action.thought[:60]}", "INFO")

    if action.action == "done":
        if dash:
            dash.log("[AGENT] LLM decided done — no email found", "SKIP")
        state["done"] = True
        state["action_history"] = history + ["done"]
        return state

    if action.action == "extract":
        state["extracted_data"] = action.data or {}
        state["done"] = True
        state["action_history"] = history + [f"extract: {action.data}"]
        return state

    # Track clicked elements to avoid repeats
    clicked_set = set(entry for entry in history if entry.startswith("click("))

    executed = False
    is_recoverable_failure = False
    if action.action == "click":
        # Resolve against live_elements (dead ones already removed)
        el = _resolve_target(action.target, live_elements)
        if el:
            href = el.get("href", "")
            if href.startswith("mailto:"):
                email = href.replace("mailto:", "").split("?")[0]
                state["extracted_data"] = {"email": email}
                state["done"] = True
                history.append(f"mailto-extract({email})")
                state["action_history"] = history
                log.info("  \u2709 Extracted from mailto: %s", email)
                if dash:
                    dash.log(f"\u2709 Found email from mailto: {email}", "SUCCESS")
                return state
            click_key = f"click({action.target})"
            if click_key in clicked_set:
                log.warning("  Already clicked element %s — trying scroll instead", action.target)
                if dash:
                    dash.log(f"[AGENT] Already clicked [{action.target}] — scrolling", "RETRY")
                executed = await browser.scroll("down", amount=400)
                is_recoverable_failure = True
            else:
                executed = await browser.click_element(el)
        elif action.target is not None:
            executed = await browser.click_selector(f"*:nth({action.target})")
    elif action.action == "type":
        if action.target is not None:
            el = _resolve_target(action.target, live_elements)
            if el and action.text:
                cx, cy = int(el["center_x"]), int(el["center_y"])
                await browser.click_coords(cx, cy)
                await browser.page.wait_for_timeout(500)
                await browser.page.keyboard.type(action.text, delay=20)
                executed = True
        elif action.text:
            try:
                await browser.page.keyboard.type(action.text, delay=20)
                executed = True
            except Exception:
                pass
    elif action.action == "scroll":
        executed = await browser.scroll(action.text or "down", amount=800)

    # ── Check for progress failures ──────────────────────────────────────────
    if not executed or is_recoverable_failure:
        failures += 1
        if failures >= MAX_FAILURES:
            log.warning("  %d consecutive failures — giving up", failures)
            if dash:
                dash.log(f"[AGENT] {failures}x failed actions — giving up on this site", "SKIP")
            state["done"] = True
            state["action_history"] = history + [f"{action.action}({action.target or action.text or ''}) gave-up"]
            return state
        log.warning("  action failed (%d/%d)", failures, MAX_FAILURES)
        if dash:
            dash.log(f"[AGENT] Action failed ({failures}/{MAX_FAILURES}) — will retry", "RETRY")
    else:
        failures = 0

    state["consecutive_failures"] = failures
    status = "\u2713" if executed else "\u2717"
    history.append(f"{action.action}({action.target or action.text or ''}) {status}")
    state["action_history"] = history
    if not executed:
        log.warning("  action failed — will scroll next step")

    state["step"] += 1
    return state


def _resolve_target(target: int | None, elements: list[dict]) -> dict | None:
    if target is None or not elements:
        return None
    idx = target if target < len(elements) else None
    return elements[idx] if idx is not None else None


def _format_page(snap, elements: list[dict] | None = None) -> str:
    """Format page info for the LLM. Only receives live elements (dead ones pre-removed)."""
    url = snap.url
    title = snap.title
    text = snap.visible_text[:1500]  # Reduced from 3000 to save tokens
    els = elements if elements is not None else snap.interactive_elements
    # Only send first 30 elements to LLM
    els = els[:30]
    lines = [f"URL: {url}", f"Title: {title}", "", "Elements:"]
    for i, el in enumerate(els):
        label = (el["text"] or el["href"] or el.get("aria_label", "") or el["tag"])[:60]
        href = el.get("href", "")
        extra = ""
        if href.startswith("mailto:"):
            extra = f" [mailto:{href[7:].split('?')[0]}]"
        elif href and any(kw in href.lower() for kw in ("contact", "about", "email", "team", "connect")):
            extra = f" [href={href[:40]}]"
        lines.append(f"  [{i}] {label}{extra}")
    lines.append("")
    lines.append("Text:")
    lines.append(text if text else "(empty)")
    return "\n".join(lines)


def build_agent(browser: BrowserManager, llm: LLMClient) -> StateGraph:
    builder = StateGraph(AgentState)

    builder.add_node("perceive", partial(_perceive, browser=browser, llm=llm))
    builder.add_node("plan", partial(_plan, browser=browser, llm=llm))

    builder.set_entry_point("perceive")
    builder.add_edge("perceive", "plan")
    builder.add_conditional_edges("plan", _route_after_plan)

    return builder.compile()


async def run_agent(
    browser: BrowserManager,
    llm: LLMClient,
    goal: str,
    start_url: str | None = None,
    max_steps: int = 15,
) -> AgentState:
    agent = build_agent(browser, llm)
    dash = getattr(llm, "dashboard", None)

    state: AgentState = {
        "goal": goal,
        "max_steps": max_steps,
        "step": 0,
        "extracted_data": {},
        "action_history": [],
        "done": False,
        "dead_clicks": [],
        "prev_fingerprint": "",
        "consecutive_failures": 0,
        "start_time": time.time(),
    }

    if start_url:
        await browser.navigate(start_url)

    # ── Dismiss any popups before exploring ──────────────────────────────
    dismissed = await browser.dismiss_popups()
    if dismissed and dash:
        dash.log(f"[AGENT] Dismissed {dismissed} popup(s)", "INFO")

    # ── Pre-scroll: quick scroll to trigger lazy-loaded content ──────────
    log.info("  \u2193 Pre-scrolling...")
    if dash:
        dash.log("[AGENT] Pre-scrolling to load dynamic content...", "INFO")
    for scroll_idx in range(3):
        await browser.scroll("down", amount=800)
        await asyncio.sleep(0.4)
    await browser.page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.3)
    # Dismiss again after scroll (some popups trigger on scroll)
    await browser.dismiss_popups()
    log.info("  \u2191 Pre-scroll done")

    for step_num in range(1, max_steps + 1):
        state["step"] = step_num
        state = await agent.ainvoke(state)

        if state["done"]:
            steps_used = step_num
            if state.get("extracted_data", {}).get("email"):
                log.info("  \u2709 Email found after %d steps", steps_used)
            else:
                log.info("  Done after %d steps — no email", steps_used)
            break

    return state
