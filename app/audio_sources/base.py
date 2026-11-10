"""Swappable audio source interface.

Every source yields raw PCM s16le mono frames of exactly 1024 bytes at 16 kHz,
already paced at real time. The rest of the system cannot tell a file from a
microphone from a LiveKit room.

Pacing is the source's job, not the STT client's: a file source sleeps between
frames, a microphone source arrives at real time naturally, and a LiveKit track
is paced by the media stack.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator

import numpy as np

FRAME_BYTES = 1024
SAMPLES_PER_FRAME = 512


class AudioSource(abc.ABC):
    """A paced stream of 1024-byte PCM frames."""

    name: str = "audio"

    @abc.abstractmethod
    def frames(self) -> AsyncIterator[bytes]:
        """Yield 1024-byte PCM s16le mono frames until the source is exhausted."""

    async def close(self) -> None:
        """Release any underlying resource. Safe to call twice."""
        return None


class PushAudioSource(AudioSource):
    """A source fed from outside (a WebSocket, a LiveKit track, a test).

    The producer pushes raw PCM chunks; consumers read fixed 1024-byte frames.
    """

    def __init__(self, name: str = "push") -> None:
        self.name = name
        import asyncio

        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        self._buffer = bytearray()
        self._closed = False

    async def push(self, chunk: bytes) -> None:
        if self._closed:
            return
        await self._queue.put(chunk)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._queue.put(None)

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                # Flush a partial frame rather than dropping the tail.
                if self._buffer:
                    padded = bytes(self._buffer) + bytes(FRAME_BYTES - len(self._buffer))
                    self._buffer.clear()
                    yield padded
                return
            self._buffer.extend(chunk)
            while len(self._buffer) >= FRAME_BYTES:
                yield bytes(self._buffer[:FRAME_BYTES])
                del self._buffer[:FRAME_BYTES]


def pcm16_frames(samples: np.ndarray) -> list[bytes]:
    """Split a mono int16 array into 1024-byte frames, zero-padding the tail."""
    flat = np.ascontiguousarray(samples, dtype=np.int16).reshape(-1)
    remainder = flat.size % SAMPLES_PER_FRAME
    if remainder:
        flat = np.concatenate([flat, np.zeros(SAMPLES_PER_FRAME - remainder, dtype=np.int16)])
    return [flat[i : i + SAMPLES_PER_FRAME].tobytes() for i in range(0, flat.size, SAMPLES_PER_FRAME)]
