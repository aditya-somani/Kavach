"""Gnani Prisma streaming STT client.

Verified against https://docs.gnani.ai/api/STT/stt-websocket:

* ``wss://api.vachana.ai/stt/v3/stream``
* configuration is sent as **WebSocket upgrade headers**
* audio is raw PCM s16le mono, **exactly 1024 bytes per frame** (512 samples)
* only *final* transcripts come back — there are no interim results
* a session is hard-capped at 15 minutes, and closes with code 1001

Two things this client owns that the docs make easy to get wrong:

1. **Reconnect on key rotation.** A rejected key fails the handshake with 401,
   so rotating keys means reconnecting, not retrying an HTTP call.
2. **Session rollover.** Because of the 15-minute cap we reconnect proactively
   just before the cap instead of losing the call mid-sentence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import websockets

from app.audio_sources.base import AudioSource
from app.config import AudioConfig, GnaniConfig
from app.keypool import AllKeysExhausted, KeyPool
from app.stt.base import STTClient, TranscriptSegment

log = logging.getLogger(__name__)

# Reconnect this many seconds before the server's 15-minute cap.
ROLLOVER_MARGIN_S = 30
# Give up if this many consecutive sessions die without producing a transcript,
# so a broken key never spins forever.
MAX_FRUITLESS_RECONNECTS = 3


class _Reconnect(Exception):
    """Internal: tear down this session and open a new one."""


class PrismaSTT(STTClient):
    def __init__(self, pool: KeyPool, gnani: GnaniConfig, audio: AudioConfig) -> None:
        self._pool = pool
        self._gnani = gnani
        self._audio = audio
        self._paused = asyncio.Event()  # set == paused

    # -- pause/resume -----------------------------------------------------
    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    # -- public -----------------------------------------------------------
    async def stream(self, source: AudioSource) -> AsyncIterator[TranscriptSegment]:
        frames = source.frames()
        fruitful = 0

        while True:
            produced = False
            try:
                async for segment in self._session(frames):
                    produced = True
                    yield segment
                return
            except _Reconnect:
                fruitful = 0 if produced else fruitful + 1
                if fruitful >= MAX_FRUITLESS_RECONNECTS:
                    raise RuntimeError(
                        "Prisma reconnected repeatedly without producing a transcript; "
                        "check the API key, quota and audio format"
                    )
                log.info("reconnecting Prisma (fruitless=%d)", fruitful)
                await asyncio.sleep(min(2.0 * fruitful, 6.0))

    # -- one websocket session -------------------------------------------
    async def _session(self, frames: AsyncIterator[bytes]) -> AsyncIterator[TranscriptSegment]:
        key = await self._pool.acquire()

        try:
            ws = await websockets.connect(
                self._gnani.stt_url,
                additional_headers=self._headers(key),
                max_size=None,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            )
        except AllKeysExhausted:
            raise
        except Exception as exc:  # noqa: BLE001 - classify and rotate
            status = _handshake_status(exc)
            self._pool.report_failure(key, status=status)
            log.warning("Prisma handshake failed (status=%s): %s", status, exc)
            raise _Reconnect() from exc

        self._pool.report_success(key)
        started = time.monotonic()
        out: asyncio.Queue[TranscriptSegment | None] = asyncio.Queue()
        errors: list[BaseException] = []
        silence_sent = 0.0

        async def sender() -> None:
            try:
                async for frame in frames:
                    if self._paused.is_set():
                        # Drop audio rather than send it: this is what stops the
                        # spoken warning from being transcribed back into the call.
                        continue
                    await ws.send(frame)
                    if time.monotonic() - started > self._gnani.session_cap_s - ROLLOVER_MARGIN_S:
                        raise _Reconnect("session rollover")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                out.put_nowait(None)

        async def receiver() -> None:
            try:
                async for raw in ws:
                    segment = self._parse(raw)
                    if segment is not None:
                        await out.put(segment)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                out.put_nowait(None)

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        done = 0

        try:
            while done < 2:
                item = await out.get()
                if item is None:
                    done += 1
                else:
                    yield item
        finally:
            for task in (send_task, recv_task):
                task.cancel()
            await asyncio.gather(send_task, recv_task, return_exceptions=True)
            await ws.close()
            _ = silence_sent

        for exc in errors:
            if isinstance(exc, _Reconnect):
                raise exc
            if isinstance(exc, AllKeysExhausted):
                raise exc
            if isinstance(exc, websockets.exceptions.ConnectionClosed):
                # Normal close at the end of a call, or the 15-minute cap.
                log.info("Prisma closed: %s", exc)
                raise _Reconnect() from exc
            if isinstance(exc, asyncio.CancelledError):
                continue
            log.warning("Prisma stream error: %s", exc)
            self._pool.report_failure(key)
            raise _Reconnect() from exc

    # -- helpers ----------------------------------------------------------
    def _headers(self, key: str) -> dict[str, str]:
        g = self._gnani
        return {
            "x-api-key-id": key,
            "model": g.stt_model,
            "language_code": g.stt_language,
            "format": g.stt_format,
            "x-sample-rate": str(self._audio.sample_rate),
            "x-vad-threshold": g.vad_threshold,
            "x-min-silence-ms": g.min_silence_ms,
            "x-min-speech-ms": g.min_speech_ms,
            "x-min-segment-ms": g.min_segment_ms,
            "x-max-speech-s": g.max_speech_s,
            "x-idle-timeout-sec": str(g.idle_timeout_s),
        }

    def _parse(self, raw: str | bytes) -> TranscriptSegment | None:
        try:
            msg: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

        kind = msg.get("type")
        if kind == "error":
            log.warning("Prisma error message: %s", msg.get("message"))
            return None
        if kind != "transcript":
            return None

        text = (msg.get("text") or "").strip()
        if not text:
            return None

        return TranscriptSegment(
            text=text,
            index=int(msg.get("segment_index") or 0),
            audio_duration_ms=int(msg.get("audio_duration_ms") or 0),
            latency_ms=int(msg.get("latency") or 0),
            language=msg.get("detected_language_code"),
        )


def _handshake_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status from a failed websocket handshake."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    # Older websockets versions expose it differently.
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None
