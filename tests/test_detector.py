"""Detector tests: the rules floor and the hysteresis that protects the demo."""

from __future__ import annotations

from app.config import DetectorConfig
from app.detector.models import LlmReading, Signal, Signals
from app.detector.rules import evaluate
from app.detector.scoring import RiskScorer

SCAM = (
    "मैं CBI से बोल रहा हूँ। आपके नाम पर ड्रग्स का parcel मिला है। "
    "आप गिरफ्तार हो सकते हैं। किसी को मत बताओ, लाइन पर रहो। "
    "अभी पचास हज़ार रुपये transfer करो।"
)

LEGIT_DELIVERY = (
    "Hello sir, आपका parcel आया है। आपको OTP मिला होगा, वो बता दीजिए "
    "ताकि मैं parcel दे सकूँ। धन्यवाद।"
)

LEGIT_RELATIVE = (
    "भाई, मेरी car खराब हो गई है। mechanic ने पाँच हज़ार बोले हैं। "
    "अगर पैसे हों तो भेज दो, कल लौटा दूँगा।"
)


def test_scam_text_hits_multiple_signals() -> None:
    result = evaluate(SCAM)
    assert len(result.signals) >= 4
    assert Signal.AUTHORITY in result.signals
    assert Signal.MONEY_REQUEST in result.signals
    assert result.floor >= 0.6


def test_delivery_otp_is_not_a_scam_by_itself() -> None:
    result = evaluate(LEGIT_DELIVERY)
    assert Signal.SENSITIVE_REQUEST in result.signals
    assert result.floor < 0.45


def test_relative_asking_for_money_stays_low() -> None:
    result = evaluate(LEGIT_RELATIVE)
    assert result.floor < 0.45


def test_single_high_reading_does_not_alert() -> None:
    scorer = RiskScorer(DetectorConfig())
    rules = evaluate(SCAM)
    llm = LlmReading(signals=rules.signals, risk=0.95)
    state = scorer.update(rules, llm)
    assert state.alerted is False
    assert state.consecutive_high == 1


def test_two_consecutive_high_readings_alert() -> None:
    scorer = RiskScorer(DetectorConfig())
    rules = evaluate(SCAM)
    llm = LlmReading(signals=rules.signals, risk=0.95)
    scorer.update(rules, llm)
    state = scorer.update(rules, llm)
    assert state.alerted is True


def test_alert_clears_only_after_two_low_readings() -> None:
    scorer = RiskScorer(DetectorConfig())
    hot = evaluate(SCAM)
    llm = LlmReading(signals=hot.signals, risk=0.95)
    scorer.update(hot, llm)
    assert scorer.update(hot, llm).alerted is True

    calm = evaluate("नमस्ते, धन्यवाद।")
    scorer.update(calm, None)
    assert scorer.state.alerted is True
    scorer.update(calm, None)
    assert scorer.state.alerted is False


def test_llm_alone_cannot_alert_without_signals() -> None:
    """A confident LLM with no signals must not raise the alarm."""
    scorer = RiskScorer(DetectorConfig())
    empty = evaluate("")
    llm = LlmReading(signals=Signals(), risk=0.99)
    scorer.update(empty, llm)
    state = scorer.update(empty, llm)
    assert state.alerted is False
