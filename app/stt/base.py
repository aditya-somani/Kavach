"""STT interface: a paced audio source in, transcript segments out."""

from __future__ import annotations

import abc
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.audio_sources.base import AudioSource


@dataclass(frozen=True)
class TranscriptSegment:
    """One final transcript segment as returned by Prisma."""

    text: str
    index: int
    audio_duration_ms: int
    latency_ms: int
    received_at: float = field(default_factory=time.monotonic)
    language: str | None = None

    @property
    def word_count(self) -> int:
        return len(self.text.split())


class STTClient(abc.ABC):
    """Anything that turns a paced audio source into transcript segments."""

    @abc.abstractmethod
    def stream(self, source: AudioSource) -> AsyncIterator[TranscriptSegment]:
        """Yield transcript segments as the audio is consumed."""

    def pause(self) -> None:
        """Stop forwarding audio (used while a spoken warning plays)."""

    def resume(self) -> None:
        """Resume forwarding audio."""
