"""Round-robin API key pool with per-key cooldown and disable-on-auth-failure.

Every outbound provider (Groq, Gnani STT, Gnani TTS) goes through one of these,
so a single dead or rate-limited key never takes the demo down.

Note on streaming: Gnani STT is a WebSocket, so a 401 is a failed *handshake*
and rotation means reconnecting with the next key. ``acquire()`` is therefore
called before every connection attempt, not just on HTTP retries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# A 429 without a Retry-After header: assume the shortest useful window.
DEFAULT_RATE_LIMIT_COOLDOWN_S = 30.0
# Transient failures (5xx, timeout, bad handshake): brief cooldown.
DEFAULT_TRANSIENT_COOLDOWN_S = 5.0

AUTH_STATUS_CODES = frozenset({401, 403})
RATE_LIMIT_STATUS_CODES = frozenset({429})


class AllKeysExhausted(RuntimeError):
    """No usable key remains. Raised loudly — never silently degraded."""


@dataclass
class _Key:
    value: str
    disabled: bool = False
    cooldown_until: float = 0.0
    uses: int = 0
    failures: int = 0

    @property
    def masked(self) -> str:
        return f"…{self.value[-4:]}" if len(self.value) > 4 else "****"

    def usable(self, now: float) -> bool:
        return not self.disabled and self.cooldown_until <= now


class KeyPool:
    """Async-safe rotating pool.

    Keys are handed out in round-robin order. A key that returns 401/403 is
    disabled for the process lifetime; a 429 puts it on cooldown.
    """

    def __init__(self, keys: list[str], name: str = "pool") -> None:
        if not keys:
            raise ValueError(f"{name}: no API keys configured (check .env)")
        # De-duplicate while preserving order — duplicated keys would just
        # multiply the same rate limit.
        seen: set[str] = set()
        unique = [k for k in keys if not (k in seen or seen.add(k))]
        self._keys = [_Key(k) for k in unique]
        self._name = name
        self._cursor = 0
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._keys)

    async def acquire(self) -> str:
        """Return the next usable key, or raise ``AllKeysExhausted``."""
        async with self._lock:
            now = time.monotonic()
            count = len(self._keys)
            for offset in range(count):
                idx = (self._cursor + offset) % count
                key = self._keys[idx]
                if key.usable(now):
                    self._cursor = (idx + 1) % count
                    key.uses += 1
                    return key.value

            disabled = sum(1 for k in self._keys if k.disabled)
            waiting = [k.cooldown_until for k in self._keys if not k.disabled]
            next_in = max(0.0, min(waiting) - now) if waiting else float("inf")
            raise AllKeysExhausted(
                f"{self._name}: all {count} keys unusable "
                f"({disabled} disabled, next cooldown in {next_in:.1f}s)"
            )

    def report_success(self, value: str) -> None:
        key = self._find(value)
        if key is not None:
            key.failures = 0

    def report_failure(
        self,
        value: str,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        key = self._find(value)
        if key is None:
            return
        key.failures += 1

        if status in AUTH_STATUS_CODES:
            key.disabled = True
            log.error("%s: key %s rejected (%s) — disabled", self._name, key.masked, status)
            return

        if status in RATE_LIMIT_STATUS_CODES:
            cooldown = retry_after if retry_after is not None else DEFAULT_RATE_LIMIT_COOLDOWN_S
            key.cooldown_until = time.monotonic() + cooldown
            log.warning("%s: key %s rate-limited, cooling down %.0fs", self._name, key.masked, cooldown)
            return

        cooldown = DEFAULT_TRANSIENT_COOLDOWN_S * key.failures
        key.cooldown_until = time.monotonic() + cooldown
        log.warning(
            "%s: key %s failed (status=%s), cooling down %.0fs",
            self._name, key.masked, status, cooldown,
        )

    def snapshot(self) -> list[dict[str, object]]:
        """Non-secret view for the health endpoint and logs."""
        now = time.monotonic()
        return [
            {
                "key": k.masked,
                "disabled": k.disabled,
                "cooling_down_s": round(max(0.0, k.cooldown_until - now), 1),
                "uses": k.uses,
                "failures": k.failures,
            }
            for k in self._keys
        ]

    def _find(self, value: str) -> _Key | None:
        for key in self._keys:
            if key.value == value:
                return key
        return None


def retry_after_seconds(headers: object) -> float | None:
    """Parse a Retry-After header defensively (seconds only, not HTTP dates)."""
    raw = None
    try:
        raw = headers.get("retry-after")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
