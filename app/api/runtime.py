"""Shared runtime: one place where long-lived objects are built.

Keeping construction here (rather than in route handlers) means tests can build
a runtime with fakes, and the API layer stays free of business logic.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.alerts.notify import ConsoleNotifier, NotifierHub
from app.alerts.tts import TimbreTTS
from app.audio_sources.base import PushAudioSource
from app.audio_sources.file_source import FileAudioSource
from app.config import AppConfig
from app.detector.llm import LlmDetector
from app.keypool import KeyPool
from app.pipeline import CallSession, EventBus
from app.storage.db import Store
from app.stt.prisma import PrismaSTT

log = logging.getLogger(__name__)

DEMO_AUDIO_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "audio" / "demo"
UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "uploads"


@dataclass
class Runtime:
    config: AppConfig
    store: Store
    bus: EventBus
    gnani_pool: KeyPool | None
    groq_pool: KeyPool | None
    llm: LlmDetector | None
    tts: TimbreTTS | None
    notifier: NotifierHub
    sessions: dict[str, CallSession] = field(default_factory=dict)
    push_sources: dict[str, PushAudioSource] = field(default_factory=dict)

    # -- readiness --------------------------------------------------------
    @property
    def stt_ready(self) -> bool:
        return self.gnani_pool is not None

    @property
    def llm_ready(self) -> bool:
        return self.llm is not None

    def require_stt(self) -> None:
        if not self.stt_ready:
            raise RuntimeError("GNANI_API_KEYS is not set in .env — cannot start a call")

    # -- calls ------------------------------------------------------------
    def start_file_call(self, path: Path, *, enable_tts: bool = True) -> CallSession:
        self.require_stt()
        assert self.gnani_pool is not None
        source = FileAudioSource(path)
        session = CallSession(
            source=source,
            stt=PrismaSTT(self.gnani_pool, self.config.gnani, self.config.audio),
            llm=self.llm or _NullLlm(),  # type: ignore[arg-type]
            store=self.store,
            bus=self.bus,
            tts=self.tts,
            notifier=self.notifier,
            config=self.config,
            enable_tts=enable_tts,
        )
        self.sessions[session.call_id] = session
        session.start()
        return session

    def start_push_call(self, *, name: str = "browser-mic") -> tuple[CallSession, PushAudioSource]:
        self.require_stt()
        assert self.gnani_pool is not None
        source = PushAudioSource(name)
        session = CallSession(
            source=source,
            stt=PrismaSTT(self.gnani_pool, self.config.gnani, self.config.audio),
            llm=self.llm or _NullLlm(),  # type: ignore[arg-type]
            store=self.store,
            bus=self.bus,
            tts=self.tts,
            notifier=self.notifier,
            config=self.config,
            enable_tts=True,
        )
        self.sessions[session.call_id] = session
        self.push_sources[session.call_id] = source
        session.start()
        return session, source

    async def stop_call(self, call_id: str) -> bool:
        session = self.sessions.get(call_id)
        if session is None:
            return False
        await session.stop()
        self.push_sources.pop(call_id, None)
        return True

    async def shutdown(self) -> None:
        for call_id in list(self.sessions):
            await self.stop_call(call_id)
        if self.llm is not None:
            await self.llm.aclose()
        if self.tts is not None:
            await self.tts.aclose()
        self.store.close()


class _NullLlm:
    """Stand-in when no Groq key is configured: rules-only detection."""

    async def assess(self, window_text: str):  # noqa: ANN201
        from app.detector.models import LlmReading

        return LlmReading(ok=False, error="no Groq key configured")

    async def aclose(self) -> None:
        return None


def build_runtime(config: AppConfig) -> Runtime:
    store = Store(config.db_path)
    bus = EventBus()

    gnani_pool = KeyPool(config.gnani.api_keys, "gnani") if config.gnani.api_keys else None
    groq_pool = KeyPool(config.groq.api_keys, "groq") if config.groq.api_keys else None

    llm = LlmDetector(groq_pool, config.groq) if groq_pool is not None else None
    tts = TimbreTTS(gnani_pool, config.gnani) if gnani_pool is not None else None

    notifier = NotifierHub([ConsoleNotifier()])

    if gnani_pool is None:
        log.warning("GNANI_API_KEYS missing — STT and TTS are unavailable")
    if groq_pool is None:
        log.warning("GROQ_API_KEYS missing — detector will run rules-only")

    return Runtime(
        config=config,
        store=store,
        bus=bus,
        gnani_pool=gnani_pool,
        groq_pool=groq_pool,
        llm=llm,
        tts=tts,
        notifier=notifier,
    )
