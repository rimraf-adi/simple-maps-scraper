"""
OpenAI-compatible LLM client for hybrid DOM + vision agent.

Upgraded to use KeyPool for multi-key rotation with automatic
error-based rotation and exponential backoff.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm.key_pool import KeyPool

log = logging.getLogger("maps_scraper.llm")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")


@dataclass
class Action:
    thought: str
    action: str            # click | type | scroll | extract | done
    target: int | None = None
    text: str = ""
    data: dict | None = None


SYSTEM_PROMPT = """You are a web automation agent extracting an email from a business website.

You see a screenshot of the page AND a numbered list of interactive elements.

## Strategy
1. Look at the screenshot: find "Contact", "About", "Email", "Get in Touch" links.
2. Match what you see in the screenshot to the numbered element list.
3. Click the matching nav link to reach a page with contact info.
4. On the contact page, look for email text or mailto: links.
5. Use **extract** when you find an email.

## Available actions
- `click` target=N — Click element [N]
- `type` target=N text="..." — Type into element [N]
- `scroll` text="down" or "up" — Scroll
- `extract` data={"email": "found@email.com"} — Return found email
- `done` — Goal unreachable

Output ONLY valid JSON:
{"thought": "...", "action": "click|type|scroll|extract|done", "target": 0, "text": "", "data": {}}
"""


def _build_prompt(goal: str, page_info: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"## Goal\n{goal}\n\n"
                f"## Page\n{page_info}\n\n"
                "Pick the next action."
            ),
        },
    ]


def _build_vision_prompt(goal: str, page_info: str, screenshot_bytes: bytes) -> list[dict]:
    """Build a prompt with a screenshot image for vision-capable models."""
    b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
    data_url = f"data:image/png;base64,{b64}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"## Goal\n{goal}\n\n"
                        f"## Page elements\n{page_info}\n\n"
                        "Look at the screenshot AND the element list. Pick the next action."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                },
            ],
        },
    ]


_JSON_FIXES = [
    (re.compile(r"^(.*\})\s*[^}]*$", re.DOTALL), r"\1"),
    (re.compile(r"'(.*?)'"), r'"\1"'),
    (re.compile(r",\s*([}\]])"), r"\1"),
    (re.compile(r"(\{|\,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:"), r'\1"\2":'),
    (re.compile(r"&quot;"), r'"'),
    (re.compile(r"\bNone\b"), "null"),
    (re.compile(r"\bTrue\b"), "true"),
    (re.compile(r"\bFalse\b"), "false"),
]


def _extract_json(text: str) -> str | None:
    """Extract the first valid JSON object from text, handling common LLM quirks."""
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        candidate = m.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end == -1:
        return None

    candidate = text[start:end]

    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        pass

    for pattern, replacement in _JSON_FIXES:
        try:
            fixed = pattern.sub(replacement, candidate)
            json.loads(fixed)
            return fixed
        except json.JSONDecodeError:
            continue

    for i in range(len(candidate), start, -1):
        try:
            json.loads(candidate[:i])
            return candidate[:i]
        except json.JSONDecodeError:
            continue
    return None


def parse_action(raw: str) -> Action | None:
    raw = raw.strip()
    if not raw:
        return None

    body = _extract_json(raw)
    if body is None:
        log.warning("LLM output has no parseable JSON:\n%s", raw[:300])
        return None

    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        log.warning("LLM output is not valid JSON:\n%s", raw[:300])
        return None

    action = obj.get("action", "")
    if action not in ("click", "type", "scroll", "extract", "done"):
        log.warning("Unknown action '%s'", action)
        return None

    return Action(
        thought=obj.get("thought", ""),
        action=action,
        target=obj.get("target"),
        text=obj.get("text", ""),
        data=obj.get("data"),
    )


VISION_MODELS = {"llava", "bakllava", "minicpm-v", "moondream", "qwen2.5-vl", "llama3.2-vision"}


class LLMClient:
    def __init__(
        self,
        base_url: str = LLM_BASE_URL,
        model: str = LLM_MODEL,
        key_pool: "KeyPool | None" = None,
        dashboard: "Any | None" = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model or "llama-3.3-70b-versatile"
        self._vision_works = None  # None = untested, True/False = cached
        self._key_pool = key_pool
        self.dashboard = dashboard

    def _get_api_key(self) -> str:
        """Get the current API key from pool or legacy env var."""
        if self._key_pool:
            return self._key_pool.current_key()
        return LLM_API_KEY
        
    def _get_base_url(self) -> str:
        if self._key_pool:
            return self._key_pool.current_base_url().rstrip("/")
        return self.base_url
        
    def _get_model(self) -> str:
        if self._key_pool:
            return self._key_pool.current_model()
        return self.model

    async def _handle_error(self, error: Exception, context: str = "") -> bool:
        """
        Handle an LLM API error. Returns True if we should retry
        (key was rotated), False if we should give up.
        """
        if not self._key_pool:
            return False

        if self._key_pool.is_auth_error(error):
            reason = f"Auth error: {str(error)[:100]}"
            self._key_pool.mark_permanently_bad(reason)
            self._key_pool.record_failure(str(error)[:200])
            log.warning("Key marked permanently bad: %s", reason)
            rotated = self._key_pool.rotate(reason)
            
            if not rotated and self._key_pool.all_exhausted():
                if not self._key_pool.has_valid_keys():
                    log.error("ALL keys are permanently bad! Cannot continue.")
                    if self.dashboard:
                        self.dashboard.log("🚨 ALL API KEYS ARE BROKEN OR INVALID! Please close the program, fix your .env file, and restart.", "ERROR")
                    return False
                
                # If some keys are just rate-limited, try backoff
                delay = self._key_pool.get_backoff_delay()
                if delay is not None:
                    log.warning("All keys exhausted (waiting on rate limits) — backing off %.0fs", delay)
                    if self.dashboard:
                        self.dashboard.log(f"⏳ API limits reached on all keys! Pausing for {int(delay)} seconds to wait for cooldown... (Do not close program)", "WARNING")
                    await asyncio.sleep(delay)
                    self._key_pool.reset_all()
                    return True
                else:
                    log.error("All keys exhausted and max backoff cycles reached")
                    if self.dashboard:
                        self.dashboard.log("❌ All API keys maxed out and maximum retries reached! Please close and try again later.", "ERROR")
                    return False
                    
            return rotated

        if self._key_pool.is_rotatable_error(error):
            reason = f"Rate limited: {str(error)[:100]}"
            self._key_pool.mark_rate_limited(reason)
            self._key_pool.record_failure(str(error)[:200])
            rotated = self._key_pool.rotate(reason)

            if not rotated and self._key_pool.all_exhausted():
                # All keys exhausted — try exponential backoff
                delay = self._key_pool.get_backoff_delay()
                if delay is not None:
                    log.warning(
                        "All keys exhausted — backing off %.0fs before resetting pool", delay
                    )
                    if self.dashboard:
                        self.dashboard.log(f"⏳ API limits reached on all keys! Pausing for {int(delay)} seconds to wait for cooldown... (Do not close program)", "WARNING")
                    await asyncio.sleep(delay)
                    self._key_pool.reset_all()
                    return True
                else:
                    log.error("All keys exhausted and max backoff cycles reached")
                    if self.dashboard:
                        self.dashboard.log("❌ All API keys maxed out and maximum retries reached! Please close and try again later.", "ERROR")
                    # Break the infinite loop by explicitly failing
                    return False

            return rotated

        return False

    async def _try_vision(self, goal: str, page_info: str, screenshot_bytes: bytes) -> Action | None:
        """Try vision-based inference. Returns None if model doesn't support vision."""
        from openai import AsyncOpenAI

        api_key = self._get_api_key()
        client = AsyncOpenAI(base_url=self._get_base_url(), api_key=api_key)
        messages = _build_vision_prompt(goal, page_info, screenshot_bytes)

        try:
            resp = await client.chat.completions.create(
                model=self._get_model(),
                messages=messages,
                temperature=0.1,
                max_tokens=512,
                timeout=180,
            )
            raw = (resp.choices[0].message.content or "").strip()
            log.debug("  vision LLM: %s", raw[:200])
            if self._key_pool:
                self._key_pool.record_success()
            if raw:
                return parse_action(raw)
        except Exception as e:
            err_str = str(e)
            err = err_str.lower()

            # Check if it's a key rotation error first
            if self._key_pool and self._key_pool.is_rotatable_error(e):
                await self._handle_error(e, "vision")
                return None

            if any(x in err for x in ("vision", "multimodal", "image", "format", "unsupported", "must be a string")):
                log.info("  Model does not support vision — falling back to text")
                self._vision_works = False
            else:
                log.info("  Vision unavailable — falling back to text")
                self._vision_works = False
        return None

    async def _try_text(self, goal: str, page_info: str) -> Action | None:
        """Text-only inference with retries and key rotation."""
        from openai import AsyncOpenAI

        messages = _build_prompt(goal, page_info)

        for msg in messages:
            if msg["role"] == "user" and isinstance(msg["content"], str) and len(msg["content"]) > 6000:
                msg["content"] = msg["content"][:6000] + "\n... (truncated)"

        for attempt in range(300):
            try:
                api_key = self._get_api_key()
                client = AsyncOpenAI(base_url=self._get_base_url(), api_key=api_key)

                resp = await client.chat.completions.create(
                    model=self._get_model(),
                    messages=messages,
                    temperature=0.1,
                    max_tokens=512,
                    timeout=90,
                )
                raw = (resp.choices[0].message.content or "").strip()
                log.debug("  text LLM [%d]: %s", attempt + 1, raw[:200])
                if self._key_pool:
                    self._key_pool.record_success()
                    self._key_pool.reset_backoff()
                if raw:
                    parsed = parse_action(raw)
                    if parsed is not None:
                        return parsed
                    log.warning("  Unparseable (attempt %d)", attempt + 1)
                else:
                    log.warning("  Empty response (attempt %d)", attempt + 1)
                    
                if attempt >= 2 and not self._key_pool:
                    break
                    
            except Exception as e:
                log.warning("  LLM request failed (attempt %d): %s", attempt + 1, e)
                # Try to rotate key on rotatable errors
                if self._key_pool:
                    retried = await self._handle_error(e, f"text attempt {attempt + 1}")
                    if retried:
                        continue  # Retry with new key
                    elif self._key_pool.all_exhausted() and self._key_pool.pool_cycle >= self._key_pool._max_pool_cycles:
                        # Max backoff reached, fail quickly
                        log.error("Failing request due to total key exhaustion.")
                        return Action(thought="API keys exhausted", action="done")
                else:
                    if attempt >= 2:
                        break

        return None

    async def decide(self, goal: str, page_info: str, screenshot_bytes: bytes | None = None) -> Action | None:
        """Hybrid decide: tries vision first if screenshot provided, falls back to text."""

        # Try vision if we have a screenshot and model might support it
        if screenshot_bytes is not None and self._vision_works is not False:
            action = await self._try_vision(goal, page_info, screenshot_bytes)
            if action is not None:
                self._vision_works = True
                return action
            if self._vision_works is False:
                pass  # fall through to text

        # Text-only fallback
        return await self._try_text(goal, page_info)
