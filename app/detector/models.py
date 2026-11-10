"""Shared detector data types.

Deliberately dependency-free so the rules, the LLM layer and the scorer can all
be tested without a network or an event loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Signal(str, Enum):
    AUTHORITY = "authority"
    FEAR = "fear"
    URGENCY = "urgency"
    ISOLATION = "isolation"
    SENSITIVE_REQUEST = "sensitive_request"
    MONEY_REQUEST = "money_request"


SIGNAL_LABELS_HI: dict[Signal, str] = {
    Signal.AUTHORITY: "सरकारी अधिकारी होने का दावा",
    Signal.FEAR: "डर या कानूनी धमकी",
    Signal.URGENCY: "तुरंत करने का दबाव",
    Signal.ISOLATION: "किसी को न बताने को कहना",
    Signal.SENSITIVE_REQUEST: "ओटीपी / आधार / ऐप मांगना",
    Signal.MONEY_REQUEST: "पैसे भेजने की मांग",
}

SIGNAL_LABELS_EN: dict[Signal, str] = {
    Signal.AUTHORITY: "Claims authority",
    Signal.FEAR: "Fear / legal threat",
    Signal.URGENCY: "Urgency pressure",
    Signal.ISOLATION: "Asks for secrecy",
    Signal.SENSITIVE_REQUEST: "Asks for OTP / ID / app",
    Signal.MONEY_REQUEST: "Demands money",
}

ALL_SIGNALS: tuple[Signal, ...] = tuple(Signal)


@dataclass(frozen=True)
class Signals:
    """The set of signals currently believed to be present."""

    active: frozenset[Signal] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.active)

    def __len__(self) -> int:
        return len(self.active)

    def __contains__(self, item: Signal) -> bool:
        return item in self.active

    @property
    def names(self) -> list[str]:
        return [s.value for s in ALL_SIGNALS if s in self.active]

    def merge(self, other: "Signals") -> "Signals":
        return Signals(self.active | other.active)

    @classmethod
    def from_mapping(cls, data: Any) -> "Signals":
        """Build from a loose dict, ignoring unknown keys and non-true values."""
        if not isinstance(data, dict):
            return cls()
        found: set[Signal] = set()
        for signal in ALL_SIGNALS:
            if data.get(signal.value) is True:
                found.add(signal)
        return cls(frozenset(found))


@dataclass(frozen=True)
class LlmReading:
    """One LLM assessment of a transcript window."""

    signals: Signals = Signals()
    risk: float = 0.0
    reason_hi: str = ""
    reason_en: str = ""
    model: str = ""
    latency_ms: int = 0
    ok: bool = True
    error: str = ""


@dataclass
class RiskState:
    """The smoothed, user-facing state of the detector."""

    score: float = 0.0
    signals: Signals = Signals()
    reason_hi: str = ""
    reason_en: str = ""
    alerted: bool = False
    consecutive_high: int = 0
    consecutive_low: int = 0
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def level(self) -> str:
        if self.alerted:
            return "alert"
        if self.score >= 0.45:
            return "elevated"
        return "calm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "level": self.level,
            "alerted": self.alerted,
            "signals": {s.value: (s in self.signals) for s in ALL_SIGNALS},
            "signal_names": self.signals.names,
            "reason_hi": self.reason_hi,
            "reason_en": self.reason_en,
            "updated_at": self.updated_at,
        }
