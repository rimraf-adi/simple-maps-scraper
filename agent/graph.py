"""
LangGraph agent: Perceive → Plan → Act loop.

The LLM sees a numbered list of interactive DOM elements and picks one by index.
Emails are auto-extracted from every page snapshot before asking the LLM.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import partial
from typing import TypedDict, Literal

from langgraph.graph import StateGraph, END

from browser.manager import BrowserManager
from llm.client import LLMClient, Action

log = logging.getLogger("maps_scraper.agent")

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


class AgentState(TypedDict):
    goal: str
    max_steps: int
    step: int
    extracted_data: dict
    action_history: list[str]
    done: bool
    dead_clicks: list[str]   # elements that were clicked but page didn't change
    prev_fingerprint: str    # "(url)|(title)|(count)" from last snapshot


def _route_after_plan(state: AgentState) -> Literal["perceive", "__end__"]:
    return "__end__" if state["done"] else "perceive"


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
    log.info("  \033[36m[step %d/%d]\033[0m", state["step"], state["max_steps"])

    snap = await browser.snapshot()
    history = state.get("action_history", [])[:50]
    dead_clicks = state.get("dead_clicks", [])
    prev_fingerprint = state.get("prev_fingerprint", "")

    # ── Filter scheduling/calendar links ────────────────────────────────────
    elements = []
    skipped = 0
    for el in snap.interactive_elements:
        href = el.get("href", "").lower()
        text = el.get("text", "").lower()
        if any(kw in href or kw in text for kw in ("calendly", "schedule", "booking", "book a", "free consultation")):
            skipped += 1
        else:
            elements.append(el)
    if skipped:
        log.info("  \033[33m\u23f0 calender schedule, doesnt work (%d skipped)\033[0m", skipped)

    # ── Compute page fingerprint ────────────────────────────────────────────
    fingerprint = f"{snap.url}|{snap.title}|{len(elements)}"

    # Detect dead clicks: last action was a click but page didn't change
    if history and history[-1].startswith("click(") and fingerprint == prev_fingerprint:
        # Extract the element index from the history entry
        m = re.search(r"click\((\d+)\)", history[-1])
        if m and m.group(1) not in dead_clicks:
            dead_clicks.append(m.group(1))
            log.info("  \033[33m\u2716 Element %s didn't change page — marked dead\033[0m", m.group(1))

    state["dead_clicks"] = dead_clicks
    state["prev_fingerprint"] = fingerprint

    # ── Auto-extract: scan every page for emails without asking LLM ───────
    auto_email = _scan_for_emails(snap.visible_text, elements)
    if auto_email:
        log.info("  \033[92m\u2709 Auto-extracted: %s\033[0m", auto_email)
        state["extracted_data"] = {"email": auto_email}
        state["done"] = True
        history.append(f"auto-extract({auto_email})")
        state["action_history"] = history
        return state

    # Log page summary for debugging
    n_els = len(elements)
    log.info("  \u2192 %s | %d elements", snap.title[:50], n_els)
    for i, el in enumerate(elements[:8]):
        text = (el["text"] or el.get("aria_label", "") or el["tag"])[:45]
        href = el.get("href", "")
        extra = f" href={href[:40]}" if href else ""
        extra += " \033[33m[dead]\033[0m" if str(i) in dead_clicks else ""
        log.info("    [%d] %s%s", i, text, extra)

    # ── Ask LLM what to do next ─────────────────────────────────────────────
    page_info = _format_page(snap, elements, dead_clicks)
    action = await llm.decide(state["goal"], page_info, snap.screenshot_bytes)

    # ── If LLM fails, auto-scroll instead of marking done ─────────────────
    if action is None:
        log.warning("  LLM no action — scrolling down as fallback")
        history.append("auto-scroll(down)")
        state["action_history"] = history
        await browser.scroll("down", amount=800)
        return state

    log.info("  \033[35m%s\033[0m %s \u2192 %s",
             action.action,
             action.target if action.target is not None else action.text,
             action.thought[:80])

    if action.action == "done":
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
    if action.action == "click":
        # Skip dead elements
        if action.target is not None and str(action.target) in dead_clicks:
            log.warning("  Element %s is dead — scrolling instead", action.target)
            executed = await browser.scroll("down", amount=400)
        el = _resolve_target(action.target, elements) if executed is False else None
        if el:
            href = el.get("href", "")
            if href.startswith("mailto:"):
                email = href.replace("mailto:", "").split("?")[0]
                state["extracted_data"] = {"email": email}
                state["done"] = True
                history.append(f"mailto-extract({email})")
                state["action_history"] = history
                log.info("  \033[92m\u2709 Extracted from mailto: %s\033[0m", email)
                return state
            # Avoid clicking the same element twice
            click_key = f"click({action.target})"
            if click_key in clicked_set:
                log.warning("  Already clicked element %s — trying scroll instead", action.target)
                executed = await browser.scroll("down", amount=400)
            else:
                executed = await browser.click_element(el)
        elif action.target is not None and executed is False:
            executed = await browser.click_selector(f"*:nth({action.target})")
    elif action.action == "type":
        if action.target is not None:
            el = _resolve_target(action.target, snap.interactive_elements)
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

    history.append(f"{action.action}({action.target or action.text or ''}) {'\u2713' if executed else '\u2717'}")
    state["action_history"] = history
    if not executed:
        log.warning("  action failed — will scroll next step")

    return state


def _resolve_target(target: int | None, elements: list[dict]) -> dict | None:
    if target is None or not elements:
        return None
    idx = target if target < len(elements) else None
    return elements[idx] if idx is not None else None


def _format_page(snap, elements: list[dict] | None = None, dead_clicks: list[str] | None = None) -> str:
    url = snap.url
    title = snap.title
    text = snap.visible_text[:3000]
    els = elements if elements is not None else snap.interactive_elements
    dc = set(dead_clicks or [])
    lines = [f"URL: {url}", f"Title: {title}", "", "Interactive elements:"]
    for i, el in enumerate(els):
        tag = el["tag"]
        label = (el["text"] or el["href"] or el.get("aria_label", "") or f"<{tag}>")[:80]
        href = el.get("href", "")
        extra = ""
        if href.startswith("mailto:"):
            extra = f" [\u2709 {href}]"
        elif href:
            extra = f" [href={href[:60]}]"
        role = el.get("role", "")
        if role:
            extra += f" role={role}"
        dead = " [DEAD]" if str(i) in dc else ""
        lines.append(f"  [{i}] {label}{extra}{dead}")
    if dc:
        lines.append(f"\nNote: elements {', '.join(sorted(dc))} were clicked but page didn't change — skip them.")
    lines.append("")
    lines.append("Visible text:")
    lines.append(text[:3000] if text else "(empty)")
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

    state: AgentState = {
        "goal": goal,
        "max_steps": max_steps,
        "step": 0,
        "extracted_data": {},
        "action_history": [],
        "done": False,
        "dead_clicks": [],
        "prev_fingerprint": "",
    }

    if start_url:
        await browser.navigate(start_url)

    # ── Pre-scroll: scroll down to trigger lazy-loaded content ──────────
    log.info("  \u2193 Pre-scrolling to load dynamic content...")
    for scroll_idx in range(8):
        await browser.scroll("down", amount=800)
        log.info("    scroll %d/8", scroll_idx + 1)
        await asyncio.sleep(0.8)
    # Scroll back to top so the agent sees the full page
    await browser.page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)
    log.info("  \u2191 Pre-scroll completed")

    for step in range(1, max_steps + 1):
        state["step"] = step
        state = await agent.ainvoke(state)

        if state["done"]:
            log.info("Agent done after %d steps", step)
            break

    return state
