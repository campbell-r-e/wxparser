# wxparser — Project Plan

Continuously listen to the NOAA Weather Radio (NWR) broadcast from **KJY93 Muncie, IN
(162.425 MHz)**, transcribe it to text, and persist **only new/updated reports** — no
duplicates from the repeating broadcast loop.

This document is the working plan. It is the source of truth for scope, architecture, and
phasing until code supersedes it.

> **Implementation status (Phases 0–6 built):** capture → novelty-gated whisper.cpp STT →
> text dedup → timestamped JSONL is running as a restart-safe `systemd` service on the
> deployment host; SAME alert decoding, typed current-conditions + forecast extraction with
> repeat-voting, a local SQLite store, and a LAN-only HTTP/JSON query API
> (`/current`, `/forecast`, `/alerts/active`) are all in place. See `wxparser/`, `deploy/`,
> and `tests/`. The phase notes below are kept as the design record.

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

## 2. Hard constraints

### 2.1 Offline operation — no internet, ever

Once built/installed, the app **must run with no internet connection**. All data comes over
the radio; there are **no runtime network calls** of any kind. This is a non-negotiable
requirement (the whole point is a station that works when the internet is down).

Implications baked into the design:
- **STT models are bundled** — downloaded once at build/install time and shipped/cached
  locally. The running service never fetches a model.
- **No cloud STT / no APIs.** Transcription is fully local (whisper.cpp or Vosk).
- **No external web APIs** (e.g. `api.weather.gov`). All weather data comes over the radio.
- **Any lookup tables ship locally** — e.g. a future FIPS→county map is a bundled data
  file, not a service call.
- **Dependencies vendored/pinned** so a build doesn't require network at runtime (install
  time may use a package index; runtime must not).

A simple acceptance test: unplug the network and the app must capture, transcribe, dedup,
and save reports with no degradation.

### 2.2 Licensing — MIT-distributable

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

v1 ends at Phase 3 (the transcript MVP). The phases below evolve wxparser into a **radio-only
local weather server** (see §8) and are additive — they consume the same novel-segment
stream and do not change v1.

- **Phase 4 — SAME → structured alerts.** Add a `samedec` (MIT/Apache) subprocess stage on
  the captured audio; emit structured alert records (event, areas, valid/expire). *Done
  when:* a Required Weekly Test and any real alert produce correct typed alert records. See
  §9 for what SAME/FIPS give us.
- **Phase 5 — Structured current conditions.** Segment the loop into products; extract typed
  fields (temp, wind, humidity, pressure) from the current-conditions product via
  grammar/regex + **repeat-voting** (§8). *Done when:* current conditions are queryable and
  numbers are stable across the loop.
- **Phase 6 — Structured forecast + query API.** Extract zone-forecast periods (highs/lows,
  sky, precip %); persist observations/forecasts/alerts to local **SQLite**; expose a
  **LAN-only HTTP/JSON query API** (FastAPI, MIT — honors §2.1). *Done when:* other services
  can query `/current`, `/forecast`, `/alerts/active`.

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

## 8. Vision: a radio-only local weather server

The longer-term goal is to turn the transcript stream into **structured, queryable weather
data** that other services can ask factual questions of ("what's the current temperature?",
"today's high?", "any active warnings?") — sourced entirely from the radio, fully offline.

This is an **evolution of the v1 pipeline, not a rewrite**: structuring is a new stage that
consumes the same novel-segment stream, and the raw transcript is always kept as a fallback
so no data is lost when an extractor misses.

### Two structured-ish channels arrive over the one radio feed

1. **SAME/EAS digital headers** — *already structured data on the wire* (see §9). `samedec`
   decodes them into typed fields. This is the easy, highly reliable half of the server —
   but it only fires for **alerts/watches/warnings/tests**, not routine forecasts.
2. **The voice transcript** — carries the **routine** data (current conditions, zone
   forecast). Free text that must be parsed into fields. This is where the real work is.

### The repetition trick (key enabler)

NWR replays the same TTS audio every few minutes until NWS updates content, so we get **many
transcriptions of the same sentence per hour**. Numbers — where STT is weakest ("72°" vs
"70 two") — are **majority-voted across repeats** and range-checked, turning flaky
single-pass extraction into something reliable, for free, from the dedup buffer we already
keep. Combined with grammar-constrained recognition (Vosk FST grammars) or prompt biasing
(Whisper), number accuracy stops being the dealbreaker it normally is.

### Difficulty by product type

| Data | Source | Difficulty | Why |
|---|---|---|---|
| Alerts / warnings (event, areas, valid time) | SAME digital | **Easy** | Already structured; `samedec` parses it. |
| Current conditions (temp, wind, humidity, pressure) | voice | **Moderate** | Highly templated; regex/grammar extractable; numbers de-risked by repeat-voting. |
| Zone forecast periods (highs/lows, sky, precip %) | voice | **Moderate–Hard** | More phrasing variety, ranges, multiple periods to segment. |
| Discussion / outlook prose | voice | **Skip** | Free-form narrative — keep as text. |

