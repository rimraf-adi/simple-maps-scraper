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


SYSTEM_PROMPT = """Extract email from business website. Pick next action from numbered elements.
Actions must be valid JSON matching one of these EXACT formats:
1. {"thought":"...", "action":"click", "target": N}
2. {"thought":"...", "action":"scroll", "text": "down"}
3. {"thought":"...", "action":"extract", "data": {"email": "x@y.com"}}
4. {"thought":"...", "action":"done"}
Reply ONLY as JSON without markdown formatting.
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
    target = obj.get("target")
    text_val = obj.get("text", "")
    data = obj.get("data")

    # Smart fallback for 8B models that hallucinate the action string from the old prompt
    if "click target=" in action:
        try:
            target = int(action.split("=")[1].strip())
            action = "click"
        except (IndexError, ValueError):
            pass
    elif "scroll text=" in action:
        action = "scroll"
        text_val = "down"

    if action not in ("click", "type", "scroll", "extract", "done"):
        log.warning("Unknown action '%s'", action)
        return None

    return Action(
        thought=obj.get("thought", ""),
        action=action,
        target=target,
        text=text_val,
        data=data,
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
        self._vision_works = False  # Disabled — Groq/Gemini text-only saves tokens
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
        (key was rotated or cooldown waited), False if we should give up.
        
        CRITICAL ORDER: Check rate limits FIRST, auth errors SECOND.
        """
        if not self._key_pool:
            return False

        # ── STEP 1: Rate limit / temporary errors (most common) ──
        # Check this FIRST so 429s with "billing" in the URL never hit is_auth_error
        if self._key_pool.is_rotatable_error(error):
            reason = f"Rate limited: {str(error)[:100]}"
            
            # Extract the exact retry delay the API tells us (e.g., "retry in 46s")
            retry_delay = self._key_pool.extract_retry_delay(error)
            if retry_delay:
                # Set a per-key cooldown so this specific key is skipped until ready
                self._key_pool.set_cooldown(retry_delay + 1.0)
                log.info("Key %d cooldown set for %.0fs", self._key_pool.current_index + 1, retry_delay)
            else:
                self._key_pool.mark_rate_limited(reason)
            
            self._key_pool.record_failure(str(error)[:200])
            rotated = self._key_pool.rotate(reason)

            if rotated:
                return True  # Successfully moved to another key, retry immediately

            # All keys are exhausted — wait for the shortest cooldown
            if self._key_pool.all_exhausted():
                if not self._key_pool.has_valid_keys():
                    # Every key is genuinely broken (not just rate limited)
                    log.error("ALL keys are permanently bad! Cannot continue.")
                    if self.dashboard:
                        self.dashboard.log("🚨 ALL API KEYS ARE BROKEN OR INVALID! Please check your .env file.", "ERROR")
                    return False

                # Keys exist but are all on cooldown/rate-limited — WAIT and retry
                shortest = self._key_pool.get_shortest_cooldown()
                wait_time = max(shortest, 10.0)  # Wait at least 10s
                wait_time = min(wait_time, 60.0)  # But never more than 60s
                
                log.info("All keys rate-limited. Waiting %.0fs for cooldowns to expire...", wait_time)
                if self.dashboard:
                    self.dashboard.log(f"⏳ All keys rate-limited. Waiting {int(wait_time)}s for cooldown... (normal, do not close)", "WARNING")
                
                await asyncio.sleep(wait_time)
                self._key_pool.reset_all()
                return True

            return False

        # ── STEP 2: Genuine auth errors (key is truly broken/revoked) ──
        if self._key_pool.is_auth_error(error):
            reason = f"Auth error: {str(error)[:100]}"
            self._key_pool.mark_permanently_bad(reason)
            self._key_pool.record_failure(str(error)[:200])
            log.warning("Key marked permanently bad: %s", reason)
            rotated = self._key_pool.rotate(reason)
            
            if rotated:
                return True  # Moved to next key
                
            if not self._key_pool.has_valid_keys():
                log.error("ALL keys are permanently bad! Cannot continue.")
                if self.dashboard:
                    self.dashboard.log("🚨 ALL API KEYS ARE BROKEN OR INVALID! Please check your .env file.", "ERROR")
                return False
            
            # Some keys are rate-limited but not permanently bad — wait
            shortest = self._key_pool.get_shortest_cooldown()
            wait_time = max(shortest, 10.0)
            wait_time = min(wait_time, 60.0)
            log.info("Waiting %.0fs for rate-limited keys to recover...", wait_time)
            if self.dashboard:
                self.dashboard.log(f"⏳ Waiting {int(wait_time)}s for API cooldown...", "WARNING")
            await asyncio.sleep(wait_time)
            self._key_pool.reset_all()
            return True

        # ── STEP 3: Unknown error — brief pause then retry ──
        self._key_pool.record_failure(str(error)[:200])
        return False

    async def _try_vision(self, goal: str, page_info: str, screenshot_bytes: bytes) -> Action | None:
        """Try vision-based inference. Returns None if model doesn't support vision."""
        from openai import AsyncOpenAI

        api_key = self._get_api_key()
        base_url = self._get_base_url()
        
        headers = None
        if "googleapis.com" in base_url:
            headers = {"x-goog-api-key": api_key}
            
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, default_headers=headers)
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

        # Aggressively truncate to save tokens
        for msg in messages:
            if msg["role"] == "user" and isinstance(msg["content"], str) and len(msg["content"]) > 3000:
                msg["content"] = msg["content"][:3000] + "\n... (truncated)"

        max_attempts = 20  # Reasonable limit, not 300
        consecutive_failures = 0

        for attempt in range(max_attempts):
            try:
                api_key = self._get_api_key()
                base_url = self._get_base_url()
                
                headers = None
                if "googleapis.com" in base_url:
                    headers = {"x-goog-api-key": api_key}
                    
                client = AsyncOpenAI(base_url=base_url, api_key=api_key, default_headers=headers)

                resp = await client.chat.completions.create(
                    model=self._get_model(),
                    messages=messages,
                    temperature=0.0,
                    max_tokens=200,
                    timeout=90,
                )
                raw = (resp.choices[0].message.content or "").strip()
                log.debug("  text LLM [%d]: %s", attempt + 1, raw[:200])
                if self._key_pool:
                    self._key_pool.record_success()
                    self._key_pool.reset_backoff()
                
                consecutive_failures = 0  # Reset on successful API call
                
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
                consecutive_failures += 1
                
                if self._key_pool and self._key_pool.is_rotatable_error(e):
                    # It's a standard rate limit, we will rotate. Log as INFO so user doesn't panic.
                    log.info("  \u21ba Rate limit reached. Auto-rotating to next API key... (attempt %d)", attempt + 1)
                else:
                    log.warning("  LLM request failed (attempt %d): %s", attempt + 1, str(e)[:200])
                
                if self._key_pool:
                    retried = await self._handle_error(e, f"text attempt {attempt + 1}")
                    if retried:
                        continue  # Retry with new/refreshed key
                    
                    # _handle_error returned False — check if we should give up
                    if not self._key_pool.has_valid_keys():
                        log.error("All keys permanently exhausted. Giving up on this lead.")
                        return Action(thought="API keys exhausted", action="done")
                    
                    # Brief pause before retrying with same state
                    await asyncio.sleep(2)
                else:
                    if attempt >= 2:
                        break
                    await asyncio.sleep(2)
                
                # If we've failed 5 times in a row, give up on this lead to avoid wasting tokens
                if consecutive_failures >= 5:
                    log.warning("5 consecutive failures — skipping this lead to save tokens")
                    return Action(thought="Too many failures, skipping", action="done")

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
