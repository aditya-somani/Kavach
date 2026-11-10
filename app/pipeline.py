"""The call pipeline.

One ``CallSession`` per call. It owns the ordering that makes the demo feel
alive:

* every transcript segment updates the rules floor *immediately*, so the risk
  meter moves without waiting for the network;
* the LLM runs in a background task on a fixed cadence and never blocks audio;
* an alert is emitted once per call, and while the spoken warning plays the STT
  client is paused so the warning is not transcribed back into the call.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from app.alerts.notify import AlertPayload, NotifierHub
from app.alerts.phrases import WARNING_HI, WARNING_HI_REPEAT
from app.alerts.tts import TimbreTTS
from app.audio_sources.base import AudioSource
from app.config import AppConfig
from app.detector.llm import LlmDetector
from app.detector.models import LlmReading, RiskState
from app.detector.rules import evaluate
from app.detector.scoring import RiskScorer
from app.detector.window import TranscriptWindow
from app.stt.base import STTClient, TranscriptSegment
from app.storage.db import Store

log = logging.getLogger(__name__)

# Extra pause after the warning finishes, to cover browser playback jitter.
RESUME_MARGIN_S = 0.4


class EventBus:
    """In-process pub/sub for the live UI (SSE) and the family page."""

    def __init__(self, queue_size: int = 512) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest event rather than stall the call.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


class CallSession:
    def __init__(
        self,
        *,
        source: AudioSource,
        stt: STTClient,
        llm: LlmDetector,
        store: Store,
        bus: EventBus,
        tts: TimbreTTS | None,
        notifier: NotifierHub,
        config: AppConfig,
        enable_tts: bool = True,
        call_id: str | None = None,
    ) -> None:
        self.call_id = call_id or f"call_{uuid.uuid4().hex[:12]}"
        self._source = source
        self._stt = stt
        self._llm = llm
        self._store = store
        self._bus = bus
        self._tts = tts
        self._notifier = notifier
        self._config = config
        self._enable_tts = enable_tts and tts is not None

        self._window = TranscriptWindow(window_s=config.detector.window_s)
        self._scorer = RiskScorer(config.detector)
        self._last_llm: LlmReading | None = None
        self._llm_task: asyncio.Task[None] | None = None
        self._last_llm_at = 0.0
        self._alerted_once = False
        self._alerts_spoken = 0
        self._alert_task: asyncio.Task[None] | None = None
        self._max_risk = 0.0
        self.started_at = 0.0
        self._reasons: list[str] = []
        self._segments: list[str] = []
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"call:{self.call_id}")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._source.close()

    @property
    def running(self) -> bool:
        return self._running

    async def wait(self) -> None:
        """Block until the call finishes. Used by the eval harness."""
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    # -- main loop --------------------------------------------------------
    async def _run(self) -> None:
        self._running = True
        self.started_at = time.time()
        await self._store.create_call(self.call_id, self._source.name)
        await self._publish("status", {"state": "running", "source": self._source.name})

        try:
            async for segment in self._stt.stream(self._source):
                await self._on_segment(segment)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead call must not kill the server
            log.exception("call %s failed", self.call_id)
            await self._publish("status", {"state": "error", "message": str(exc)})
        finally:
            self._running = False
            await self._cancel_llm()
            if self._alert_task is not None:
                await asyncio.gather(self._alert_task, return_exceptions=True)
            summary = self._summary()
            await self._store.finish_call(
                self.call_id, self._max_risk, self._alerted_once, summary, self._reasons
            )
            await self._publish(
                "status",
                {
                    "state": "ended",
                    "call_id": self.call_id,
                    "max_risk": round(self._max_risk, 3),
                    "alerted": self._alerted_once,
                    "summary": summary,
                },
            )

    async def _on_segment(self, segment: TranscriptSegment) -> None:
        self._window.add(segment.text, segment.received_at)
        self._segments.append(segment.text)
        await self._store.add_segment(
            self.call_id, segment.index, segment.text, segment.latency_ms
        )
        await self._publish(
            "segment",
            {
                "text": segment.text,
                "index": segment.index,
                "latency_ms": segment.latency_ms,
                "language": segment.language,
            },
        )

        # Rules run on every segment: instant meter movement, no network wait.
        await self._apply(evaluate(self._window.text()))
        self._maybe_schedule_llm()

    # -- scoring ----------------------------------------------------------
    async def _apply(self, rules: Any) -> RiskState:
        state = self._scorer.update(rules, self._last_llm)
        self._max_risk = max(self._max_risk, state.score)
        if state.reason_en and state.reason_en not in self._reasons:
            self._reasons.append(state.reason_en)

        await self._publish("risk", state.to_dict())
        if state.alerted and not self._alerted_once:
            self._alerted_once = True
            self._alert_task = asyncio.create_task(
                self._raise_alert(state), name="alert"
            )
        return state

    def _maybe_schedule_llm(self) -> None:
        now = time.monotonic()
        if self._llm_task is not None and not self._llm_task.done():
            return
        if now - self._last_llm_at < self._config.detector.llm_interval_s:
            return
        self._last_llm_at = now
        self._llm_task = asyncio.create_task(self._score_with_llm(), name="llm-score")

    async def _score_with_llm(self) -> None:
        window_text = self._window.text()
        if not window_text:
            return
        reading = await self._llm.assess(window_text)
        self._last_llm = reading
        if reading.ok:
            await self._publish(
                "llm",
                {
                    "risk": round(reading.risk, 3),
                    "signals": reading.signals.names,
                    "reason_hi": reading.reason_hi,
                    "reason_en": reading.reason_en,
                    "model": reading.model,
                    "latency_ms": reading.latency_ms,
                },
            )
        else:
            log.debug("LLM unavailable: %s", reading.error)
        await self._apply(evaluate(self._window.text()))

    async def _cancel_llm(self) -> None:
        if self._llm_task is not None and not self._llm_task.done():
            self._llm_task.cancel()
            await asyncio.gather(self._llm_task, return_exceptions=True)

    # -- alerts -----------------------------------------------------------
    async def _raise_alert(self, state: RiskState) -> None:
        await self._store.add_event(
            self.call_id, "alert", state.score, state.signals.names, state.reason_en
        )
        await self._publish(
            "alert",
            {
                "call_id": self.call_id,
                "score": round(state.score, 3),
                "signals": state.signals.names,
                "reason_hi": state.reason_hi,
                "reason_en": state.reason_en,
                "warning_hi": WARNING_HI,
            },
        )
        await self._notifier.notify(
            AlertPayload(
                call_id=self.call_id,
                score=state.score,
                signals=state.signals.names,
                reason_hi=state.reason_hi,
                reason_en=state.reason_en,
                summary=self._summary(),
            )
        )
        if self._enable_tts:
            asyncio.create_task(self._speak_warning(), name="tts-warning")

    async def _speak_warning(self) -> None:
        assert self._tts is not None
        text = WARNING_HI if self._alerts_spoken == 0 else WARNING_HI_REPEAT
        self._alerts_spoken += 1
        self._stt.pause()
        try:
            audio = await self._tts.synthesize(text)
        except Exception as exc:  # noqa: BLE001 - a TTS failure must not end the call
            log.warning("TTS failed, continuing without spoken warning: %s", exc)
            self._stt.resume()
            return

        path = self._config.db_path.parent / "audio" / "warnings" / f"{self.call_id}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        await self._publish(
            "warning",
            {"url": f"/audio/warnings/{self.call_id}.wav", "text": text},
        )
        await self._store.add_event(self.call_id, "warning_spoken", 0.0, [], text)

        # Keep STT paused for the length of the clip so the warning is not
        # captured by the microphone and fed back in.
        await asyncio.sleep(_wav_duration_s(audio) + RESUME_MARGIN_S)
        self._stt.resume()

    # -- helpers ----------------------------------------------------------
    def _summary(self) -> str:
        head = " ".join(self._segments[:3]).strip()
        if len(head) > 180:
            head = head[:177] + "..."
        return head

    async def _publish(self, kind: str, data: dict[str, Any]) -> None:
        await self._bus.publish(
            {"kind": kind, "call_id": self.call_id, "at": time.time(), "data": data}
        )


def _wav_duration_s(wav_bytes: bytes) -> float:
    """Duration of a 16 kHz mono 16-bit WAV without importing a decoder."""
    data_bytes = max(0, len(wav_bytes) - 44)
    return data_bytes / (16_000 * 2)
