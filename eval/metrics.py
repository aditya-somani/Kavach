"""Metrics for the evaluation harness.

Kept separate from the runner so the numbers can be unit-tested with synthetic
inputs and no network.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

import jiwer

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[।,.!?;:\"'“”‘’()\[\]—–-]")


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Prisma returns final segments without punctuation and with inconsistent
    spacing, so WER is only meaningful after normalising both sides.
    """
    cleaned = _PUNCT.sub(" ", text.lower())
    return _WS.sub(" ", cleaned).strip()


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref = normalise(reference)
    hyp = normalise(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return float(jiwer.wer(ref, hyp))


@dataclass
class CallResult:
    call_id: str
    label: str            # "scam" | "legit"
    variant: str          # "clean" | "noisy"
    alerted: bool
    max_risk: float
    time_to_alert_s: float | None
    wer: float
    segments: int
    duration_s: float
    error: str = ""
    reasons: list[str] = field(default_factory=list)


def summarise(results: list[CallResult]) -> dict[str, float]:
    scams = [r for r in results if r.label == "scam"]
    legits = [r for r in results if r.label == "legit"]

    detected = [r for r in scams if r.alerted]
    false_alarms = [r for r in legits if r.alerted]
    ttfa = [r.time_to_alert_s for r in detected if r.time_to_alert_s is not None]

    return {
        "scam_calls": float(len(scams)),
        "legit_calls": float(len(legits)),
        "detection_rate": len(detected) / len(scams) if scams else 0.0,
        "false_alarm_rate": len(false_alarms) / len(legits) if legits else 0.0,
        "median_time_to_alert_s": statistics.median(ttfa) if ttfa else 0.0,
        "mean_time_to_alert_s": statistics.fmean(ttfa) if ttfa else 0.0,
        "mean_wer_clean": _mean_wer(results, "clean"),
        "mean_wer_noisy": _mean_wer(results, "noisy"),
    }


def _mean_wer(results: list[CallResult], variant: str) -> float:
    scores = [r.wer for r in results if r.variant == variant]
    return statistics.fmean(scores) if scores else 0.0


def to_markdown(results: list[CallResult], summary: dict[str, float]) -> str:
    lines: list[str] = []
    lines.append("# Kavach evaluation results\n")
    lines.append(
        "All calls are **synthetic and scripted** (`data/scripts/calls.json`), "
        "spoken by Gnani Timbre. No real call recordings are used.\n"
    )
    lines.append("## Summary\n")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Scam calls | {int(summary['scam_calls'])} |")
    lines.append(f"| Legitimate calls | {int(summary['legit_calls'])} |")
    lines.append(f"| Detection rate | {summary['detection_rate']:.0%} |")
    lines.append(f"| False alarm rate | {summary['false_alarm_rate']:.0%} |")
    lines.append(f"| Median time to alert | {summary['median_time_to_alert_s']:.1f} s |")
    lines.append(f"| Mean WER (clean) | {summary['mean_wer_clean']:.1%} |")
    lines.append(f"| Mean WER (noisy, 10 dB SNR) | {summary['mean_wer_noisy']:.1%} |")
    lines.append("")
    lines.append("## Per call\n")
    lines.append("| Call | Label | Variant | Alerted | Max risk | Time to alert | WER | Segments |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        ttfa = f"{r.time_to_alert_s:.1f} s" if r.time_to_alert_s is not None else "—"
        lines.append(
            f"| {r.call_id} | {r.label} | {r.variant} | "
            f"{'yes' if r.alerted else 'no'} | {r.max_risk:.2f} | {ttfa} | "
            f"{r.wer:.0%} | {r.segments} |"
        )
    lines.append("")
    return "\n".join(lines)
