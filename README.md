# wxparser

A fully-offline, radio-only local weather server. It continuously listens to the NOAA
Weather Radio (NWR) broadcast from **KJY93 Muncie, IN (162.425 MHz)**, transcribes the voice
loop, decodes SAME/EAS digital alerts, and turns it all into **structured, queryable weather
data** — sourced entirely from the radio, working even when the internet is down.

Audio is tapped from a dedicated weather radio (a Reecom R-1630) into the PC's mic/line-in, so
RF reception is handled in hardware and the entire software stack stays MIT-licensed and
permissive.

**Fully offline at runtime:** no network calls of any kind. Transcription (whisper.cpp) and
SAME decoding run locally, lookup tables (FIPS→county) are bundled, and the data store
(PostgreSQL) runs on the same box. Every dependency is permissively licensed — notably the
Postgres driver is `pg8000` (pure-Python, **BSD**) rather than the LGPL `psycopg`, and audio
capture is the `arecord` subprocess (a GPL tool used across a process boundary, never linked).

## How it works

NWR replays the same loop of products every few minutes until NWS updates content, and the
audio is deterministic TTS. wxparser fingerprints the audio to detect repeats and runs
speech-to-text **only on genuinely novel** segments, so it keeps up even on weak hardware
(it's running on a 2-core Core2 Duo).

```
radio line-out → mic-in → arecord (continuous PCM)
   │
   ├── SAME monitor ───────────► detect AFSK burst → decode header → typed alert  (no STT)
   │
   └── VAD segmenter → audio fingerprint → novelty gate
          repeat ─► drop (no STT)
          novel  ─► STT worker (whisper.cpp) → text dedup
                       ├─► transcript report (JSONL)
                       ├─► per-(city,condition) readings  (voted)
                       └─► forecast periods
                                      ↓
                         PostgreSQL ──► LAN-only HTTP/JSON query API
```

Two threads decouple capture from the slower-than-real-time transcriber: a **producer**
(capture → segment → fingerprint → gate, plus the SAME monitor) and an **STT worker** that
drains a queue. Novel segments queue up while a fresh product airs and drain during the long
stretches where the loop just repeats.

### Components (`wxparser/`)

| Module | Role |
|---|---|
| `capture.py` | persistent `arecord` → frames / WAVs; retries on transient device-busy |
| `segment.py` | energy-VAD segmentation (splits on inter-sentence silence) |
| `fingerprint.py` | numpy mel-spectral fingerprint + cosine **novelty gate** |
| `stt.py` | whisper.cpp `whisper-cli` wrapper (dynamic `--audio-ctx`, greedy decode) |
| `dedup.py` | text-level dedup + `supersedes` update chains |
| `extract.py` | multi-city current-conditions + forecast extraction, repeat-voting |
| `same.py` | SAME AFSK decoder + FIPS/event lookups + live burst monitor |
| `db.py` | PostgreSQL store (pg8000) |
| `api.py` | stdlib-http LAN query API |
| `main.py` | wiring + service loop |
| `data/` | bundled `fips.json` (US counties) + SAME event/originator codes |

## Data products

- **Transcripts** — timestamped JSON reports (`transcripts/reports.jsonl`), deduped against the
  repeating loop; updates link to the report they `supersede`.
- **Current conditions** — temp / dewpoint / humidity / pressure / wind / sky, extracted from
  the voice and **majority-voted across repeats** to harden numbers against STT slips. Stored
  **per city**: the station's primary city (Muncie) gets the full set; other cities named in
  the broadcast (the "Nearby …" list and regional temp roundup) get temperature.
- **Forecast** — zone-forecast periods (highs/lows, precip %, sky) with computed
  `valid_from`/`valid_to`, tagged with the area they cover.
- **Alerts** — two layers. The **SAME header** is decoded straight from the audio (event,
  FIPS→county areas, valid time) with **zero STT** — instant and reliable (validated on a real
  over-the-air Required Weekly Test). The **spoken narrative** that follows is transcribed and
  parsed into structured detail (expiry time, storm motion, threats, locations, spotter
  activation), then **linked back to the SAME alert** by capture-time window — so a query
  returns both *what* was issued and *what was said* about it.

## Data model (PostgreSQL, db `wxparser`)

- `city_observations` — long-format history: one row per `(captured_at, city, condition)` with
  `value_num`/`value_text` and vote provenance.
- `city_conditions` — latest value + **cumulative sightings** per `(city, condition)`. The
  sightings counter lets the API hide one-off STT-garbage city names.
- `forecasts` — one row per `(issued_at, city, period)` with `valid_from`/`valid_to`,
  `high_f`/`low_f`/`precip_pct`/`sky`. Append-only issuances → full forecast history.
- `alerts` — decoded SAME alerts with `expires_at` for active-alert queries.
- `alert_details` — structured fields parsed from a spoken warning/statement transcript
  (`until_text`, `motion`, `threats`, `locations`, `spotter_activation`), keyed by the
  transcript's report id and linked to `alerts` by capture-time window.

Timestamps are `timestamptz`, JSON columns are `jsonb`. Because forecasts store their valid
window and readings are timestamped, **"what did we forecast for a day vs. what actually
happened?"** is a SQL join.

## Query API (LAN-only)

Generic and city-agnostic — one endpoint per condition, returning every city that reports it:

```
GET /conditions                  → available conditions (index)
GET /conditions/{condition}      → every city's latest value (temperature, humidity,
                                   pressure, dewpoint, wind, sky, ...); accepts friendly
                                   names or stored keys (temperature_f, humidity_pct, ...)
GET /conditions/history?condition=&city=&from=&to=&limit=
                                 → historical readings between two times
GET /forecast                    → latest forecast for all heard cities/areas
GET /forecast/history?from=&to=&city=
                                 → historical forecast predictions between dates
GET /transcripts?from=&to=&q=&product=&limit=
                                 → raw transcript records (newest first); q= is a
                                   case-insensitive text search, product= filters on
                                   product_type (current_conditions, zone_forecast, ...)
GET /alerts/active               → SAME alerts not yet expired; each carries a
                                   "spoken" list linking the structured details
                                   parsed from its narrative (?details=0 to skip)
GET /alerts/details?from=&to=    → structured spoken-warning details on their own
                                   (until, motion, threats, locations, spotter flag)
GET /health                      → liveness + counts
```

`from`/`to` are ISO-8601 (`2026-06-24T12:00:00Z`), inclusive. The condition endpoints only
surface a city once it's been **heard ≥2 times** (`WX_MIN_SIGHTINGS`, override per request
with `?min=`) so STT-garbage city names are suppressed; `/conditions/history` keeps the raw
data. Served from PostgreSQL over the LAN, e.g. `curl http://<host>:8080/conditions/temperature`.
`/transcripts` reads the raw JSONL transcript log directly (e.g.
`curl 'http://<host>:8080/transcripts?product=tornado_warning&limit=20'`).

