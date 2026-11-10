"""Rolling transcript window used by the LLM layer.

Prisma returns final segments only, so a window is simply the last N seconds of
concatenated text. Keeping the timestamps lets us ask "is the LLM due yet?"
without a second timer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Line:
    text: str
    at: float


@dataclass
class TranscriptWindow:
    window_s: float = 30.0
    _lines: list[_Line] = field(default_factory=list)

    def add(self, text: str, at: float | None = None) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self._lines.append(_Line(cleaned, at if at is not None else time.monotonic()))

    def prune(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.monotonic()) - self.window_s
        self._lines = [line for line in self._lines if line.at >= cutoff]

    def text(self, now: float | None = None) -> str:
        self.prune(now)
        return " ".join(line.text for line in self._lines)

    def clear(self) -> None:
        self._lines.clear()

    def __len__(self) -> int:
        return len(self._lines)
