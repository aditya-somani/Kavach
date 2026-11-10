"""The scam detector: rules, LLM, and the scorer that combines them."""

from __future__ import annotations

from app.detector.llm import LlmDetector
from app.detector.models import (
    ALL_SIGNALS,
    SIGNAL_LABELS_EN,
    SIGNAL_LABELS_HI,
    LlmReading,
    RiskState,
    Signal,
    Signals,
)
from app.detector.rules import RuleResult, evaluate
from app.detector.scoring import RiskScorer
from app.detector.window import TranscriptWindow

__all__ = [
    "ALL_SIGNALS",
    "SIGNAL_LABELS_EN",
    "SIGNAL_LABELS_HI",
    "LlmDetector",
    "LlmReading",
    "RiskScorer",
    "RiskState",
    "RuleResult",
    "Signal",
    "Signals",
    "TranscriptWindow",
    "evaluate",
]
