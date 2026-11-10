"""Evaluation harness.

Runs every synthetic call through the real pipeline — real Prisma STT, real
detector, real SQLite — with the spoken warning disabled. Produces a markdown
table of detection rate, false alarms, time-to-alert and WER.

Usage::

    python -m eval.run_eval                 # clean + noisy
    python -m eval.run_eval --variant clean
    python -m eval.run_eval --only scam_digital_arrest,legit_delivery

The LLM responses are cached by window hash, so a second run costs nothing and
gives identical numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from app.alerts.notify import CollectingNotifier, NotifierHub
from app.audio_sources.file_source import FileAudioSource
from app.config import ROOT, settings
from app.detector.llm import LlmDetector
from app.keypool import KeyPool
from app.pipeline import CallSession, EventBus
from app.storage.db import Store
from app.stt.prisma import PrismaSTT
from eval.metrics import CallResult, summarise, to_markdown, word_error_rate

MANIFEST = ROOT / "data" / "results" / "manifest.json"
RESULTS_MD = ROOT / "data" / "results" / "results.md"
RESULTS_JSON = ROOT / "data" / "results" / "results.json"


async def run_one(entry: dict, variant: str, deps: "Deps") -> CallResult:
    path = ROOT / entry[variant]
    duration = float(entry.get("duration_s") or 0.0)

    if not path.exists():
        return CallResult(
            call_id=entry["id"], label=entry["label"], variant=variant,
            alerted=False, max_risk=0.0, time_to_alert_s=None, wer=1.0,
            segments=0, duration_s=duration, error=f"missing audio: {path}",
        )

    bus = EventBus()
    queue = bus.subscribe()
    notifier = CollectingNotifier()
    session = CallSession(
        source=FileAudioSource(path),
        stt=PrismaSTT(deps.gnani_pool, settings.gnani, settings.audio),
        llm=deps.llm,
        store=deps.store,
        bus=bus,
        tts=None,
        notifier=NotifierHub([notifier]),
        config=settings,
        enable_tts=False,
        call_id=f"eval_{entry['id']}_{variant}",
    )

    session.start()
    await session.wait()

    events: list[dict] = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)

    segments = [e["data"]["text"] for e in events if e["kind"] == "segment"]
    alert = next((e for e in events if e["kind"] == "alert"), None)
    risk_events = [e for e in events if e["kind"] == "risk"]
    max_risk = max((e["data"]["score"] for e in risk_events), default=0.0)

    ttfa = None
    if alert is not None and session.started_at:
        ttfa = max(0.0, alert["at"] - session.started_at)

    return CallResult(
        call_id=entry["id"],
        label=entry["label"],
        variant=variant,
        alerted=alert is not None,
        max_risk=max_risk,
        time_to_alert_s=ttfa,
        wer=word_error_rate(entry.get("reference", ""), " ".join(segments)),
        segments=len(segments),
        duration_s=duration,
        reasons=[alert["data"].get("reason_en", "")] if alert else [],
    )


class Deps:
    def __init__(self, gnani_pool: KeyPool, llm: LlmDetector, store: Store) -> None:
        self.gnani_pool = gnani_pool
        self.llm = llm
        self.store = store


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Kavach evaluation")
    parser.add_argument("--variant", choices=["clean", "noisy", "both"], default="both")
    parser.add_argument("--only", default="", help="comma-separated call ids")
    parser.add_argument("--limit", type=int, default=0, help="only the first N calls")
    args = parser.parse_args()

    if not MANIFEST.exists():
        print(f"missing {MANIFEST.relative_to(ROOT)} — run scripts.make_test_calls first", file=sys.stderr)
        return 1
    if not settings.gnani.api_keys:
        print("GNANI_API_KEYS is not set in .env", file=sys.stderr)
        return 1

    manifest: list[dict] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        manifest = [m for m in manifest if m["id"] in wanted]
    if args.limit:
        manifest = manifest[: args.limit]

    variants = ["clean", "noisy"] if args.variant == "both" else [args.variant]

    gnani_pool = KeyPool(settings.gnani.api_keys, "gnani")
    groq_pool = KeyPool(settings.groq.api_keys, "groq") if settings.groq.api_keys else None
    llm = LlmDetector(groq_pool, settings.groq) if groq_pool else _RulesOnlyLlm()
    store = Store(ROOT / "data" / "results" / "eval.db")
    deps = Deps(gnani_pool, llm, store)

    results: list[CallResult] = []
    try:
        total = len(manifest) * len(variants)
        done = 0
        for entry in manifest:
            for variant in variants:
                done += 1
                print(f"[{done}/{total}] {entry['id']} ({variant}) …", flush=True)
                started = time.perf_counter()
                try:
                    result = await run_one(entry, variant, deps)
                except Exception as exc:  # noqa: BLE001
                    print(f"    ERROR: {exc}")
                    result = CallResult(
                        call_id=entry["id"], label=entry["label"], variant=variant,
                        alerted=False, max_risk=0.0, time_to_alert_s=None, wer=1.0,
                        segments=0, duration_s=entry.get("duration_s", 0.0), error=str(exc),
                    )
                elapsed = time.perf_counter() - started
                flag = "ALERT" if result.alerted else "quiet"
                print(
                    f"    {flag} risk={result.max_risk:.2f} "
                    f"ttfa={result.time_to_alert_s if result.time_to_alert_s is None else round(result.time_to_alert_s, 1)} "
                    f"wer={result.wer:.0%} segs={result.segments} ({elapsed:.0f}s)"
                )
                results.append(result)
    finally:
        await llm.aclose()
        store.close()

    summary = summarise(results)
    markdown = to_markdown(results, summary)
    RESULTS_MD.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_MD.write_text(markdown, encoding="utf-8")
    RESULTS_JSON.write_text(
        json.dumps(
            {"summary": summary, "results": [r.__dict__ for r in results]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n" + markdown)
    print(f"written: {RESULTS_MD.relative_to(ROOT)}")
    return 0


class _RulesOnlyLlm:
    """Fallback when no Groq key is configured."""

    async def assess(self, window_text: str):  # noqa: ANN201
        from app.detector.models import LlmReading

        return LlmReading(ok=False, error="no Groq key configured")

    async def aclose(self) -> None:
        return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
