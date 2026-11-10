"""Generate the synthetic test calls with Gnani Timbre.

Every clip here is **synthetic and scripted** — it is produced by text-to-speech
from ``data/scripts/calls.json``. No real call recordings are used anywhere in
this project.

For each script we produce:

* ``data/audio/demo/<id>.wav``          clean, two voices
* ``data/audio/eval/<id>_noisy.wav``    same audio mixed with background noise

Usage::

    python -m scripts.make_test_calls
    python -m scripts.make_test_calls --force --snr 10
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

from app.alerts.tts import TimbreTTS
from app.config import ROOT, settings
from app.keypool import KeyPool

SCRIPTS_PATH = ROOT / "data" / "scripts" / "calls.json"
DEMO_DIR = ROOT / "data" / "audio" / "demo"
EVAL_DIR = ROOT / "data" / "audio" / "eval"
MANIFEST_PATH = ROOT / "data" / "results" / "manifest.json"

# Two distinct voices make the "call" readable in the demo.
VOICE_BY_SPEAKER = {"caller": "Avinash", "receiver": "Shivani"}
TURN_GAP_S = 0.45
END_PAD_S = 1.0
TARGET_RATE = 16_000


def load_scripts() -> list[dict]:
    return json.loads(SCRIPTS_PATH.read_text(encoding="utf-8"))


def decode_wav(data: bytes) -> np.ndarray:
    samples, rate = sf.read(io.BytesIO(data), dtype="int16", always_2d=True)
    mono = samples.mean(axis=1) if samples.shape[1] > 1 else samples[:, 0]
    mono = np.asarray(mono, dtype=np.float64)
    if rate != TARGET_RATE:
        length = int(round(mono.size * TARGET_RATE / rate))
        mono = np.interp(np.linspace(0, mono.size - 1, length), np.arange(mono.size), mono)
    return mono


def pink_noise(length: int, rng: np.random.Generator) -> np.ndarray:
    """Cheap 1/f-ish noise: integrated white noise with drift removed."""
    white = rng.standard_normal(length)
    walk = np.cumsum(white)
    walk -= np.linspace(walk[0], walk[-1], length)
    peak = np.max(np.abs(walk))
    return walk / peak if peak else walk


def add_noise(signal: np.ndarray, snr_db: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = pink_noise(signal.size, rng)

    speech_rms = float(np.sqrt(np.mean(signal**2))) or 1.0
    noise_rms = float(np.sqrt(np.mean(noise**2))) or 1.0
    target_noise_rms = speech_rms / (10 ** (snr_db / 20))
    noisy = signal + noise * (target_noise_rms / noise_rms)

    peak = float(np.max(np.abs(noisy)))
    if peak > 32767:
        noisy = noisy * (32767 / peak)
    return noisy


def write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(samples, -32768, 32767).astype(np.int16), TARGET_RATE, subtype="PCM_16")


async def build_call(tts: TimbreTTS, script: dict, *, force: bool, snr_db: float) -> dict:
    call_id = script["id"]
    clean_path = DEMO_DIR / f"{call_id}.wav"
    noisy_path = EVAL_DIR / f"{call_id}_noisy.wav"

    if clean_path.exists() and noisy_path.exists() and not force:
        print(f"  skip {call_id} (exists)")
    else:
        pieces: list[np.ndarray] = []
        for turn in script["turns"]:
            audio = await tts.synthesize(
                turn["text"],
                voice=VOICE_BY_SPEAKER.get(turn["speaker"], "Avinash"),
                language="auto",
            )
            pieces.append(decode_wav(audio))
            pieces.append(np.zeros(int(TARGET_RATE * TURN_GAP_S)))

        pieces.append(np.zeros(int(TARGET_RATE * END_PAD_S)))
        clean = np.concatenate(pieces)
        write_wav(clean_path, clean)
        write_wav(noisy_path, add_noise(clean, snr_db, seed=abs(hash(call_id)) % 10_000))
        print(f"  built {call_id} ({clean.size / TARGET_RATE:.1f}s)")

    duration = sf.info(str(clean_path)).duration
    return {
        "id": call_id,
        "label": script["label"],
        "scam_type": script["scam_type"],
        "clean": str(clean_path.relative_to(ROOT)).replace("\\", "/"),
        "noisy": str(noisy_path.relative_to(ROOT)).replace("\\", "/"),
        "duration_s": round(duration, 2),
        "reference": " ".join(t["text"] for t in script["turns"]),
        "synthetic": True,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Build synthetic test calls with Gnani Timbre")
    parser.add_argument("--force", action="store_true", help="regenerate even if files exist")
    parser.add_argument("--snr", type=float, default=10.0, help="noise SNR in dB for the noisy variant")
    parser.add_argument("--only", default="", help="comma-separated call ids")
    args = parser.parse_args()

    if not settings.gnani.api_keys:
        print("GNANI_API_KEYS is not set in .env", file=sys.stderr)
        return 1

    scripts = load_scripts()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        scripts = [s for s in scripts if s["id"] in wanted]

    pool = KeyPool(settings.gnani.api_keys, "gnani")
    tts = TimbreTTS(pool, settings.gnani)

    print(f"building {len(scripts)} synthetic calls (SNR {args.snr} dB)")
    manifest: list[dict] = []
    try:
        for script in scripts:
            entry = await build_call(tts, script, force=args.force, snr_db=args.snr)
            manifest.append(entry)
    finally:
        await tts.aclose()

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {MANIFEST_PATH.relative_to(ROOT)} ({len(manifest)} calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
