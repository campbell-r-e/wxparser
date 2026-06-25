# wxparser

A fully-offline, radio-only local weather server. It continuously listens to the NOAA
Weather Radio (NWR) broadcast from **KJY93 Muncie, IN (162.425 MHz)**, transcribes the voice
loop, decodes SAME/EAS digital alerts, and turns it all into **structured, queryable weather
data** — sourced entirely from the radio, working even when the internet is down.

Audio is tapped from a dedicated weather radio (a Reecom R-1630) into the PC's mic/line-in, so
RF reception is handled in hardware and the entire software stack stays MIT-licensed and
permissive.

**Fully offline at runtime:** no network calls of any kind out of the box. Transcription
(whisper.cpp) and SAME decoding run locally, lookup tables (FIPS→county) are bundled, and the
data store (PostgreSQL) runs on the same box. (The only way out is opt-in: setting
`WX_WEBHOOK_URL` enables outbound alert POSTs; the inbound SSE `/stream` and the query API make
no outbound calls.) Every dependency is permissively licensed — notably the
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
| `segment.py` | energy-VAD segmentation (coalesces to product-level on inter-product silence) |
| `fingerprint.py` | numpy mel-spectral fingerprint + cosine **novelty gate** |
| `stt.py` | whisper.cpp `whisper-cli` wrapper (`base.en-q5_1`, dynamic `--audio-ctx`, greedy decode, vocabulary prompt) |
| `dedup.py` | text-level dedup + `supersedes` update chains |
| `extract.py` | multi-city current-conditions + forecast extraction, repeat-voting, spoken-alert detail parsing |
| `same.py` | SAME AFSK decoder + FIPS/event lookups + live burst monitor |
| `db.py` | PostgreSQL store (pg8000) |
| `api.py` | stdlib-http LAN query API |
| `main.py` | wiring + service loop |
| `data/` | bundled `fips.json` (US counties), SAME event/originator codes, `place_names.py` STT mis-hearing corrections |

## Data products

- **Transcripts** — timestamped JSON reports (`transcripts/reports.jsonl`), deduped against the
  repeating loop; updates link to the report they `supersede`.
- **Current conditions** — temp / dewpoint / humidity / pressure / wind / sky, extracted from
  the voice and **majority-voted across repeats** to harden numbers against STT slips. Stored
  **per city**: the station's primary city (Muncie) gets the full set; other cities named in
  the broadcast (the "Nearby …" list and regional temp roundup) get temperature. City names are
  **auto-corrected at extraction time** (`data/place_names.py`) so the store never sees STT
  mis-hearings (e.g. "Monthsy"→Muncie, "Deepan"→Dayton).
- **Forecast** — zone-forecast periods (highs/lows, precip %, sky) with computed
  `valid_from`/`valid_to`, tagged with the area they cover.
- **Alerts** — two layers. The **SAME header** is decoded straight from the audio (event,
  FIPS→county areas, valid time) with **zero STT** — instant and reliable (validated on a real
  over-the-air Required Weekly Test). The **spoken narrative** that follows is transcribed and
  parsed into structured detail (expiry time, storm motion, threats, locations, spotter
  activation), then **linked back to the SAME alert** by capture-time window — so a query
  returns both *what* was issued and *what was said* about it. When a SAME burst fires, the
  segments that follow get **priority in the STT queue**, so the warning narrative transcribes
  ahead of routine forecast/conditions backlog even when STT is busy.

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

> **Full usage guide with examples for every feature: [`docs/USAGE.md`](docs/USAGE.md)** —
> snapshot, pagination, incremental sync, live push (SSE + webhook), health/monitoring, the
> trust model, cleanup jobs, multi-transmitter, and the STT-model trial.

Generic and city-agnostic, with a one-call snapshot, full pagination, and an incremental
export so **every row in every store is reachable with no silent truncation**:

