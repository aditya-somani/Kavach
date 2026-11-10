"""Central configuration.

Every tunable lives here. Modules import ``settings`` and never read the
environment directly, so there is exactly one place to look when something
is misconfigured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _csv(name: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class AudioConfig:
    """Prisma streaming format, verified against the Gnani docs.

    Frames must be raw PCM s16le mono, exactly 1024 bytes (512 samples),
    sent at real-time cadence.
    """

    sample_rate: int = 16_000
    channels: int = 1
    bytes_per_frame: int = 1_024
    samples_per_frame: int = 512
    bytes_per_sample: int = 2

    @property
    def frame_ms(self) -> int:
        """Frame duration in ms (32 ms at 16 kHz)."""
        return round(self.samples_per_frame * 1000 / self.sample_rate)

    @property
    def frame_seconds(self) -> float:
        return self.samples_per_frame / self.sample_rate


@dataclass(frozen=True)
class GnaniConfig:
    api_keys: list[str] = field(default_factory=lambda: _csv("GNANI_API_KEYS"))
    stt_url: str = _str("GNANI_STT_URL", "wss://api.vachana.ai/stt/v3/stream")
    tts_url: str = _str("GNANI_TTS_URL", "https://api.vachana.ai/api/v1/tts/inference")
    stt_model: str = _str("GNANI_STT_MODEL", "prisma-v3.0")
    stt_language: str = _str("GNANI_STT_LANGUAGE", "hi-IN,en-IN")
    stt_format: str = _str("GNANI_STT_FORMAT", "transcribe")
    tts_model: str = _str("GNANI_TTS_MODEL", "timbre-v3.0")
    tts_voice: str = _str("GNANI_TTS_VOICE", "Shivani")
    tts_language: str = _str("GNANI_TTS_LANGUAGE", "hi-IN")

    # VAD tuning for a noisy telephony line. Fixed for the life of a session.
    vad_threshold: str = "0.7"
    min_silence_ms: str = "500"
    min_speech_ms: str = "200"
    min_segment_ms: str = "150"
    max_speech_s: str = "20"

    # Server-side hard limits we must respect.
    session_cap_s: int = 15 * 60
    idle_timeout_s: int = 120


@dataclass(frozen=True)
class GroqConfig:
    api_keys: list[str] = field(default_factory=lambda: _csv("GROQ_API_KEYS"))
    base_url: str = "https://api.groq.com/openai/v1"
    model: str = _str("GROQ_MODEL", "llama-3.3-70b-versatile")
    fallback_model: str = _str("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")
    timeout_s: float = 12.0
    temperature: float = 0.0
    max_tokens: int = 400
    max_attempts: int = 3


@dataclass(frozen=True)
class DetectorConfig:
    """Layer 3 knobs: window, cadence, and hysteresis."""

    window_s: float = 30.0
    llm_interval_s: float = 3.5
    alert_threshold: float = 0.75
    clear_threshold: float = 0.45
    consecutive_high: int = 2
    # Smoothing: new score = alpha * raw + (1 - alpha) * previous.
    smoothing_alpha: float = 0.6
    # A hard signal alone cannot alert; it needs corroboration.
    min_signals_for_alert: int = 2


@dataclass(frozen=True)
class AppConfig:
    host: str = _str("APP_HOST", "127.0.0.1")
    port: int = _int("APP_PORT", 8000)
    db_path: Path = ROOT / _str("DB_PATH", "data/kavach.db")
    audio: AudioConfig = field(default_factory=AudioConfig)
    gnani: GnaniConfig = field(default_factory=GnaniConfig)
    groq: GroqConfig = field(default_factory=GroqConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)

    # LiveKit is optional; the app runs fully without it.
    livekit_url: str = _str("LIVEKIT_URL", "")
    livekit_api_key: str = _str("LIVEKIT_API_KEY", "")
    livekit_api_secret: str = _str("LIVEKIT_API_SECRET", "")

    @property
    def livekit_configured(self) -> bool:
        return bool(self.livekit_url and self.livekit_api_key and self.livekit_api_secret)


settings = AppConfig()
