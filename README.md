# wxparser

A fully-offline, radio-only local weather server. It continuously listens to the NOAA
Weather Radio (NWR) broadcast from **KJY93 Muncie, IN (162.425 MHz)**, transcribes the voice
loop, decodes SAME/EAS digital alerts, and turns it all into **structured, queryable weather
data** — sourced entirely from the radio, working even when the internet is down.

Audio is tapped from a dedicated weather radio (a Reecom R-1630) into the PC's mic/line-in, so
RF reception is handled in hardware and the entire software stack stays MIT-licensed and
permissive.

Everything region-specific — the station callsign/frequency, home city, whisper vocabulary
prompt, and place-name corrections — lives in a **station profile** (a JSON file selected with
`WX_PROFILE`), so covering a different NWR transmitter is a drop-in profile, not a code edit
(see [Station profiles](#station-profiles)).

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
                       ├─► raw transcript report (the immutable source of truth)
                       ├─► per-(city,condition) readings  (voted)
                       └─► forecast periods
                                      ↓
                         PostgreSQL ──► LAN-only HTTP/JSON query API
```

Two threads decouple capture from the slower-than-real-time transcriber: a **producer**
(capture → segment → fingerprint → gate, plus the SAME monitor) and an **STT worker** that
drains a queue. Novel segments queue up while a fresh product airs and drain during the long
stretches where the loop just repeats.

### Architecture: two services, one database

wxparser is two processes around one PostgreSQL database, and the database is the **only
interface between them** — there is no service-to-service RPC anywhere:

```
wxparser.service (the WRITE side — sole writer)
   capture → STT → extract → vote
   │  writes: raw_reports (immutable transcript store, the source of truth),
   │          the structured tables projected from it,
   │          and the pipeline_health heartbeat row
   ▼
PostgreSQL ──── the contract ────► wxparser-api.service (the READ side — read-only)
```

That shape is CQRS with an event-sourcing flavor: the structured serving tables are a
**pure projection** of `raw_reports`, and `wxparser.reprocess` rebuilds them by replaying
the raw store — so an improved correction table or extraction regex applies retroactively
to the whole record. Even the pipeline's liveness heartbeat travels through the DB (the
`pipeline_health` row), so `/health` reports the truth when the API runs on a different
machine than the capture box (`health.json` remains as the same-box fallback and for the
AGC timer).

The practical consequence: the tiers separate cleanly onto up to three machines — the
radio/pipeline box (the only one that needs a sound card and whisper.cpp), a PostgreSQL
box, and one or more API boxes — with nothing but `WX_PG_*` env vars pointing them at each
other. See [`docs/DEPLOY.md` §13](docs/DEPLOY.md#13-splitting-across-three-machines).
The default deployment remains everything on one box.

### Components (`wxparser/`)

| Module | Role |
|---|---|
| `capture.py` | persistent `arecord` → frames / WAVs; retries on transient device-busy |
| `segment.py` | energy-VAD segmentation (coalesces to product-level on inter-product silence) |
| `fingerprint.py` | numpy mel-spectral fingerprint + cosine **novelty gate** |
| `enhance.py` | optional pre-STT DSP chain (mains-hum notches + low-pass + spectral subtraction), off by default (`WX_STT_ENHANCE`) |
| `stt.py` | whisper.cpp `whisper-cli` wrapper (`small.en-q5_1`, dynamic `--audio-ctx`, greedy decode, vocabulary prompt, repetition-loop guard) |
| `dedup.py` | text-level dedup + `supersedes` update chains |
| `extract.py` | multi-city current-conditions + forecast + almanac extraction, repeat-voting, spoken-alert detail parsing |
| `pipeline.py` | the shared transcript → structured-data step (live worker and offline reprocess both call it, so they can never drift) |
| `same.py` | SAME AFSK decoder + FIPS/event lookups + live burst monitor |
| `store.py` | report building + product classification (reports land in `raw_reports`, the immutable store) |
| `db.py` | PostgreSQL store (pg8000) + query readers |
| `api.py` | stdlib-http LAN query API (snapshot, history, export, SSE, EmComm formats) |
| `verify.py` | forecast-vs-observed scoring over the full record (the `/verify` endpoint) |
| `trust.py` | STT trust scoring (advisory vs authoritative) |
| `health.py` | pipeline heartbeat (write-through to the `pipeline_health` DB row + `health.json`) + fail-loud status for `/health` |
| `notify.py` | opt-in outbound webhook |
| `formats.py` | EmComm bulletin / sitrep / APRS renderers |
| `reprocess.py` | rebuild the structured DB as a pure projection of the raw transcript store |
| `profile.py` / `profiles/` | station-profile loader + the bundled KJY93 profile |
| `main.py` | wiring + service loop |
| `data/` | bundled `fips.json` (US counties), SAME event/originator codes, `place_names.py` STT mis-hearing corrections |

## Data products

- **Transcripts** — timestamped JSON report docs in the `raw_reports` table (the immutable
  source of truth everything else is derived from), deduped against the repeating loop;
  updates link to the report they `supersede`.
- **Current conditions** — temp / dewpoint / humidity / pressure / wind / sky, extracted from
  the voice and **majority-voted across repeats** to harden numbers against STT slips. Stored
  **per city**: the station's primary city (Muncie) gets the full set; other cities named in
  the broadcast (the "Nearby …" list and regional temp roundup) get temperature. City names are
  **auto-corrected at extraction time** (`data/place_names.py`) so the store never sees STT
  mis-hearings (e.g. "Monthsy"→Muncie, "Deepan"→Dayton).
- **Forecast** — zone-forecast periods (highs/lows, precip %, sky) with computed
  `valid_from`/`valid_to`, tagged with the area they cover. Staleness is judged by the last
  time the forecast **aired**, not the last time its content changed.
- **Almanac** — the climate-recap segment (year-to-date precipitation and departure,
  sunrise/sunset, degree days), voted and trust-annotated like conditions.
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
GET /bulletin?city=              → plain-text read-on-air net bulletin (EmComm/SKYWARN)
GET /sitrep?city=                → plain-text situation report (Winlink-pasteable/printable)
GET /aprs?city=&format=text      → APRS weather report + alert bulletins (RF beacon)
GET /almanac                     → the day's almanac (YTD precip + departure, sunrise/
                                   sunset, degree days), voted + trust-annotated
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
GET /forecast                    → latest forecast for all heard cities/areas; each
                                   issuance carries age_minutes (since issued) plus
                                   confirmed_age_minutes (since it last AIRED, changed
                                   or not) — stale is judged on the latter
GET /forecast/history?from=&to=&city=&limit=&offset=
                                 → historical forecast predictions between dates (paginated)
GET /verify                      → forecast accuracy scored against what this station
                                   later observed, over the whole record (temp bias/MAE
                                   by lead day, sky agreement, Brier-scored rain PoPs)

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

Bulk / sync / live
GET /export?since=&limit=        → incremental watermark feed of every store
                                   (observations, forecasts, alerts, alert_details,
                                   almanac, transcripts) captured after `since`
GET /stream                      → live push: Server-Sent Events as new alerts /
                                   observations / forecasts land (?since= replays first)
GET /health                      → pipeline liveness + counts (HTTP 503 when degraded/down)
```

`from`/`to`/`since` are ISO-8601 (`2026-06-24T12:00:00Z`); `from`/`to` are inclusive, `since`
is exclusive. The condition endpoints only surface a city once it's been **heard ≥2 times**
(`WX_MIN_SIGHTINGS`, override per request with `?min=`) so STT-garbage city names are
suppressed; `/conditions/history` keeps the raw data. Served from PostgreSQL over the LAN,
e.g. `curl http://<host>:8080/now`. `/transcripts` and `/export` read the raw transcript
store (`raw_reports`) directly
(e.g. `curl 'http://<host>:8080/transcripts?product=tornado_warning&limit=20'`).

**Pagination** — the list endpoints return `{total, count, limit, offset, next_offset}`
alongside the data; page until `next_offset` is `null` to retrieve every row. **Incremental
sync** — `/export` returns `{next_since, more, ...}`; re-request with `since=next_since` while
`more` is true to drain the whole store losslessly (the primitive a mesh publisher or a mirror
uses). Both keep the answer complete — no result is ever silently capped.

## Deploy

> **Full step-by-step walkthrough — hardware, radio wiring, whisper.cpp, PostgreSQL,
> profiles, systemd, firewall, CD: [`docs/DEPLOY.md`](docs/DEPLOY.md)** — or run the
> interactive installer, once per machine, picking the role(s) each machine plays
> (`db` / `radio` / `api`, or all three for a single box): `bash deploy/install.sh`

```bash
deploy/setup-postgres.sh                 # one-time: install + init PostgreSQL, role + dbs
sudo cp deploy/*.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now wxparser wxparser-api
sudo firewall-cmd --permanent --add-port=8080/tcp && sudo firewall-cmd --reload
```

- `wxparser.service` — capture → transcribe → dedup → store (Restart=always, after
  `postgresql`/`sound`).
- `wxparser-api.service` — the LAN query API (reads the same DB).

Maintenance timers (all in `deploy/`, all idempotent — see
[`docs/USAGE.md` §9](docs/USAGE.md) and [`docs/DEVELOPMENT.md` §7](docs/DEVELOPMENT.md)):
`wxparser-agc` keeps the capture input level in the decoder's sweet spot every 3 minutes (an
analog level that drifts too quiet makes the box silently go deaf); `wxparser-fixspelling`,
`wxparser-fixterms`, and `wxparser-prune` run nightly store cleanup; `wxparser-deploy` is the
pull-based CD.

Run directly for development:

```bash
python3 -m wxparser.main      # capture pipeline
python3 -m wxparser.api       # query API
```

### Demo client

[`demo/wx.sh`](demo/README.md) is a one-file shell client for any wxparser node: with no
arguments it renders a live terminal dashboard (refreshing every 30 s, with forecast
issued/heard-on-air freshness), and it has subcommands for the main endpoints
(`demo/wx.sh bulletin`, `demo/wx.sh almanac`, …). Point it at a node with `WX_HOST` or a
gitignored `demo/.env`.

## Configuration (env vars)

All settings live in `wxparser/config.py` and are env-overridable. Common ones:

| Variable | Default | Purpose |
|---|---|---|
| `WX_PROFILE` | `kjy93_muncie` | station profile: a bundled name (`wxparser/profiles/<name>.json`) or a path to any `.json` |
| `WX_STATION` / `WX_PRIMARY_CITY` | from profile (`KJY93` / `Muncie`) | station + home city |
| `WX_TZ` | from profile (`America/Indiana/Indianapolis`) | station timezone — anchors `/verify`'s local-wall-clock windows |
| `WX_ALSA_DEVICE` | `plughw:0,0` | capture device |
| `WX_WHISPER_BIN` / `WX_WHISPER_MODEL` | `~/whisper.cpp/...ggml-small.en-q5_1.bin` | STT binary + model (see [STT model](#stt-model-smallen-q5_1-default-baseen-q5_1-fallback)) |
| `WX_WHISPER_THREADS` | `2` | STT threads |
| `WX_STT_PROMPT` | place names from profile (on) | whisper vocabulary-bias prompt; **must be `""` if you point `WX_WHISPER_MODEL` at `tiny.en`**, which degenerates with any prompt |
| `WX_STT_ENHANCE` | `0` (off) | pre-STT speech-enhancement DSP chain (`enhance.py`) — A/B it on a new deployment before trusting it |
| `WX_STT_CONF_FLOOR` | `0.5` | transcripts below this mean token-confidence are stored but never voted into conditions/forecast/almanac |
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
| `WX_HEALTH_NOVEL_STALE_MIN` | `60` | `/health` flags `degraded` after this many minutes with no novel segment (static/dead carrier) |
| `WX_WEBHOOK_URL` | `""` (off) | when set, POST each new SAME alert here (opt-in outbound push) |
| `WX_STREAM_POLL_S` | `3` | `/stream` (SSE) poll interval |
| `WX_STALE_PRUNE_HOURS` | `24` | nightly prune drops non-home cities not heard in this long |

### STT model (small.en-q5_1 default, base.en-q5_1 fallback)

The shipped default is **`small.en-q5_1`** — noticeably more accurate than `base.en` on the
proper nouns and decade words the correctors used to patch, at a real speed cost (~3.4× slower
per segment on the Core2 Duo). It keeps up only because the novelty gate drops most repeated
audio before STT ever runs. **Watch `/health`** on your hardware: if `pipeline.queue_depth`
climbs and stays up, the transcriber is falling behind real airings — fall back to the faster
model:

```bash
bash ~/whisper.cpp/models/download-ggml-model.sh base.en-q5_1
WX_WHISPER_MODEL=~/whisper.cpp/models/ggml-base.en-q5_1.bin python3 -m wxparser.main
```

The model is just `WX_WHISPER_MODEL` — the pipeline derives everything else (the encoder
context is sized per-segment, the vocabulary prompt is already wired, and `model_name`
self-labels from the path), so swapping models is a download + one env var, no code change.
(Unlike `tiny.en`, both `base.en` and `small.en` absorb the vocabulary prompt cleanly, so
leave `WX_STT_PROMPT` on.)

## Station profiles

Porting wxparser to another part of the country is a **profile, not a code edit**. A profile
JSON carries the station callsign + frequency, home city, timezone, the whisper vocabulary
prompt (your NWS office's counties/towns), and the place-name correction table for your
coverage area; the pipeline, the SAME decoder (national FIPS/event tables), and the extraction
patterns are all generic. Select one with `WX_PROFILE` — a bundled name or a path to any
`.json`:

```bash
WX_PROFILE=/etc/wxparser/wxk97_dayton.json python3 -m wxparser.main
```

To add a region: copy `wxparser/profiles/kjy93_muncie.json`, set
`station`/`frequency_mhz`/`primary_city`/`tz`, replace `stt_prompt` with your local
counties/towns, and seed `place_corrections` loosely — `deploy/propose_corrections.py` mines
the stored transcripts for consistent STT garbles so the table fills in from real airings over
time.

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

Each instance has its own capture device, Postgres database (raw + structured + heartbeat),
out_dir, and API port —
no shared state, no contention. A consumer (dashboard, mesh publisher) fans out to each
`:PORT/now` (or pages `/export?since=` per instance) and merges by `station`. Template the
systemd units per instance (`wxparser@.service`) or just set the env block per unit.

## Tests & CI

> **Developer guide (setup, test layout, coverage policy, CI/CD): [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)**

**100% line *and branch* coverage**, enforced. Tests run against the `wxparser_test`
PostgreSQL database; subprocesses (`arecord`, `whisper-cli`) are mocked, so only PostgreSQL is
needed:

```bash
pip install -e '.[test]'         # pytest + coverage
createdb wxparser_test           # once
coverage run -m pytest           # whole suite (244 tests), branch mode via .coveragerc
coverage report                  # fails under 100%
```

**CI** (`.github/workflows/ci.yml`) runs the suite on every push/PR across Python 3.11–3.12
with a Postgres service container, ruff error-lint, and **gates on 100% line + branch coverage**.
**CD** is pull-based (the box has no inbound access): `deploy/wxparser-deploy.timer` runs
`deploy/auto_deploy.sh` every 10 min, which fast-forwards `main`, re-runs the gated suite, and
restarts the services only if green — rolling back on failure.

## License

[MIT](LICENSE)
