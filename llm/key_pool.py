"""
Multi-API Key Pool with auto-rotation and exponential backoff.

Supports both Groq and Gemini keys automatically by detecting their prefix.
Supports 1–15 keys via LLM_API_KEY_1..LLM_API_KEY_15 env vars.
"""

from __future__ import annotations

import logging
import os
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
                model = "llama-3.3-70b-versatile"
            elif k.startswith("AIzaSy"):
                provider = "gemini"
                base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
                model = "gemini-2.0-flash"
            else:
                # Default to Groq if unknown, but user can override via env vars
                provider = "unknown"
                base_url = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
                model = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
                
            self._keys.append(_KeyState(
                key=k, index=i, provider=provider, base_url=base_url, model=model
            ))
            
        self._current_index: int = 0
        self._lock: Lock = Lock()
        self._pool_cycle: int = 0
        self._max_pool_cycles: int = 100

    @classmethod
    def from_env(cls) -> "KeyPool":
        """Load keys from environment, supporting numbered formats."""
        from dotenv import load_dotenv
        load_dotenv()

        keys: list[str] = []
        for i in range(1, 16):
            k = os.getenv(f"LLM_API_KEY_{i}", "").strip()
            if k:
                keys.append(k)

        # Fallback to legacy single key
        if not keys:
            legacy = os.getenv("LLM_API_KEY", "").strip()
            if legacy:
                keys.append(legacy)

        if not keys:
            raise ValueError(
                "No API keys found. Set LLM_API_KEY_1..LLM_API_KEY_15 or LLM_API_KEY in .env"
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
        tried = 0
        while tried < len(self._keys):
            next_idx = (self._current_index + 1) % len(self._keys)
            self._current_index = next_idx
            state = self._keys[next_idx]
            tried += 1
            if not state.is_permanently_bad and not state.is_rate_limited:
                masked = self._mask_key(state.key)
                log.info(
                    "⚡ API key rotated: %s → Key %d/%d (%s) reason=%s",
                    old_index + 1, next_idx + 1, len(self._keys), masked, reason
                )
                return True

        # All keys exhausted
        return False

    def mark_rate_limited(self, reason: str = "") -> None:
        """Mark the current key as rate-limited."""
        with self._lock:
            self._keys[self._current_index].is_rate_limited = True
            self._keys[self._current_index].last_error = reason

    def mark_permanently_bad(self, reason: str = "") -> None:
        """Mark the current key as permanently unusable (bad auth)."""
        with self._lock:
            self._keys[self._current_index].is_permanently_bad = True
            self._keys[self._current_index].last_error = reason

    def reset_all(self) -> None:
        """Reset all rate-limited keys (but not permanently bad ones)."""
        with self._lock:
            for state in self._keys:
                if not state.is_permanently_bad:
                    state.is_rate_limited = False
            self._current_index = 0
            # Skip permanently bad keys
            for i, state in enumerate(self._keys):
                if not state.is_permanently_bad:
                    self._current_index = i
                    break

    def all_exhausted(self) -> bool:
        """Check if all keys are either rate-limited or permanently bad."""
        with self._lock:
            return all(
                s.is_rate_limited or s.is_permanently_bad for s in self._keys
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

    def is_rotatable_error(self, error: Exception) -> bool:
        """Check if an exception should trigger key rotation."""
        err_type = type(error).__name__
        err_msg = str(error).lower()

        # OpenAI SDK specific error types
        if err_type in ("RateLimitError",):
            return True
        if err_type in ("AuthenticationError",):
            return True

        # HTTP status code checks
        if "429" in err_msg:
            return True
        if "401" in err_msg:
            return True
        if "503" in err_msg:
            return True

        # Message content checks
        rotate_signals = [
            "rate limit", "quota", "limit exceeded",
            "rate_limit", "too many requests",
            "overloaded",
        ]
        return any(signal in err_msg for signal in rotate_signals)

    def is_auth_error(self, error: Exception) -> bool:
        """Check if an error indicates a permanently bad key."""
        err_type = type(error).__name__
        err_msg = str(error).lower()
        if err_type == "AuthenticationError":
            return True
        if "401" in err_msg and ("invalid" in err_msg or "unauthorized" in err_msg):
            return True
        if "api_key_invalid" in err_msg:
            return True
        if "400" in err_msg and "valid api key" in err_msg:
            return True
        return False