```
Snapshot & discovery
GET /now?city=                   → one call: a city's full current ob + the regional
                                   roundup + the latest forecast + active alerts
GET /cities                      → cities with data, each with first/last_seen + count
GET /city/{city}                 → every current condition for one city (the full ob)

Conditions
GET /conditions                  → available conditions (index)
GET /conditions/{condition}      → every city's latest value (temperature, humidity,
                                   pressure, dewpoint, wind, sky, ...); accepts friendly
                                   names or stored keys (temperature_f, humidity_pct, ...).
                                   Each reading carries age_minutes + a stale flag
                                   (older than WX_STALE_AFTER_MIN); ?fresh=1 hides stale,
                                   ?stale_after=N overrides the threshold
GET /conditions/history?condition=&city=&from=&to=&limit=&offset=
                                 → historical readings between two times (paginated)

Forecast
GET /forecast                    → latest forecast for all heard cities/areas, each
                                   issuance annotated with age_minutes + stale
GET /forecast/history?from=&to=&city=&limit=&offset=
                                 → historical forecast predictions between dates (paginated)

Transcripts
GET /transcripts?from=&to=&q=&product=&limit=&offset=
                                 → raw transcript records (newest first); q= is a
                                   case-insensitive text search, product= filters on
                                   product_type (current_conditions, zone_forecast, ...)

Alerts
GET /alerts/active               → SAME alerts not yet expired; each carries a
                                   "spoken" list linking the structured details
                                   parsed from its narrative (?details=0 to skip)
GET /alerts/history?from=&to=&event=&limit=&offset=
                                 → all SAME alerts, active + expired (?details=1 to link)
GET /alerts/details?from=&to=    → structured spoken-warning details on their own
                                   (until, motion, threats, locations, spotter flag)

Bulk / sync
GET /export?since=&limit=        → incremental watermark feed of every store
                                   (observations, forecasts, alerts, alert_details,
                                   transcripts) captured after `since`
GET /health                      → liveness + counts
```

`from`/`to`/`since` are ISO-8601 (`2026-06-24T12:00:00Z`); `from`/`to` are inclusive, `since`
is exclusive. The condition endpoints only surface a city once it's been **heard ≥2 times**
(`WX_MIN_SIGHTINGS`, override per request with `?min=`) so STT-garbage city names are
suppressed; `/conditions/history` keeps the raw data. Served from PostgreSQL over the LAN,
e.g. `curl http://<host>:8080/now`. `/transcripts` and `/export` read the raw JSONL transcript
log directly (e.g. `curl 'http://<host>:8080/transcripts?product=tornado_warning&limit=20'`).

**Pagination** — the list endpoints return `{total, count, limit, offset, next_offset}`
alongside the data; page until `next_offset` is `null` to retrieve every row. **Incremental
sync** — `/export` returns `{next_since, more, ...}`; re-request with `since=next_since` while
`more` is true to drain the whole store losslessly (the primitive a mesh publisher or a mirror
uses). Both keep the answer complete — no result is ever silently capped.

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
| `WX_WHISPER_BIN` / `WX_WHISPER_MODEL` | `~/whisper.cpp/...ggml-base.en-q5_1.bin` | STT binary + model (q5_1 = base.en accuracy at ~tiny speed) |
| `WX_WHISPER_THREADS` | `2` | STT threads |
| `WX_STT_PROMPT` | local place names (on) | whisper vocabulary-bias prompt; **must be `""` if you point `WX_WHISPER_MODEL` back at `tiny.en`**, which degenerates with any prompt |
| `WX_FP_SIMILARITY` | `0.97` | novelty-gate repeat threshold |
| `WX_VAD_MIN_SILENCE` / `WX_VAD_MAX_SEGMENT` | `1.0` / `28` | coalesce to product-level segments (fewer STT calls amortize model-load overhead) |
| `WX_ALERT_PRIORITY_WINDOW` | `120` | seconds after a SAME burst that captured segments jump the STT queue (warning narrative transcribes ahead of routine backlog) |
| `WX_STALE_AFTER_MIN` | `60` | conditions reading older than this is flagged `stale` |
| `WX_VAD_DBFS` | `-35` | VAD speech threshold |
| `WX_MIN_SIGHTINGS` | `2` | API: min times a city is heard before surfacing |
| `WX_PG_HOST/PORT/DATABASE/USER` | `127.0.0.1/5432/wxparser/wxparser` | Postgres (local trust) |
| `WX_API_HOST` / `WX_API_PORT` | `0.0.0.0` / `8080` | API bind |
| `WX_HEALTH_AUDIO_SILENT_MIN` | `5` | `/health` flags `degraded` after this many minutes of no audio (deaf radio) |
| `WX_HEALTH_HEARTBEAT_STALE_MIN` | `3` | `/health` flags `down` if the pipeline heartbeat is older than this |
| `WX_WEBHOOK_URL` | `""` (off) | when set, POST each new SAME alert here (opt-in outbound push) |
| `WX_STREAM_POLL_S` | `3` | `/stream` (SSE) poll interval |
| `WX_STALE_PRUNE_HOURS` | `24` | nightly prune drops non-home cities not heard in this long |

