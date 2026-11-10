"""Gnani Timbre TTS (REST) — the spoken warning.

Verified against https://docs.gnani.ai/api/TTS/tts-inference:

* ``POST https://api.vachana.ai/api/v1/tts/inference``
* header ``X-API-Key-ID``
* returns raw binary audio in the requested container (WAV here)

We ask for 16 kHz mono WAV so the browser can play it directly and so it
matches the STT sample rate.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from app.config import GnaniConfig
from app.keypool import AllKeysExhausted, KeyPool, retry_after_seconds

log = logging.getLogger(__name__)


class TimbreTTS:
    def __init__(self, pool: KeyPool, cfg: GnaniConfig) -> None:
        self._pool = pool
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=8.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        language: str | None = None,
        speed: float = 1.0,
        attempts: int = 3,
    ) -> bytes:
        """Return WAV bytes for ``text``. Raises ``AllKeysExhausted`` if no key works."""
        if not text.strip():
            raise ValueError("cannot synthesize empty text")

        cfg = self._cfg
        payload = {
            "text": text,
            "voice": voice or cfg.tts_voice,
            "model": cfg.tts_model,
            "language": language or cfg.tts_language,
            "speed": speed,
            "audio_config": {
                "sample_rate": 16_000,
                "num_channels": 1,
                "sample_width": 2,
                "encoding": "linear_pcm",
                "container": "wav",
            },
        }

        last_error = "no attempt made"
        for _ in range(attempts):
            api_key = await self._pool.acquire()
            try:
                response = await self._client.post(
                    cfg.tts_url,
                    json=payload,
                    headers={"X-API-Key-ID": api_key},
                )
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                self._pool.report_failure(api_key)
                last_error = type(exc).__name__
                continue

            if response.status_code == 200:
                self._pool.report_success(api_key)
                audio = response.content
                if not audio:
                    last_error = "empty audio"
                    continue
                return audio

            # Gnani returns flat JSON errors for 400/500 and a detail envelope
            # for 401/403/429.
            last_error = f"HTTP {response.status_code}: {_error_message(response)}"
            self._pool.report_failure(
                api_key,
                status=response.status_code,
                retry_after=retry_after_seconds(response.headers),
            )

        raise RuntimeError(f"Timbre TTS failed: {last_error}")

    async def synthesize_to_file(self, text: str, path: str | Path, **kwargs: object) -> Path:
        audio = await self.synthesize(text, **kwargs)  # type: ignore[arg-type]
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(audio)
        return target


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        if "message" in body:
            return str(body["message"])[:200]
        detail = body.get("detail")
        if isinstance(detail, dict) and "message" in detail:
            return str(detail["message"])[:200]
        if "error" in body and isinstance(body["error"], dict):
            return str(body["error"].get("message", ""))[:200]
    return str(body)[:200]
