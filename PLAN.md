# wxparser — Project Plan

Continuously listen to the NOAA Weather Radio (NWR) broadcast from **KJY93 Muncie, IN
(162.425 MHz)**, transcribe it to text, and persist **only new/updated reports** — no
duplicates from the repeating broadcast loop.

This document is the working plan. It is the source of truth for scope, architecture, and
phasing until code supersedes it.

---

## 1. Goal & scope

**In scope (v1):**
- Continuously capture audio from a dedicated weather radio.
- Transcribe the spoken broadcast to text.
- Detect when broadcast content is **genuinely new** vs. the repeating loop, and save only
  new reports (deduplicated), each timestamped.
- Emit every saved report as **structured JSON** (one JSON object per report, written as
  JSONL) so other apps can consume it directly — see §5.1.
- Run continuously as an always-on service.

**Out of scope (deferred / possible later):**
- SAME/EAS digital alert decoding and area targeting.
- Meshtastic / mesh publishing.
- HTTP/MQTT live feed or query API.
- Online enrichment from `api.weather.gov`.

These are intentionally cut to keep v1 tight. The architecture leaves clean seams to add
them later (see §8).

## 2. Hard constraint: licensing

The app must be **MIT-licensable**, so every bundled/linked dependency must be
permissively licensed. Anything copyleft (GPL/LGPL) may only be used **across a process
boundary** (separate binary, subprocess/pipe) — never imported or linked.

Because RF reception is handled by the **hardware radio** (not `rtl_fm`/`librtlsdr`), there
is **no GPL component** in the software stack at all:

| Component | Role | License | Notes |
|---|---|---|---|
| sounddevice / PortAudio | audio capture | MIT | |
| whisper.cpp | transcription | MIT | `tiny.en` / `base.en` models (MIT) |
| Vosk (alternative) | transcription | Apache-2.0 | lighter; better on weak CPUs |
| librosa | audio fingerprint (MFCC) | ISC | |
| numpy | numerics | BSD | |
| webrtcvad (BSD) / silero-vad (MIT) | voice activity detection | permissive | |
| rapidfuzz | fuzzy text dedup | MIT | |
| SQLite (stdlib `sqlite3`) | optional storage | public domain | |

## 3. Hardware & signal path

Audio is tapped from a **Reecom R-1630** weather radio (the radio does RF + FM demod +
squelch in hardware).

```
Reecom R-1630
  └─ "external audio output" jack (continuous, real-time broadcast)
       └─ 3.5 mm cable
            └─ USB audio adapter (line-in)
                 └─ Linux PC running wxparser
```

Notes:
- Use the **external audio output** jack (continuous), **not** the ALARM OUT socket (only
  active during a siren). The continuous output also carries the SAME burst tones, leaving
  the door open for SAME decoding later from the same single cable.
- **Verify output level** during Phase 0: if the jack is speaker-level rather than
  line-level it will overdrive a mic input — use a line-in or an inline attenuator.

### Host

Target host: an old **32-bit Linux PC** the user already has.

Risk & mitigation (the main project risk):
- 32-bit-only hardware is old/weak and most STT engines ship 64-bit prebuilt binaries.
  whisper.cpp can be compiled for 32-bit (slow, but acceptable — see §4). Vosk likely
  requires compiling Kaldi on 32-bit.
- **Mitigation:** the design is host-agnostic. If the old PC can't keep up or the STT build
  is too painful, a 64-bit Raspberry Pi (prebuilt binaries, runs `tiny.en`/Vosk
  comfortably) drops in with **zero code changes**. The old PC can alternatively act only as
  the recorder and hand audio to a beefier box.
- **To confirm:** CPU model (`grep "model name" /proc/cpuinfo`) and RAM (`free -h`) to pick
  the STT engine/model.

## 4. Why continuous transcription is feasible on weak hardware

NWR broadcasts a **repeating loop** of products that replays every few minutes until NWS
updates content. The audio is **deterministic TTS**, so a repeated product produces
near-identical audio each loop.

Consequence: we can detect repeats by cheap **audio fingerprint** comparison and run
expensive STT **only on novel audio**. Because new content appears only a handful of times
per day — and the loop repeats (idle for the CPU) for long stretches between updates — even
an STT engine running **slower than real-time** can keep up by buffering novel segments and
catching up during the long repeat periods.

So: STT rarely runs, and when it does it has plenty of idle time to finish. This is what
makes a 32-bit host viable.

## 5. Architecture

```
continuous audio capture (ring buffer)
  → segment on silence (VAD)
  → per-segment audio fingerprint (MFCC summary) — no STT
  → compare against fingerprints already seen this loop:
        match → SKIP (repeat)
        novel → enqueue for transcription
  → STT worker transcribes ONLY novel segments (background, buffered)
  → normalize + fuzzy text dedup vs. last saved report (safety net)
  → persist new report (timestamped); never re-save a duplicate
```

### Components

- **Capture** — `sounddevice` stream into a ring buffer; mono, 16 kHz (STT-native rate).
- **Segmenter** — VAD splits the stream on inter-product/inter-sentence silence into
  segments.
- **Fingerprint / novelty gate** — compute an MFCC-summary fingerprint per segment; keep a
  rolling set of fingerprints seen in the current loop; skip matches, pass novel segments
  through. This is the dedup workhorse and is cheap.