### Trialing a more accurate STT model (small.en)

The model is just `WX_WHISPER_MODEL` — the pipeline derives everything else (the encoder
context is sized per-segment, the vocabulary prompt is already wired, and `model_name`
self-labels from the path), so trialing a bigger model is a download + one env var, no code
change. `base.en-q5_1` is the shipped default; `small.en` is the next rung up and would *reduce*
the STT mis-hearings the term/place correctors patch (decade words, place names):

```bash
bash ~/whisper.cpp/models/download-ggml-model.sh small.en          # or small.en-q5_1 for the slow box
WX_WHISPER_MODEL=~/whisper.cpp/models/ggml-small.en.bin python3 -m wxparser.main
```

Tradeoff: more accurate, ~2-3× slower per segment. On a weak box keep the quantized variant
(`small.en-q5_1`) and watch `/health` — if `queue_depth` climbs and stays up, the transcriber
is falling behind real airings; revert to `base.en-q5_1`. (Unlike `tiny.en`, `small.en` does
**not** degenerate with the vocabulary prompt, so leave `WX_STT_PROMPT` on.)

## Multi-transmitter (run N instances + aggregate)

One instance = one transmitter. To cover several NWR stations, run **one instance per
transmitter**, each fully isolated by env vars, and aggregate at the API layer (every `/now`
and `/health` response carries its `station` so a fan-out consumer can label them):

```bash
# instance A — KJY93 (Muncie)
WX_STATION=KJY93 WX_PRIMARY_CITY=Muncie WX_ALSA_DEVICE=plughw:0,0 \
  WX_PG_DATABASE=wxparser_kjy93 WX_OUT_DIR=transcripts/kjy93 WX_API_PORT=8080 \
  python3 -m wxparser.main   # + a matching wxparser.api on :8080

# instance B — a second station on a second radio/sound card
WX_STATION=WXK42 WX_PRIMARY_CITY=Anderson WX_ALSA_DEVICE=plughw:1,0 \
  WX_PG_DATABASE=wxparser_wxk42 WX_OUT_DIR=transcripts/wxk42 WX_API_PORT=8081 \
  python3 -m wxparser.main   # + a matching wxparser.api on :8081
```

Each instance has its own capture device, Postgres database, transcript log, and API port —
no shared state, no contention. A consumer (dashboard, mesh publisher) fans out to each
`:PORT/now` (or pages `/export?since=` per instance) and merges by `station`. Template the
systemd units per instance (`wxparser@.service`) or just set the env block per unit.

## Tests

Run against the `wxparser_test` PostgreSQL database (created by `setup-postgres.sh`):

```bash
python3 -m tests.test_same      # SAME encode/decode round-trip (clean + noisy)
python3 -m tests.test_extract   # multi-city extraction + repeat-voting + forecast
python3 -m tests.test_db        # PostgreSQL store, history, pagination, export readers
python3 -m tests.test_health    # fail-loud pipeline health assessment
python3 -m tests.test_trust     # STT trust scoring
python3 -m tests.test_notify    # opt-in webhook push
```

## License

[MIT](LICENSE)