## Deploy

```bash
deploy/setup-postgres.sh                 # one-time: install + init PostgreSQL, role + dbs
sudo cp deploy/*.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now wxparser wxparser-api
sudo firewall-cmd --permanent --add-port=8080/tcp && sudo firewall-cmd --reload
```

- `wxparser.service` — capture → transcribe → dedup → store (Restart=always, after
  `postgresql`/`sound`).
- `wxparser-api.service` — the LAN query API (reads the same DB).

Run directly for development:

```bash
python3 -m wxparser.main      # capture pipeline
python3 -m wxparser.api       # query API
```

## Configuration (env vars)

All settings live in `wxparser/config.py` and are env-overridable. Common ones:

| Variable | Default | Purpose |
|---|---|---|
| `WX_STATION` / `WX_PRIMARY_CITY` | `KJY93` / `Muncie` | station + home city |
| `WX_ALSA_DEVICE` | `plughw:0,0` | capture device |
| `WX_WHISPER_BIN` / `WX_WHISPER_MODEL` | `~/whisper.cpp/...` | STT binary + model |
| `WX_WHISPER_THREADS` | `2` | STT threads |
| `WX_STT_PROMPT` | `` (off) | whisper vocabulary-bias prompt for local place names; **off by default** — `tiny.en` degenerates with any prompt, enable only on `base.en`/`small.en` |
| `WX_FP_SIMILARITY` | `0.97` | novelty-gate repeat threshold |
| `WX_VAD_DBFS` | `-40` | VAD speech threshold |
| `WX_MIN_SIGHTINGS` | `2` | API: min times a city is heard before surfacing |
| `WX_PG_HOST/PORT/DATABASE/USER` | `127.0.0.1/5432/wxparser/wxparser` | Postgres (local trust) |
| `WX_API_HOST` / `WX_API_PORT` | `0.0.0.0` / `8080` | API bind |

## Tests

Run against the `wxparser_test` PostgreSQL database (created by `setup-postgres.sh`):

```bash
python3 -m tests.test_same      # SAME encode/decode round-trip (clean + noisy)
python3 -m tests.test_extract   # multi-city extraction + repeat-voting
python3 -m tests.test_db        # PostgreSQL store + history + sightings filter
```

## License

[MIT](LICENSE)
