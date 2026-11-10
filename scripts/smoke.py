"""API gate: prove Prisma and Timbre work before building anything else.

Run this first. It answers, with evidence, the two questions that decide the
whole project:

1. Does the Gnani key authenticate?
2. What exact audio formats come back, and does streaming STT return segments?

Usage::

    python -m scripts.smoke
"""

from __future__ import annotations

import asyncio
import sys

from app.alerts.tts import TimbreTTS
from app.audio_sources.base import PushAudioSource, pcm16_frames
from app.config import ROOT, settings
from app.keypool import KeyPool
from app.stt.prisma import PrismaSTT

DEMO = ROOT / "data" / "audio" / "demo" / "scam_digital_arrest.wav"
PHRASE = "सावधान। यह स्कैम हो सकता है।"


async def check_tts() -> bool:
    print("\n[1/2] Timbre TTS")
    if not settings.gnani.api_keys:
        print("  FAIL: GNANI_API_KEYS is not set in .env")
        return False

    pool = KeyPool(settings.gnani.api_keys, "gnani")
    tts = TimbreTTS(pool, settings.gnani)
    try:
        audio = await tts.synthesize(PHRASE)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {exc}")
        return False
    finally:
        await tts.aclose()

    header = audio[:4]
    is_wav = header == b"RIFF"
    print(f"  OK: {len(audio)} bytes, RIFF header={is_wav}, model={settings.gnani.tts_model}, "
          f"voice={settings.gnani.tts_voice}")
    print(f"  duration ≈ {max(0, len(audio) - 44) / 32000:.2f}s at 16 kHz mono s16")
    out = ROOT / "data" / "audio" / "smoke_tts.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio)
    print(f"  saved {out.relative_to(ROOT)}")
    return is_wav


async def check_stt() -> bool:
    print("\n[2/2] Prisma streaming STT")
    if not settings.gnani.api_keys:
        print("  FAIL: GNANI_API_KEYS is not set in .env")
        return False
    if not DEMO.exists():
        print(f"  SKIP: no demo audio at {DEMO.relative_to(ROOT)} (run scripts.make_test_calls first)")
        return False

    from app.audio_sources.file_source import load_pcm16

    pool = KeyPool(settings.gnani.api_keys, "gnani")
    stt = PrismaSTT(pool, settings.gnani, settings.audio)

    # Feed the file through the same 1024-byte frame path the app uses.
    source = PushAudioSource("smoke")
    samples = load_pcm16(DEMO)
    frames = pcm16_frames(samples)

    async def feed() -> None:
        for frame in frames:
            await source.push(frame)
            await asyncio.sleep(settings.audio.frame_seconds)
        await source.close()

    feeder = asyncio.create_task(feed())
    count = 0
    try:
        async for segment in stt.stream(source):
            count += 1
            print(f"  [{segment.index}] {segment.latency_ms} ms · {segment.text[:90]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {exc}")
        return False
    finally:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)

    print(f"  OK: {count} transcript segments")
    return count > 0


async def main() -> int:
    print("Kavach API gate")
    print(f"  STT model   {settings.gnani.stt_model}")
    print(f"  STT lang    {settings.gnani.stt_language}")
    print(f"  sample rate {settings.audio.sample_rate} Hz, frame {settings.audio.bytes_per_frame} bytes "
          f"({settings.audio.frame_ms} ms)")
    print(f"  gnani keys  {len(settings.gnani.api_keys)}")
    print(f"  groq keys   {len(settings.groq.api_keys)}")

    tts_ok = await check_tts()
    stt_ok = await check_stt()

    print("\nRESULT")
    print(f"  timbre: {'PASS' if tts_ok else 'FAIL'}")
    print(f"  prisma: {'PASS' if stt_ok else 'FAIL'}")
    return 0 if (tts_ok and stt_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