What makes the moderate tiers tractable: NWR text is **formulaic**, and we target **one
station** (KJY93 / the Indianapolis WFO's phrasing), so templates are tuned to a narrow,
stable target rather than "all of English."

### Server shape

```
radio → transcript pipeline (v1)
  ├─ SAME stage (samedec) ──────────► structured alerts
  └─ product segmenter → per-type extractors (grammar/regex + repeat-voting)
        → typed records: observations / forecast periods / alerts
  → local store (SQLite — offline, public-domain license)
  → LAN-only HTTP/JSON query API (FastAPI, MIT — honors §2.1)
       GET /current        → {temp_f, wind, humidity, pressure, as_of}
       GET /forecast       → [{period, high_f, low_f, sky, precip_pct}, …]
       GET /alerts/active  → [{event, areas, expires}, …]
```

Every typed field carries **provenance + confidence + raw text** (voice vs SAME, vote
count), so consumers know what is authoritative vs. best-effort.

### Forecast vs. actual (history is the point)

Because the store is **append-only and timestamped**, and forecast periods carry
`valid_from`/`valid_to` while observations carry `captured_at`, the data model supports
asking **"what did we forecast for a given day, and what actually happened?"** as a join
between the forecast rows valid for that window and the observations recorded during it. This
retrospective ("how good were the forecasts?") falls straight out of the schema — the
forecast table reserves the valid-window columns specifically so the comparison is possible.

### Other future hooks (designed-for, not built)

- **Meshtastic** — isolated optional module; talk to the node via CLI/serial across a process
  boundary so the MIT core never imports GPL `meshtastic-python`.
- **Live push feed** — MQTT or HTTP/SSE publisher behind the same report stream (LAN only;
  honors §2.1).

## 9. Reference: SAME and FIPS codes

### SAME (Specific Area Message Encoding)

SAME is the digital signaling EAS/NWR uses to mark alerts. Before the spoken alert, the
station transmits short AFSK data bursts (the "duck" tones) that encode the alert as
machine-readable fields — and because they ride in the **same continuous audio** we already
capture, `samedec` (MIT/Apache) can decode them with no extra hardware.

A SAME header decodes to:

- **Originator** — who issued it (e.g. `WXR` = National Weather Service, `EAS`, `CIV`).
- **Event code** — a 3-letter type, e.g. `TOR` (Tornado Warning), `SVR` (Severe
  Thunderstorm Warning), `FFW` (Flash Flood Warning), `TOA` (Tornado Watch), `RWT`
  (Required Weekly Test), `RMT` (Required Monthly Test).
- **Location codes** — one or more 6-digit **FIPS area codes** (see below) naming exactly
  which counties/areas the alert applies to.
- **Valid duration** — purge time / how long the alert is in effect.
- **Issue time** — day-of-year + time (UTC) the message was sent.
- **Station ID** — the originating transmitter callsign (e.g. `KJY93`).

What this gives us: **instant, reliable, pre-transcription detection and typing of alerts**,
with exact area targeting and timing — no NLP, no STT errors. It's the trustworthy backbone
of the alert side of the weather server. (SAME does *not* cover routine forecasts/obs; those
come from the voice transcript.)

### FIPS codes (area targeting)

Each SAME location is a 6-digit code `PSSCCC`:

- **P** — part-of-county indicator (`0` = entire county/area; `1`–`9` = a specific portion).
- **SS** — 2-digit **state** FIPS code (Indiana = `18`).
- **CCC** — 3-digit **county** FIPS code (e.g. Delaware County, IN = `035`).

So `018035` = all of Delaware County, Indiana. KJY93 covers a set of east-central Indiana
counties, each with its own FIPS code. With a **bundled local FIPS→county lookup table**
(shipped with the app — no network, per §2.1) we translate those codes into human-readable
county names and can **filter alerts to only the areas we care about**. This is what lets the
server answer "is there an active warning *for my county*?" purely from the radio.

### Worked example: tornado warning for Delaware County

A SAME header decodes to fields like this — **no transcription involved**, this is straight
from the digital burst:

```
ZCZC-WXR-TOR-018035-018057+0045-1741830-KJY93-
        │    │    │       │      │       │
        │    │    │       │      │       └─ originating station = KJY93 (Muncie)
        │    │    │       │      └───────── issued: day 174, 18:30 UTC
        │    │    │       └──────────────── valid for 00 hr 45 min
        │    │    └──────────────────────── FIPS areas: 018035, 018057
        │    └───────────────────────────── event = TOR (Tornado Warning)
        └────────────────────────────────── originator = WXR (National Weather Service)
```

Two local lookups finish the job:

- **Event code** `TOR` → "Tornado Warning" (small built-in code→label table).
- **FIPS** `018035` → state `18` = Indiana, county `035` = **Delaware County**; `018057` →
  Hamilton County (bundled FIPS→county table).

Yielding, fully offline and with zero STT:

> **Tornado Warning** — Delaware County, IN — until 19:15 UTC (issued 18:30, KJY93)

Because we also have the affected-FIPS list, we can **filter to just our county** (drop the
alert if `018035` isn't present). The voice transcript then layers descriptive detail on top
(e.g. "…spotted near Yorktown moving northeast at 30 mph…").

Caveat: SAME only fires for **alert-class products** (warnings, watches, tests). Routine
forecasts and current conditions carry no SAME header and come from the transcript side.

## 10. Open questions

1. Old PC specs (CPU model, RAM) → pick whisper.cpp vs. Vosk and model size.
2. R-1630 external audio output level (line vs. speaker) → capture wiring.
3. Report granularity: dedup per **segment/product**, or per **whole loop**? (Leaning
   per-segment for robustness to partial updates.)
4. Structured-server scope: how far into forecast structuring is worth it vs. keeping prose
   products as plain text?
