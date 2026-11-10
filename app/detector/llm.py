"""Layer 2 — Groq reasoning over a rolling transcript window.

The LLM is one input to the score, never the decision. It is asked for strict
JSON, and anything malformed is discarded rather than guessed at. A failure
here degrades the detector to rules-only instead of breaking the call.

Responses are cached by window hash so evaluation runs are reproducible and
cheap, and so re-running the table after a rule tweak does not re-bill tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any

import httpx

from app.config import GroqConfig
from app.detector.models import ALL_SIGNALS, LlmReading, Signals
from app.keypool import AllKeysExhausted, KeyPool, retry_after_seconds

log = logging.getLogger(__name__)

CACHE_MAX_ENTRIES = 512

SYSTEM_PROMPT = """You are a fraud analyst listening to a phone call in Hindi, Hinglish or Indian English.
Your job is to judge whether the CALLER is trying to scam the person who received the call.

Reply with ONLY a JSON object, no prose, no markdown, exactly this shape:
{
  "signals": {
    "authority": true/false,
    "fear": true/false,
    "urgency": true/false,
    "isolation": true/false,
    "sensitive_request": true/false,
    "money_request": true/false
  },
  "risk": 0.0 to 1.0,
  "reason_hi": "one short sentence in Hindi",
  "reason_en": "one short sentence in English"
}

Signal meanings:
- authority: claims to be police, CBI, customs, court, bank fraud department, TRAI, RBI, or a government officer.
- fear: threatens arrest, jail, legal case, frozen account, or says the listener is already in trouble.
- urgency: pushes the listener to act within minutes, says this is the last chance or final notice.
- isolation: tells the listener not to tell anyone, to stay on the line, or not to hang up.
- sensitive_request: asks for an OTP, Aadhaar, PAN, card number, CVV, PIN, bank details, or asks them to install AnyDesk/TeamViewer or share their screen.
- money_request: asks for a transfer, UPI payment, fee, penalty, deposit or clearance payment.

Scoring rules:
- Judge only what is actually in the transcript. Do not invent.
- One signal alone is common in legitimate calls. Keep risk below 0.5 unless at least two signals reinforce each other.
- Use risk >= 0.75 only when authority or fear is combined with a demand for money, OTP, identity documents or remote access, or with a demand for secrecy.
- These are legitimate and must stay low unless there is an explicit threat or secrecy demand: a bank reminding about an EMI, a delivery agent asking for an OTP to hand over a parcel, a clinic confirming an appointment, a relative genuinely asking for help.
- reason_hi must be calm, short and specific. Never write more than 20 words."""


class _BadPayload(Exception):
    """The model returned something that is not usable JSON."""


class LlmDetector:
    def __init__(self, pool: KeyPool, cfg: GroqConfig) -> None:
        self._pool = pool
        self._cfg = cfg
        self._cache: OrderedDict[str, LlmReading] = OrderedDict()
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            timeout=httpx.Timeout(cfg.timeout_s, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def assess(self, window_text: str) -> LlmReading:
        """Score a transcript window. Never raises; returns ``ok=False`` instead."""
        if not window_text.strip():
            return LlmReading(risk=0.0, ok=True, reason_en="No speech yet.")

        cache_key = self._cache_key(window_text)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached

        models = [self._cfg.model, self._cfg.fallback_model]
        last_error = "no attempt made"

        for attempt in range(self._cfg.max_attempts):
            model = models[min(attempt, len(models) - 1)]
            try:
                api_key = await self._pool.acquire()
            except AllKeysExhausted as exc:
                return LlmReading(ok=False, error=str(exc))

            started = time.perf_counter()
            try:
                reading = await self._call(api_key, model, window_text)
            except _BadPayload as exc:
                last_error = f"bad JSON: {exc}"
                log.warning("Groq returned unusable JSON (attempt %d)", attempt + 1)
                continue
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                self._pool.report_failure(
                    api_key, status=status, retry_after=retry_after_seconds(exc.response.headers)
                )
                last_error = f"HTTP {status}"
                continue
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                self._pool.report_failure(api_key)
                last_error = type(exc).__name__
                continue

            self._pool.report_success(api_key)
            reading = LlmReading(
                signals=reading.signals,
                risk=reading.risk,
                reason_hi=reading.reason_hi,
                reason_en=reading.reason_en,
                model=model,
                latency_ms=int((time.perf_counter() - started) * 1000),
                ok=True,
            )
            self._remember(cache_key, reading)
            return reading

        return LlmReading(ok=False, error=last_error)

    # -- internals --------------------------------------------------------
    async def _call(self, api_key: str, model: str, window_text: str) -> LlmReading:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Transcript (oldest first):\n{window_text}"},
            ],
            "temperature": self._cfg.temperature,
            "max_tokens": self._cfg.max_tokens,
            "response_format": {"type": "json_object"},
        }
        response = await self._client.post(
            "/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return _parse(content)

    def _cache_key(self, text: str) -> str:
        digest = hashlib.sha1(f"{self._cfg.model}\x00{text}".encode("utf-8")).hexdigest()
        return digest

    def _remember(self, key: str, reading: LlmReading) -> None:
        self._cache[key] = reading
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)


def _parse(content: Any) -> LlmReading:
    if not isinstance(content, str) or not content.strip():
        raise _BadPayload("empty content")

    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _BadPayload(str(exc)) from exc

    if not isinstance(data, dict):
        raise _BadPayload("not an object")

    raw_risk = data.get("risk")
    try:
        risk = float(raw_risk)
    except (TypeError, ValueError) as exc:
        raise _BadPayload("risk not numeric") from exc

    signals = Signals.from_mapping(data.get("signals"))
    unknown = set((data.get("signals") or {}).keys()) - {s.value for s in ALL_SIGNALS}
    if unknown:
        log.debug("ignoring unknown signals: %s", unknown)

    return LlmReading(
        signals=signals,
        risk=max(0.0, min(1.0, risk)),
        reason_hi=str(data.get("reason_hi") or "").strip(),
        reason_en=str(data.get("reason_en") or "").strip(),
        ok=True,
    )
