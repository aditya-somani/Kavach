# Kavach — build plan

Real-time scam-call shield for Hindi/Hinglish phone calls.
Hackathon: Gnani AI "Great Indian AI Internship Challenge". Hard deadline
**10 Nov 2026, 23:59 IST**.

This file is the working plan and is updated as things change. If it disagrees
with the code, the code is wrong or this file is stale — one of them gets fixed.

---

## 1. What we are actually claiming

We listen to call audio, transcribe it with **Gnani Prisma**, and score it for
scam behaviour while the call is still happening. When the score crosses a
threshold we show a red banner and speak a short Hindi warning with **Gnani
Timbre**, and flag the call on a family dashboard.

**Not claimed:** intercepting real phone calls, blocking calls, sub-second
detection, production accuracy. See the README limits section.

---

## 2. Scope: in, out, and why

| Item | Status | Reason |
|---|---|---|
| File audio → Prisma → detector → UI → Timbre warning | **In** | The demo spine |
| Browser microphone (live) | **In** | Makes the demo feel live, no telephony risk |
| Rules + LLM + code-combined scoring, hysteresis | **In** | The intellectual core; judges probe this |
| Live transcript UI, risk meter, signal checklist, red banner | **In** | The visible product |
| Family page (live over SSE) | **In** | Replaces Telegram, better on camera |
| SQLite event log | **In** | Cheap, required |
| Eval harness, 14 scripted calls, clean + noisy, WER | **In** | Honest numbers are a judging criterion |
| README + limits section + `.env.example` | **In** | Disqualification risk if skipped |
| 60-second demo video | **In** | Highest-leverage item; own time slot |
| Telegram bot | **Out** (interface only) | Needs bot token + chat_id + a phone on camera |
| Email/SMTP fallback | **Out** | Not demo-visible |
| LiveKit inbound phone call | **Optional stretch** | 45-min box, only if ahead of schedule |
| Outbound calls / call bridging | **Out** | Explicitly out of brief |
| Deploying the agent to LiveKit Cloud | **Out** | 10–20 s cold start would wreck a 60 s video |

---

## 3. Architecture

```
audio source ──► Prisma STT (WS) ──► transcript segments
 (file | mic | LiveKit)                  │
                                         ▼
                              detector ──┬── rules.py   (keyword floor)
                                         ├── llm.py     (Groq, strict JSON, every ~3.5 s)
                                         └── scoring.py (combine, smooth, hysteresis)
                                         │
                                         ▼
                                   risk state ──┬──► live UI (SSE)
                                                ├──► Timbre warning (spoken)
                                                ├──► family page + notifier
                                                └──► SQLite event log
```

Audio sources sit behind one interface (`AudioSource` yielding 1024-byte PCM
frames at real-time cadence), so file, mic and LiveKit are interchangeable and
the rest of the system cannot tell them apart.

### Module boundaries

| Module | Responsibility | Must not |
|---|---|---|
| `app/audio_sources/` | Produce PCM frames at 16 kHz | Know about scams |
| `app/stt/` | Prisma WS client, emits transcript segments | Know about scoring |
| `app/detector/rules.py` | Hard-signal regex/keywords | Call the network |
| `app/detector/llm.py` | Groq call, strict JSON parse | Decide the alert |
| `app/detector/scoring.py` | Combine layers, smooth, hysteresis | Do I/O |
| `app/alerts/` | TTS synthesis, notifiers | Know about STT internals |
| `app/api/` | FastAPI routes, SSE fan-out | Contain business logic |
| `app/storage/` | SQLite writes/reads | Contain scoring logic |
| `eval/` | Run scripts, compute metrics | Be imported by the app |

---

## 4. Detector design (the part that matters)

Six signals: `authority`, `fear`, `urgency`, `isolation`, `sensitive_request`,
`money_request`.

**Layer 1 — rules.** Hindi + Hinglish keyword and pattern sets produce a floor
score per signal. Deliberately conservative: a hard signal alone must not alert.

**Layer 2 — LLM.** Every ~3.5 s, or on each final segment, send the last ~30 s
of transcript to Groq and demand strict JSON:
`{"signals": {...}, "risk": 0..1, "reason_hi": "...", "reason_en": "..."}`.
Malformed output is discarded, never guessed at.

**Layer 3 — scoring.** Combine rules and LLM, smooth over time, then apply
hysteresis: alert only after `consecutive_high` readings above
`alert_threshold`, and clear only below `clear_threshold`. Alert needs at least
`min_signals_for_alert` distinct signals.

**False positives are a first-class concern.** Four legitimate scripts (bank EMI,
delivery, doctor appointment, relative genuinely in trouble) are in the eval set
and must not alert.

---

## 5. Timeline (5.5 h)

| Slot | Work | Checkpoint |
|---|---|---|
| 0:00–0:35 | env, deps, **API gate**: one Prisma transcript, one Timbre WAV | — |
| 0:35–1:40 | thin end-to-end: file → Prisma → rules → risk → SQLite → bare UI | `v0.1` |
| 1:40–2:40 | LLM layer, scoring, hysteresis, Timbre warning, mic input | `v0.2` |
| 2:40–3:25 | live UI, family page + SSE, notifier | `v0.3` |
| 3:25–4:20 | eval harness, 14 calls, noise, WER, results table | `v0.4` |
| 4:20–4:45 | README, limits, `.env.example`, tag | `v1.0` |
| 4:45–5:30 | demo video, record + edit | submit |

Buffer: ~20 min. LiveKit inbound is attempted only if `v0.4` lands early.

---

## 6. Known risks

| Risk | Mitigation |
|---|---|
| Gnani key invalid / quota | Round-robin `KeyPool`; API gate before any app code |
| Prisma 15-min session cap | Reconnect path shared with key rotation |
| Python 3.14 wheels | Isolated 3.12 venv via `uv` |
| No ffmpeg on the machine | Everything is 16 kHz mono WAV; mic resamples in JS |
| Echo loop (TTS → mic) | Pause STT while the warning plays |
| LLM rate limits during eval | Cache responses by segment hash |
| Running out of time | Cut order is explicit: LiveKit → Telegram → eval size |

---

## 7. Honesty rules (non-negotiable)

- Test calls are **synthetic and scripted**. Labelled in `data/`, in code, in the README.
- README states exactly which sources were demonstrated: file, mic, or real inbound call.
- No accuracy claim beyond the eval table.
- No secrets committed; `.env` is gitignored, `.env.example` is the template.
