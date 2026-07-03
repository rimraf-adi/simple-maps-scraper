"""
Multi-API Key Pool with auto-rotation and exponential backoff.

Supports both Groq and Gemini keys automatically by detecting their prefix.
Supports 1–15 keys via GEMINI_API_KEY_1..15 or GROQ_API_KEY_1..15 env vars.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from threading import Lock

log = logging.getLogger("maps_scraper.keypool")


@dataclass
class _KeyState:
    """Internal state for a single API key."""
    key: str
    index: int
    provider: str
    base_url: str
    model: str
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    last_error: str = ""
    is_rate_limited: bool = False
    is_permanently_bad: bool = False
    cooldown_until: float = 0.0  # timestamp when this key can be used again


class KeyPool:
    """Manages multiple LLM API keys with automatic rotation on errors."""

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("At least one API key is required")
            
        self._keys: list[_KeyState] = []
        for i, k in enumerate(keys):
            if k.startswith("gsk_"):
                provider = "groq"
                base_url = "https://api.groq.com/openai/v1"
                model = "llama-3.1-8b-instant"
            elif k.startswith("AIzaSy") or k.startswith("AQ."):
                provider = "gemini"
                base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
                model = "gemini-3.1-flash-lite"
            else:
                provider = "unknown"
                base_url = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
                model = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite")
                
            self._keys.append(_KeyState(
                key=k, index=i, provider=provider, base_url=base_url, model=model
            ))
            
        self._current_index: int = 0
        self._lock: Lock = Lock()
        self._pool_cycle: int = 0
        self._max_pool_cycles: int = 100

    @classmethod
    def from_env(cls, provider: str = "gemini") -> "KeyPool":
        """Load keys from environment, supporting numbered formats based on provider selection."""
        from dotenv import load_dotenv
        load_dotenv()

        keys: list[str] = []
        prefix = "GEMINI_API_KEY_" if provider.lower() == "gemini" else "GROQ_API_KEY_"
        
        for i in range(1, 16):
            k = os.getenv(f"{prefix}{i}", "").strip()
            if k:
                keys.append(k)

        # Fallback to legacy single key
        if not keys:
            legacy = os.getenv("LLM_API_KEY", "").strip()
            if legacy:
                keys.append(legacy)

        if not keys:
            raise ValueError(
                f"No API keys found for {provider}. Set {prefix}1..15 in .env"
            )

        return cls(keys=keys)

    @property
    def total_keys(self) -> int:
        return len(self._keys)

    @property
    def current_index(self) -> int:
        return self._current_index

    def current_key(self) -> str:
        """Return the currently active API key."""
        with self._lock:
            return self._keys[self._current_index].key
            
    def current_base_url(self) -> str:
        with self._lock:
            return self._keys[self._current_index].base_url
            
    def current_model(self) -> str:
        with self._lock:
            return self._keys[self._current_index].model
            
    def current_provider(self) -> str:
        with self._lock:
            return self._keys[self._current_index].provider

    def record_success(self) -> None:
        """Record a successful API call on the current key."""
        with self._lock:
            state = self._keys[self._current_index]
            state.total_calls += 1
            state.successful_calls += 1
            # Clear rate limit flag on success
            state.is_rate_limited = False

    def record_failure(self, error: str) -> None:
        """Record a failed API call on the current key."""
        with self._lock:
            state = self._keys[self._current_index]
            state.total_calls += 1
            state.failed_calls += 1
            state.last_error = error

    def rotate(self, reason: str = "") -> bool:
        """
        Rotate to the next available key.
        Returns True if a new key was found, False if all keys are exhausted.
        """
        with self._lock:
            return self._rotate_locked(reason)

    def _rotate_locked(self, reason: str) -> bool:
        """Internal rotation logic (must be called with lock held)."""
        old_index = self._current_index
        now = time.time()
        tried = 0
        while tried < len(self._keys):
            next_idx = (self._current_index + 1) % len(self._keys)
            self._current_index = next_idx
            state = self._keys[next_idx]
            tried += 1
            if not state.is_permanently_bad and not state.is_rate_limited and now >= state.cooldown_until:
                masked = self._mask_key(state.key)
                log.info(
                    "⚡ API key rotated: %s → Key %d/%d (%s) reason=%s",
                    old_index + 1, next_idx + 1, len(self._keys), masked, reason
                )
                return True

        # All keys exhausted
        return False

    def mark_rate_limited(self, reason: str = "") -> None:
        """Mark the current key as rate-limited (temporary — will be reset on backoff)."""
        with self._lock:
            self._keys[self._current_index].is_rate_limited = True
            self._keys[self._current_index].last_error = reason

    def set_cooldown(self, seconds: float) -> None:
        """Set a cooldown timer on the current key so it's skipped until the timer expires."""
        with self._lock:
            self._keys[self._current_index].cooldown_until = time.time() + seconds

    def mark_permanently_bad(self, reason: str = "") -> None:
        """Mark the current key as permanently unusable (genuinely broken/revoked key)."""
        with self._lock:
            self._keys[self._current_index].is_permanently_bad = True
            self._keys[self._current_index].last_error = reason

    def reset_all(self) -> None:
        """Reset all rate-limited keys and cooldowns (but not permanently bad ones)."""
        with self._lock:
            for state in self._keys:
                if not state.is_permanently_bad:
                    state.is_rate_limited = False
                    state.cooldown_until = 0.0
            self._current_index = 0
            # Skip permanently bad keys
            for i, state in enumerate(self._keys):
                if not state.is_permanently_bad:
                    self._current_index = i
                    break

    def all_exhausted(self) -> bool:
        """Check if all keys are either rate-limited, on cooldown, or permanently bad."""
        with self._lock:
            now = time.time()
            return all(
                s.is_permanently_bad or s.is_rate_limited or now < s.cooldown_until
                for s in self._keys
            )

    def has_valid_keys(self) -> bool:
        """Return True if there is at least one key that is NOT permanently bad."""
        with self._lock:
            return any(not s.is_permanently_bad for s in self._keys)

    def get_backoff_delay(self) -> float | None:
        """
        Return the backoff delay in seconds for the current pool cycle,
        or None if max cycles exceeded.
        """
        if self._pool_cycle >= self._max_pool_cycles:
            return None
        delays = [30.0, 60.0, 120.0]
        delay = delays[min(self._pool_cycle, len(delays) - 1)]
        self._pool_cycle += 1
        return delay

    def get_shortest_cooldown(self) -> float:
        """Return the shortest remaining cooldown time across all non-permanently-bad keys."""
        with self._lock:
            now = time.time()
            cooldowns = []
            for s in self._keys:
                if not s.is_permanently_bad and s.cooldown_until > now:
                    cooldowns.append(s.cooldown_until - now)
            return min(cooldowns) if cooldowns else 0.0

    def reset_backoff(self) -> None:
        """Reset the pool cycle counter."""
        self._pool_cycle = 0

    @property
    def pool_cycle(self) -> int:
        return self._pool_cycle

    def status(self) -> list[dict]:
        """Return status info for each key, suitable for UI display."""
        with self._lock:
            result = []
            for state in self._keys:
                result.append({
                    "index": state.index + 1,
                    "provider": state.provider,
                    "masked_key": self._mask_key(state.key),
                    "total_calls": state.total_calls,
                    "successful_calls": state.successful_calls,
                    "failed_calls": state.failed_calls,
                    "last_error": state.last_error,
                    "is_rate_limited": state.is_rate_limited,
                    "is_permanently_bad": state.is_permanently_bad,
                    "is_active": state.index == self._current_index,
                })
            return result

    @staticmethod
    def _mask_key(key: str) -> str:
        """Mask an API key for display: show first 8 and last 4 chars."""
        if len(key) <= 12:
            return key[:4] + "..." + key[-2:]
        return key[:8] + "..." + key[-4:]

    @staticmethod
    def extract_retry_delay(error: Exception) -> float | None:
        """Extract the retry delay from a rate limit error message (e.g. 'retry in 46.7s')."""
        err_msg = str(error)
        # Match patterns like "retry in 46.716724325s" or "retry in 4.82s" or "Please try again in 5.57s"
        m = re.search(r"(?:retry|try again) in (\d+(?:\.\d+)?)s", err_msg, re.IGNORECASE)
        if m:
            return float(m.group(1))
        return None

    def is_rotatable_error(self, error: Exception) -> bool:
        """Check if an exception should trigger key rotation (temporary error)."""
        err_type = type(error).__name__
        err_msg = str(error).lower()

        # Hard limits (daily quota exceeded) should NOT be rotated temporarily.
        # They must fall through to is_auth_error to be marked permanently bad.
        if "billing details" in err_msg or "billing account" in err_msg or "out of credits" in err_msg:
            return False

        # ANY 429 is always a rate limit — rotate immediately
        if err_type == "RateLimitError" or "429" in err_msg:
            return True

        # Authentication errors should also rotate (to try next key)
        if err_type == "AuthenticationError" or "401" in err_msg:
            return True

        # Server overload
        if "503" in err_msg:
            return True

        # Message content checks
        rotate_signals = [
            "rate limit", "quota", "limit exceeded",
            "rate_limit", "too many requests",
            "overloaded", "resource_exhausted",
        ]
        return any(signal in err_msg for signal in rotate_signals)

    def is_auth_error(self, error: Exception) -> bool:
        """
        Check if an error indicates a GENUINELY broken/revoked API key.
        
        CRITICAL: This must NEVER match rate limit errors (429).
        A 429 means the key is valid but temporarily throttled.
        Only match errors that prove the key itself is invalid/revoked.
        """
        err_type = type(error).__name__
        err_msg = str(error).lower()

        # ── Daily Quota / Hard Limits (Effectively permanently bad for this session) ──
        # "You exceeded your current quota, please check your plan and billing details"
        if "billing details" in err_msg or "billing account" in err_msg or "out of credits" in err_msg:
            return True

        # ── SAFETY: Never treat standard rate limits as auth errors ──
        # 429 errors are ALWAYS rate limits (unless it's a billing/hard quota error caught above)
        if "429" in err_msg or err_type == "RateLimitError":
            return False
        # Quota/rate limit keywords — always temporary unless caught above
        if any(kw in err_msg for kw in ("rate limit", "rate_limit", "resource_exhausted", "too many requests")):
            return False

        # ── True auth errors (key is genuinely broken) ──
        # OpenAI SDK AuthenticationError type
        if err_type == "AuthenticationError":
            return True
        # Gemini: "Invalid Auth key" (400)
        if "invalid auth key" in err_msg:
            return True
        # Generic: 401 with invalid/unauthorized
        if "401" in err_msg and ("invalid" in err_msg or "unauthorized" in err_msg):
            return True
        # Generic: api_key_invalid
        if "api_key_invalid" in err_msg:
            return True
        # Gemini: project denied access (403)
        if "denied access" in err_msg or "permission_denied" in err_msg:
            return True

        return False
