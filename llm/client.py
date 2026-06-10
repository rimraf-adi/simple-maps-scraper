"""
OpenAI-compatible LLM client for hybrid DOM + vision agent.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("maps_scraper.llm")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
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
    def __init__(self, base_url: str = LLM_BASE_URL, model: str = LLM_MODEL):
        self.base_url = base_url.rstrip("/")
        self.model = model or "qwen3.5:0.8b"
        self._vision_works = None  # None = untested, True/False = cached

    async def _try_vision(self, goal: str, page_info: str, screenshot_bytes: bytes) -> Action | None:
        """Try vision-based inference. Returns None if model doesn't support vision."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=self.base_url, api_key=LLM_API_KEY)
        messages = _build_vision_prompt(goal, page_info, screenshot_bytes)

        try:
            resp = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1,
                max_tokens=512,
                timeout=180,
            )
            raw = (resp.choices[0].message.content or "").strip()
            log.debug("  vision LLM: %s", raw[:200])
            if raw:
                return parse_action(raw)
        except Exception as e:
            err_str = str(e)
            err = err_str.lower()
            if any(x in err for x in ("vision", "multimodal", "image", "format", "unsupported", "must be a string")):
                log.info("  Model does not support vision — falling back to text")
                self._vision_works = False
            else:
                log.info("  Vision unavailable — falling back to text")
                self._vision_works = False
        return None

    async def _try_text(self, goal: str, page_info: str) -> Action | None:
        """Text-only inference with retries."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=self.base_url, api_key=LLM_API_KEY)
        messages = _build_prompt(goal, page_info)

        for msg in messages:
            if msg["role"] == "user" and isinstance(msg["content"], str) and len(msg["content"]) > 6000:
                msg["content"] = msg["content"][:6000] + "\n... (truncated)"

        for attempt in range(3):
            try:
                resp = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=512,
                    timeout=90,
                )
                raw = (resp.choices[0].message.content or "").strip()
                log.debug("  text LLM [%d]: %s", attempt + 1, raw[:200])
                if raw:
                    parsed = parse_action(raw)
                    if parsed is not None:
                        return parsed
                    log.warning("  Unparseable (attempt %d)", attempt + 1)
                else:
                    log.warning("  Empty response (attempt %d)", attempt + 1)
            except Exception as e:
                log.warning("  LLM request failed (attempt %d): %s", attempt + 1, e)

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
