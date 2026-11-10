"""Layer 1 — fast keyword and pattern rules.

These are a *floor*, not a decision. They are deliberately conservative: a
single signal never alerts, because legitimate calls routinely contain one
(a delivery agent says "OTP", a bank says "EMI"). What they buy us is
determinism and zero latency when the LLM is slow or down.

Hindi is matched as a substring in Devanagari, Hinglish/English case-folded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.detector.models import Signal, Signals

# Each entry is a phrase; matching is substring-based on normalised text.
_KEYWORDS: dict[Signal, tuple[str, ...]] = {
    Signal.AUTHORITY: (
        # English / Hinglish
        "cbi", "central bureau", "enforcement directorate", "income tax", "narcotics",
        "cyber crime", "cyber cell", "crime branch", "customs", "custom officer",
        "police", "inspector", "sub inspector", "officer", "court", "judge",
        "arrest warrant", "warrant", "fir", "trai", "rbi", "reserve bank",
        "digital arrest", "government officer", "legal department", "advocate",
        "supreme court", "high court", "ed officer",
        # Devanagari
        "सीबीआई", "पुलिस", "कस्टम", "अदालत", "जज", "वारंट", "गिरफ्तार",
        "एफआईआर", "नारकोटिक्स", "साइबर", "क्राइम ब्रांच", "अधिकारी",
        "आयकर", "प्रवर्तन निदेशालय", "ट्राई", "आरबीआई", "डिजिटल अरेस्ट",
        "कानूनी", "वकील", "कोर्ट",
    ),
    Signal.FEAR: (
        "arrest", "jail", "prison", "case filed", "non-bailable", "money laundering",
        "account will be frozen", "account freeze", "blocked", "punishment",
        "you will be caught", "legal action", "court notice", "summons",
        "drugs found", "parcel contains", "illegal items", "deportation",
        "गिरफ्तारी", "जेल", "केस", "मामला", "सजा", "खाता बंद", "फंस",
        "कानूनी कार्रवाई", "नोटिस", "धोखाधड़ी", "पुलिस केस", "जुर्म",
        "ड्रग्स", "गैरकानूनी",
    ),
    Signal.URGENCY: (
        "immediately", "right now", "within 10 minutes", "within 10 min",
        "within an hour", "last warning", "final notice", "final warning",
        "hurry", "act fast", "before it is too late", "today itself",
        "abhi", "turant", "fauran", "jaldi", "aaj hi", "abhi karo",
        "aakhri mauka", "last chance", "2 hours", "ten minutes",
        "तुरंत", "अभी", "फौरन", "जल्दी", "आज ही", "आखिरी मौका",
        "अंतिम चेतावनी", "दस मिनट", "एक घंटे",
    ),
    Signal.ISOLATION: (
        "do not tell anyone", "don't tell anyone", "do not tell anybody",
        "keep this confidential", "keep it confidential", "stay on the line",
        "do not hang up", "don't hang up", "do not disconnect", "stay connected",
        "do not involve", "between you and me", "secret", "alone",
        "kisi ko mat batao", "kisi ko nahi batana", "kisi ko mat batana",
        "phone mat kaato", "phone mat kato", "line par raho", "line pe raho",
        "akela", "gupt", "kisi ko mat bolo",
        "किसी को मत बताओ", "किसी को नहीं बताना", "किसी को मत बोलना",
        "गुप्त", "फोन मत काटो", "लाइन पर रहो", "अकेले", "किसी को न बताएं",
    ),
    Signal.SENSITIVE_REQUEST: (
        "otp", "one time password", "aadhaar", "aadhar", "pan card", "pan number",
        "card number", "cvv", "atm pin", "upi pin", "net banking password",
        "anydesk", "teamviewer", "quicksupport", "quick support", "screen share",
        "screen sharing", "remote access", "install this app", "download this app",
        "apk", "bank details", "account number", "ifsc", "date of birth",
        "ओटीपी", "आधार", "पैन", "कार्ड नंबर", "सीवीवी", "पिन",
        "एनीडेस्क", "टीमव्यूअर", "स्क्रीन शेयर", "ऐप डाउनलोड", "रिमोट",
        "बैंक डिटेल", "खाता संख्या", "आधार नंबर",
    ),
    Signal.MONEY_REQUEST: (
        "transfer", "send money", "send the money", "pay now", "make the payment",
        "upi", "gpay", "google pay", "phonepe", "paytm", "bank transfer",
        "neft", "rtgs", "deposit", "fine", "penalty", "clearance fee",
        "processing fee", "security deposit", "refund", "rupees", "lakh", "crore",
        "paise bhejo", "paisa bhejo", "transfer karo", "payment karo",
        "jurmana", "fees", "rupaye",
        "पैसे भेजो", "पैसे भेजिए", "ट्रांसफर", "भुगतान", "जुर्माना",
        "फीस", "यूपीआई", "रुपये", "लाख", "करोड़", "खाते में डालो",
    ),
}

# Numeric money mentions ("50,000", "₹5000", "2 lakh") strengthen a money signal.
_MONEY_AMOUNT = re.compile(r"(₹|rs\.?\s?\d|\d[\d,]{2,}\s*(rupees|rupaye|रुपये|lakh|लाख|crore|करोड़))", re.I)
_OTP_LIKE = re.compile(r"\b\d{4,6}\b")

_MAX_RULE_FLOOR = 0.70          # rules alone cannot cross the 0.75 alert line
_MAX_RULE_FLOOR_EXTREME = 0.80  # except on a four-signal, explicit combo


@dataclass(frozen=True)
class RuleHit:
    signal: Signal
    matched: tuple[str, ...]


@dataclass(frozen=True)
class RuleResult:
    signals: Signals
    hits: tuple[RuleHit, ...]
    floor: float

    def explain(self) -> str:
        return ", ".join(f"{h.signal.value}({h.matched[0]})" for h in self.hits)


def normalise(text: str) -> str:
    return " ".join(text.lower().split())


def evaluate(text: str) -> RuleResult:
    """Extract signals from a transcript window and compute a floor score."""
    if not text or not text.strip():
        return RuleResult(Signals(), (), 0.0)

    haystack = normalise(text)
    hits: list[RuleHit] = []
    active: set[Signal] = set()

    for signal, phrases in _KEYWORDS.items():
        matched = tuple(p for p in phrases if p in haystack)
        if matched:
            active.add(signal)
            hits.append(RuleHit(signal, matched))

    if Signal.MONEY_REQUEST in active and _MONEY_AMOUNT.search(haystack):
        hits.append(RuleHit(Signal.MONEY_REQUEST, ("<amount>",)))
    if Signal.SENSITIVE_REQUEST in active and _OTP_LIKE.search(haystack):
        hits.append(RuleHit(Signal.SENSITIVE_REQUEST, ("<numeric-code>",)))

    signals = Signals(frozenset(active))
    return RuleResult(signals, tuple(hits), _floor(signals))


def _floor(signals: Signals) -> float:
    """How much a combination of signals is worth on its own."""
    authority = Signal.AUTHORITY in signals
    fear = Signal.FEAR in signals
    isolation = Signal.ISOLATION in signals
    sensitive = Signal.SENSITIVE_REQUEST in signals
    money = Signal.MONEY_REQUEST in signals
    urgency = Signal.URGENCY in signals

    # The classic digital-arrest shape: authority + fear + a demand.
    if authority and fear and (money or sensitive) and (isolation or urgency):
        return _MAX_RULE_FLOOR_EXTREME

    score = 0.0
    if (money or sensitive) and (authority or isolation):
        score = max(score, 0.62)
    if authority and fear:
        score = max(score, 0.55)
    if money and sensitive:
        score = max(score, 0.50)
    if len(signals) >= 3:
        score = max(score, 0.55)

    # Singles: deliberately weak.
    singles = {
        Signal.AUTHORITY: 0.22,
        Signal.FEAR: 0.20,
        Signal.ISOLATION: 0.30,
        Signal.SENSITIVE_REQUEST: 0.25,
        Signal.MONEY_REQUEST: 0.20,
        Signal.URGENCY: 0.12,
    }
    if len(signals) == 1:
        score = max(score, singles[next(iter(signals.active))])

    return min(score, _MAX_RULE_FLOOR)
