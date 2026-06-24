# wxparser

A fully-offline, radio-only local weather server. It continuously listens to the NOAA
Weather Radio broadcast from **KJY93 Muncie, IN (162.425 MHz)**, transcribes the voice loop,
decodes SAME/EAS digital alerts, and turns it all into **structured, queryable weather data**
— sourced entirely from the radio, working even when the internet is down.

Audio is tapped from a dedicated weather radio (a Reecom R-1630), so RF reception is handled
in hardware and the entire software stack stays MIT-licensed and permissive.

**Fully offline:** no runtime network calls of any kind. Transcription (whisper.cpp) and SAME
decoding run locally; lookup tables (FIPS→county) are bundled.

## How it works

NWR replays the same loop of products every few minutes until NWS updates content, and the
audio is deterministic TTS. wxparser fingerprints the audio to detect repeats and runs
speech-to-text **only on genuinely new** segments, so it keeps up even on weak hardware.

```
radio line-out → mic-in → capture (arecord) ─┬→ VAD → audio-fingerprint novelty gate
                                              │     → transcribe novel segments (whisper.cpp)
                                              │     → text dedup → reports + observations + forecast
                                              └→ SAME monitor → decode AFSK burst → typed alerts
                                                            ↓
                                              SQLite store ──→ LAN-only HTTP/JSON query API
```

- **Transcripts** — timestamped JSON reports (`transcripts/reports.jsonl`), deduped against
  the repeating loop; updates link to what they supersede.
- **Current conditions** — temp / dewpoint / humidity / pressure / wind / sky, extracted from
  the voice and majority-voted across repeats to harden numbers against STT slips.
- **Forecast** — zone-forecast periods (highs/lows, precip %, sky) with computed valid
  windows.
- **Alerts** — SAME headers decoded straight from the audio (event, FIPS→county areas, valid
  time) with zero STT — instant and reliable.

## Query API (LAN-only)

```
GET /current        → latest voted current conditions
GET /forecast       → zone-forecast periods (with valid_from/valid_to)
GET /alerts/active  → SAME alerts not yet expired
GET /health         → liveness + counts
```

Because forecasts store `valid_from`/`valid_to` and observations are timestamped, the history
supports **"what did we forecast for a day vs. what actually happened?"** as a simple join.

## Run

```bash
python3 -m wxparser.main      # capture → transcribe → dedup → store
python3 -m wxparser.api       # serve the LAN query API
```

Or as services (see `deploy/`): `wxparser.service` (capture) and `wxparser-api.service`
(API). Configuration is via environment variables (see `wxparser/config.py`) — ALSA device,
whisper paths/threads, VAD/fingerprint thresholds, API host/port, etc.

## Tests

```bash
python3 -m tests.test_same      # SAME encode/decode round-trip
python3 -m tests.test_extract   # field extraction + repeat-voting
python3 -m tests.test_db        # SQLite store round-trip
```

## License

[MIT](LICENSE)
