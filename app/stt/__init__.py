"""STT clients."""

from __future__ import annotations

from app.stt.base import STTClient, TranscriptSegment
from app.stt.prisma import PrismaSTT

__all__ = ["STTClient", "TranscriptSegment", "PrismaSTT"]
