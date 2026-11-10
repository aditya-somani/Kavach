"""Pre-recorded audio as an audio source.

Used for development, the evaluation harness, and the demo fallback. Audio is
16 kHz mono WAV; anything else is converted on load with numpy so the machine
never needs ffmpeg.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import soundfile as sf

from app.audio_sources.base import AudioSource, pcm16_frames

TARGET_RATE = 16_000
# Prisma finalises an utterance only after a pause. A trailing second of
# silence makes the last sentence of a file actually come back as a transcript.
TRAILING_SILENCE_S = 1.0


def load_pcm16(path: str | Path) -> np.ndarray:
    """Load an audio file as mono int16 samples at 16 kHz."""
    data, rate = sf.read(str(path), dtype="int16", always_2d=True)
    if data.size == 0:
        return np.zeros(0, dtype=np.int16)

    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = np.asarray(mono, dtype=np.float64)

    if rate != TARGET_RATE:
        duration = mono.size / rate
        target_len = int(round(duration * TARGET_RATE))
        if target_len <= 0:
            return np.zeros(0, dtype=np.int16)
        src_idx = np.linspace(0.0, mono.size - 1, target_len)
        mono = np.interp(src_idx, np.arange(mono.size), mono)

    return np.clip(mono, -32768, 32767).astype(np.int16)


class FileAudioSource(AudioSource):
    """Replay a file at real-time cadence (or a multiple of it, for eval runs)."""

    def __init__(
        self,
        path: str | Path,
        *,
        speed: float = 1.0,
        trailing_silence_s: float = TRAILING_SILENCE_S,
        name: str | None = None,
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.path = Path(path)
        self.speed = speed
        self.trailing_silence_s = trailing_silence_s
        self.name = name or self.path.stem

    async def frames(self) -> AsyncIterator[bytes]:
        samples = load_pcm16(self.path)
        if self.trailing_silence_s > 0:
            pad = np.zeros(int(TARGET_RATE * self.trailing_silence_s), dtype=np.int16)
            samples = np.concatenate([samples, pad])

        delay = (512 / TARGET_RATE) / self.speed
        for frame in pcm16_frames(samples):
            yield frame
            await asyncio.sleep(delay)