- **STT worker** — a queue + worker; transcribes only novel segments; tolerant of
  slower-than-real-time throughput via buffering.
- **Text dedup** — normalize (lowercase, collapse whitespace, strip filler) and
  fuzzy-compare (rapidfuzz) the new transcript against recently saved reports as a
  second-line guard against fingerprint misses.
- **Store** — write each new report as a timestamped record. v1: append JSONL and/or one
  file per report under `transcripts/`. Optional SQLite later.

### 5.1 Structured JSON output

Every saved report is a self-contained JSON object so downstream apps can process it
without re-parsing free text. Reports are appended to a **JSONL** file
(`transcripts/reports.jsonl`) — one object per line — which is trivial to stream, tail, or
ingest. Optionally mirrored one-file-per-report and/or into SQLite later.

```json
{
  "schema_version": 1,
  "id": "2026-06-23T14:05:11Z-a1b2c3",
  "station": "KJY93",
  "frequency_mhz": 162.425,
  "captured_at": "2026-06-23T14:05:11Z",
  "duration_s": 47.2,
  "product_type": "zone_forecast",
  "text": "…full transcribed text of the new report…",
  "segments": [
    { "start_s": 0.0,  "end_s": 6.4,  "text": "…sentence…" },
    { "start_s": 6.4,  "end_s": 12.1, "text": "…sentence…" }
  ],
  "stt": { "engine": "whisper.cpp", "model": "base.en", "avg_confidence": 0.0 },
  "fingerprint": "…",
  "supersedes": null
}
```

Field notes / feasibility:

- **Core fields are free** — they fall straight out of the capture/STT/dedup pipeline:
  `id`, `captured_at`, `duration_s`, `text`, `segments` (whisper.cpp/Vosk already emit
  per-segment timestamps), `fingerprint`, `stt`.
- **`product_type`** is best-effort classification via cheap keyword matching on the text
  (e.g. "zone forecast", "hazardous weather outlook", "current conditions", "tornado
  warning"). It's a hint, not authoritative — defaults to `"unknown"`. Authoritative typing
  comes later from SAME decoding (§8).
- **`supersedes`** links a report to the `id` of the previous report it replaces (set when
  text-dedup finds a near-match that changed), giving consumers an update chain for free.
- **`schema_version`** lets the JSON evolve without breaking consumers.

Deep field extraction (parsing temperatures, wind, valid times, affected counties into typed
fields) is **deliberately out of v1** — it's brittle off raw TTS transcription. The clean
path to that is SAME headers + the `api.weather.gov` source text (§8). v1 gives consumers
well-structured *envelopes* around reliable transcript text, which is the feasible and
genuinely useful 80%.

## 6. Phases & milestones

- **Phase 0 — Capture & bring-up.** Wire R-1630 external audio out → USB adapter → PC.
  Confirm levels, record a sample WAV, capture one full loop cycle. *Done when:* clean
  recorded audio at correct level.
- **Phase 1 — Transcribe everything.** Continuous capture → STT → raw timestamped
  transcript to stdout/JSONL. *Done when:* live transcript is produced and we know whether
  the 32-bit PC keeps up (decides STT engine/model).
- **Phase 2 — Novelty gating.** Add VAD segmentation + audio-fingerprint dedup so only
  novel segments reach STT. *Done when:* a stable loop produces near-zero new saves; an
  actual content update produces exactly one new save.
- **Phase 3 — Clean reports + service.** Text-level dedup, tidy report records, run as a
  restart-safe `systemd` service. *Done when:* it runs unattended for days, saving only
  real updates.

## 7. Repo layout (proposed)

```
wxparser/
├── PLAN.md            # this file
├── README.md
├── LICENSE            # MIT
├── pyproject.toml     # deps, entry point
├── wxparser/
│   ├── __init__.py
│   ├── capture.py     # audio capture / ring buffer
│   ├── segment.py     # VAD segmentation
│   ├── fingerprint.py # MFCC fingerprint + novelty gate
│   ├── stt.py         # transcription worker (whisper.cpp or vosk backend)
│   ├── dedup.py       # text normalization + fuzzy dedup
│   ├── store.py       # report persistence
│   └── main.py        # wiring + service loop
├── transcripts/       # output (gitignored)
└── tests/             # tests against recorded WAVs
```

## 8. Future hooks (designed-for, not built)

- **SAME/EAS** — the continuous audio already carries SAME bursts; add a `samedec`
  (MIT/Apache) subprocess stage for structured alerts + FIPS targeting.
- **Meshtastic** — isolated optional module; talk to the node via CLI/serial across a
  process boundary so the MIT core never imports GPL `meshtastic-python`.
- **Live feed / API** — MQTT or HTTP/SSE publisher behind the same report stream.
- **Online enrich** — on a SAME hit, fetch authoritative public-domain text from
  `api.weather.gov` for perfect text + transcript verification.

## 9. Open questions

1. Old PC specs (CPU model, RAM) → pick whisper.cpp vs. Vosk and model size.
2. R-1630 external audio output level (line vs. speaker) → capture wiring.
3. Report granularity: dedup per **segment/product**, or per **whole loop**? (Leaning
   per-segment for robustness to partial updates.)
