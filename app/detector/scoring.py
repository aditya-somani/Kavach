"""Layer 3 — combine, smooth, and decide.

This module is pure logic: no network, no I/O, no clock except an injectable
``now``. That makes the alerting behaviour unit-testable, which matters because
hysteresis bugs are exactly the kind that ruin a live demo.

Decision shape:

* rules give a floor, the LLM gives an opinion
* the two are combined, then smoothed so one odd reading cannot spike the meter
* an alert needs the smoothed score above threshold **and** at least two
  distinct signals **and** two consecutive high readings
* once alerting, it stays alerting until the score is clearly low again
"""

from __future__ import annotations

import time

from app.config import DetectorConfig
from app.detector.models import (
    SIGNAL_LABELS_EN,
    SIGNAL_LABELS_HI,
    ALL_SIGNALS,
    LlmReading,
    RiskState,
    Signals,
)
from app.detector.rules import RuleResult

# Discount applied to the rules floor when the LLM is unavailable, so a rules-only
# call is a little less trigger-happy.
RULES_ONLY_DISCOUNT = 0.85
# Bonus when the two layers independently agree that this looks bad.
AGREEMENT_BONUS = 0.08
# Readings below the clear threshold needed before dropping an alert.
CLEAR_READINGS = 2


class RiskScorer:
    def __init__(self, cfg: DetectorConfig) -> None:
        self._cfg = cfg
        self._state = RiskState()

    @property
    def state(self) -> RiskState:
        return self._state

    def reset(self) -> None:
        self._state = RiskState()

    def update(
        self,
        rules: RuleResult,
        llm: LlmReading | None = None,
        now: float | None = None,
    ) -> RiskState:
        cfg = self._cfg
        previous = self._state.score
        llm_ok = llm is not None and llm.ok

        signals = rules.signals.merge(llm.signals) if llm_ok else rules.signals

        if llm_ok:
            raw = max(rules.floor, llm.risk)
            if rules.floor >= 0.5 and llm.risk >= 0.5:
                raw = min(1.0, raw + AGREEMENT_BONUS)
        else:
            raw = rules.floor * RULES_ONLY_DISCOUNT

        if previous == 0.0:
            smoothed = raw
        else:
            smoothed = cfg.smoothing_alpha * raw + (1 - cfg.smoothing_alpha) * previous
        smoothed = max(0.0, min(1.0, smoothed))

        high = smoothed >= cfg.alert_threshold and len(signals) >= cfg.min_signals_for_alert
        consecutive_high = self._state.consecutive_high + 1 if high else 0

        alerted = self._state.alerted
        consecutive_low = self._state.consecutive_low
        if alerted:
            if smoothed < cfg.clear_threshold:
                consecutive_low += 1
                if consecutive_low >= CLEAR_READINGS:
                    alerted = False
                    consecutive_low = 0
            else:
                consecutive_low = 0
        elif consecutive_high >= cfg.consecutive_high:
            alerted = True
            consecutive_low = 0

        reason_hi, reason_en = self._reason(rules, llm, signals)

        self._state = RiskState(
            score=smoothed,
            signals=signals,
            reason_hi=reason_hi,
            reason_en=reason_en,
            alerted=alerted,
            consecutive_high=consecutive_high,
            consecutive_low=consecutive_low,
            updated_at=now if now is not None else time.monotonic(),
        )
        return self._state

    @staticmethod
    def _reason(
        rules: RuleResult,
        llm: LlmReading | None,
        signals: Signals,
    ) -> tuple[str, str]:
        if llm is not None and llm.ok and (llm.reason_hi or llm.reason_en):
            return llm.reason_hi, llm.reason_en

        ordered = [s for s in ALL_SIGNALS if s in signals]
        if not ordered:
            return "अभी तक कोई खतरे का संकेत नहीं मिला।", "No risk signals yet."
        hi = " + ".join(SIGNAL_LABELS_HI[s] for s in ordered[:3])
        en = " + ".join(SIGNAL_LABELS_EN[s] for s in ordered[:3])
        return hi, en
