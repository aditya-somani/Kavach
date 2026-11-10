"""Pipeline test: a fake STT drives the real CallSession end to end.

No network, no API keys. This is the test that catches ordering bugs in the
alert path, which is the part a live demo cannot afford to get wrong.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.alerts.notify import CollectingNotifier, NotifierHub
from app.audio_sources.base import AudioSource
from app.config import AppConfig, DetectorConfig
from app.detector.models import LlmReading, Signal, Signals
from app.pipeline import CallSession, EventBus
from app.storage.db import Store
from app.stt.base import STTClient, TranscriptSegment

SCAM_LINES = [
    "मैं CBI से इंस्पेक्टर बोल रहा हूँ।",
    "आपके नाम पर ड्रग्स का parcel मिला है और गिरफ्तारी वारंट है।",
    "किसी को मत बताओ, लाइन पर रहो।",
    "अभी पचास हज़ार रुपये transfer करो।",
]


class FakeSTT(STTClient):
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def stream(self, source: AudioSource) -> AsyncIterator[TranscriptSegment]:
        for index, line in enumerate(self._lines, start=1):
            await asyncio.sleep(0.01)
            yield TranscriptSegment(text=line, index=index, audio_duration_ms=1200, latency_ms=90)


class FakeLlm:
    def __init__(self) -> None:
        self.calls = 0

    async def assess(self, window_text: str) -> LlmReading:
        self.calls += 1
        if "CBI" in window_text:
            return LlmReading(
                signals=Signals(frozenset({Signal.AUTHORITY, Signal.FEAR, Signal.ISOLATION, Signal.MONEY_REQUEST})),
                risk=0.93,
                reason_hi="सरकारी अधिकारी बनकर डराया जा रहा है।",
                reason_en="Caller impersonates CBI and demands a transfer.",
            )
        return LlmReading(signals=Signals(), risk=0.05, reason_en="Looks legitimate.")

    async def aclose(self) -> None:
        return None


class EmptySource(AudioSource):
    name = "test"

    async def frames(self) -> AsyncIterator[bytes]:
        if False:  # pragma: no cover
            yield b""


@pytest.mark.asyncio
async def test_scam_call_alerts_once_and_persists(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    bus = EventBus()
    queue = bus.subscribe()
    notifier = CollectingNotifier()
    config = AppConfig(detector=DetectorConfig(llm_interval_s=0.0))

    session = CallSession(
        source=EmptySource(),
        stt=FakeSTT(SCAM_LINES),
        llm=FakeLlm(),  # type: ignore[arg-type]
        store=store,
        bus=bus,
        tts=None,
        notifier=NotifierHub([notifier]),
        config=config,
        enable_tts=False,
    )

    session.start()
    await session.wait()
    await asyncio.sleep(0.05)

    events = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)

    alerts = [e for e in events if e["kind"] == "alert"]
    assert len(alerts) == 1, "an alert must fire exactly once per call"
    assert len(notifier.alerts) == 1

    segments = [e for e in events if e["kind"] == "segment"]
    assert len(segments) == len(SCAM_LINES)

    call = await store.get_call(session.call_id)
    assert call is not None
    assert call["alerted"] is True
    assert len(call["segments"]) == len(SCAM_LINES)
    store.close()


@pytest.mark.asyncio
async def test_legitimate_call_stays_quiet(tmp_path: Path) -> None:
    store = Store(tmp_path / "quiet.db")
    bus = EventBus()
    notifier = CollectingNotifier()
    config = AppConfig(detector=DetectorConfig(llm_interval_s=0.0))

    session = CallSession(
        source=EmptySource(),
        stt=FakeSTT(["नमस्ते, आपका parcel आया है।", "OTP बता दीजिए ताकि मैं parcel दे सकूँ।"]),
        llm=FakeLlm(),  # type: ignore[arg-type]
        store=store,
        bus=bus,
        tts=None,
        notifier=NotifierHub([notifier]),
        config=config,
        enable_tts=False,
    )

    session.start()
    await session.wait()

    assert notifier.alerts == []
    store.close()
