# wxparser usage guide

How to use everything the query layer exposes. The API is read-only, LAN-only,
stdlib HTTP/JSON, served by `wxparser-api` (default `:8080`). All timestamps are
ISO-8601 UTC (`2026-06-25T21:39:49Z`). Examples assume `H=http://<host>:8080`.

- [1. Quick start](#1-quick-start)
- [2. The snapshot — `/now`](#2-the-snapshot--now)
- [2a. EmComm formats — `/bulletin`, `/sitrep`, `/aprs`](#2a-emcomm-formats)
- [3. Discovery — `/cities`, `/city`, `/conditions`](#3-discovery)
- [3a. Almanac — `/almanac`](#3a-almanac--almanac)
- [4. History & pagination](#4-history--pagination)
- [5. Incremental sync — `/export`](#5-incremental-sync--export)
- [6. Live push — SSE `/stream` and the webhook](#6-live-push)
- [6a. Forecast verification — `/verify`](#6a-forecast-verification--verify)
- [7. Health & monitoring — `/health`](#7-health--monitoring)
- [8. Trust & the advisory/authoritative model](#8-trust--the-advisoryauthoritative-model)
- [9. Operations — maintenance timers & the reprocess toolkit](#9-operations--maintenance-timers--the-reprocess-toolkit)
- [10. Multi-transmitter](#10-multi-transmitter)
- [11. STT model selection](#11-stt-model-selection)

---

## 1. Quick start

The whole current picture in one call:

```bash
curl -s $H/now | jq .
```

Want machine-readable discovery of every route? `GET /` returns the endpoint list.
Liveness for a monitor: `GET /health` (HTTP 200 when healthy, 503 when not).

---

## 2. The snapshot — `/now`

`GET /now?city=Muncie` is the default "give me the weather" call. One request returns
the home city's full current observation, the regional roundup, the latest forecast,
and any active alerts — already annotated with freshness and trust.

```jsonc
{
  "generated_at": "2026-06-25T21:45:43Z",
  "station": "KJY93",                 // which transmitter (for multi-instance fan-out)
  "city": "Muncie",
  "conditions": [                     // the home city's full ob, one row per field
    { "condition": "temperature_f", "value": 81, "age_minutes": 4.1, "stale": false,
      "source": "stt", "advisory": true, "trust": 1.0, "confidence": "high",
      "votes": 2, "total": 2, "sightings": 35 }
    // humidity_pct, pressure_in, pressure_trend, wind, wind_speed_mph, sky ...
  ],
  "roundup": [                        // nearby cities (temperature only)
    { "city": "Anderson", "value": 78, "age_minutes": 10.0, "stale": false, "trust": 1.0 }
  ],
  "forecast": [
    { "city": "Muncie", "issued_at": "2026-06-25T21:39:49Z", "age_minutes": 6.0,
      "stale": false, "source": "stt", "advisory": true,
      "periods": [ { "period": "Tonight", "low_f": 61, "precip_pct": 50,
                     "sky": "mostly cloudy", "valid_from": "...", "valid_to": "..." } ] }
  ],
  "alerts": []                        // active SAME alerts, each with linked "spoken" details
}
```

- `?city=Anderson` snapshots a different city (nearby cities carry only temperature).
- `?min=1` lowers the sightings filter (default 2 — see §3); `?stale_after=30` overrides
  the staleness threshold (default 60 min); `?fresh=1` drops stale rows.
- Forecast freshness has two clocks: `age_minutes` counts from `issued_at`, which only
  moves when the voted **content changes**; `confirmed_age_minutes` counts from
  `last_confirmed_at`, the newest time the forecast **aired** (changed or not). `stale` is
  judged on the latter — an unchanged forecast the station keeps re-airing stays fresh,
  one the station stopped airing goes stale even if it never changed.

---

## 2a. EmComm formats

Operator-ready renderings of the same snapshot, for amateur-radio emergency comms /
SKYWARN. All carry the authoritative-vs-advisory distinction so you never relay
transcribed STT as fact. Pass `?city=` to target a city (defaults to the home city).

### `/bulletin` — read-on-air net bulletin (`text/plain`)

```bash
curl -s $H/bulletin
```

```
WX BULLETIN -- KJY93 Muncie -- 2026-06-25 2159Z

** ACTIVE WARNINGS (SAME -- authoritative) **
  TORNADO WARNING -- Delaware County, IN -- until 2230Z  ** SPOTTERS ACTIVATED **

CURRENT (advisory -- transcribed from voice):
  Temp 80F  Sky sunny  Wind southwest at 15  Humidity 54%  Pressure 29.94 falling

FORECAST (advisory):
  Tonight: low 61, mostly cloudy, rain 50%
  ...
```

Warnings come first (authoritative SAME), with **SPOTTERS ACTIVATED** flagged when the
narrative requested it — net control reads it verbatim.

### `/sitrep` — Winlink-pasteable / printable situation report (`text/plain`)

```bash
curl -s $H/sitrep | tee sitrep.txt        # paste into Winlink, or print for the EOC wall
```

A fuller report: source/disclaimer header, active warnings (with spoken detail), labelled
current conditions (stale values marked `*`), the regional temperature roundup, and the full
forecast.

### `/aprs` — RF beacon strings

```bash
curl -s $H/aprs                 # JSON: { weather_report, bulletins }
curl -s "$H/aprs?format=text"   # plain lines, ready to beacon
```

```
_06252159c225s015g...t080h54b10139      ← APRS positionless weather report
:BLN1WX   :No active NWS warnings KJY93  ← APRS bulletin (one per active alert, body ≤67)
```

The weather report encodes wind direction (text→degrees), speed, temperature (°F), humidity,
and pressure (inHg→tenths-mb) in standard APRS form. Pipe the `text` form into your APRS
beacon/TNC. Each active SAME alert becomes a `:BLNnWX   :` bulletin line.

---

## 3. Discovery

| Call | Returns |
|---|---|
| `GET /cities` | every city with data, its condition count, and first/last_seen |
| `GET /city/{city}` | every current condition for one city (the full ob), trust-annotated |
| `GET /conditions` | the index of available conditions (temperature, humidity, …) |
| `GET /conditions/{condition}` | every city's latest value for that condition |

```bash
curl -s $H/conditions/temperature        # every city's temp, freshest data
curl -s $H/city/Muncie                   # Muncie's full observation
```

`{condition}` accepts friendly names (`temperature`, `humidity`, `pressure`, `dewpoint`,
`wind`, `sky`) or stored keys (`temperature_f`, `humidity_pct`, …). A city is only surfaced
once it's been **heard ≥2 times** (`WX_MIN_SIGHTINGS`), which suppresses one-off STT-garble
city names; override per request with `?min=1`.

---

## 3a. Almanac — `/almanac`

The climate-recap segment of the broadcast loop carries the day's almanac. It's
voted and trust-annotated like conditions (transcribed → `advisory`), and is also
embedded in `/now` under an `almanac` key.

```bash
curl -s $H/almanac
```

```jsonc
{ "generated_at": "...", "min_sightings": 2, "stale_after_min": 120,
  "almanac": [
    { "field": "precip_year_in",        "value": 17.39, "advisory": true, "trust": 0.83, ... },
    { "field": "precip_departure_in",   "value": -2.72 },   // signed: below normal
    { "field": "sunrise",               "value": "6:14 AM" },
    { "field": "sunset",                "value": "9:15 PM" },
    { "field": "normal_precip_week_in", "value": 1.10 },
    { "field": "heating_degree_days",   "value": 0 },
    { "field": "cooling_degree_days",   "value": 6 } ] }
```

Fields are sightings-gated and stale-flagged exactly like `/conditions`; history flows
through `/export` (an `almanac` section). Sunrise/sunset are text; the rest are numeric.

---

## 4. History & pagination

The history endpoints expose the full append-only record and **never silently truncate** —
each response carries paging metadata so you can retrieve every row.

```bash
# first page
curl -s "$H/conditions/history?condition=temperature&city=Muncie&limit=500&offset=0"
```

```jsonc
{ "condition": "temperature_f", "city": "Muncie",
  "readings": [ ... ],
  "total": 586, "count": 500, "limit": 500, "offset": 0, "next_offset": 500 }
```

Page until `next_offset` is `null`:

```bash
offset=0
while [ "$offset" != "null" ]; do
  page=$(curl -s "$H/conditions/history?condition=temperature&limit=1000&offset=$offset")
  echo "$page" | jq -c '.readings[]'
  offset=$(echo "$page" | jq '.next_offset')
done
```

Same pattern for `GET /forecast/history?from=&to=&city=&limit=&offset=` and
`GET /transcripts?from=&to=&q=&product=&limit=&offset=` (`q=` is a case-insensitive text
search; `product=` filters on `product_type` — `current_conditions`, `zone_forecast`,
`almanac`, `hazardous_weather_outlook`, …). `from`/`to` are inclusive ISO-8601.

---

## 5. Incremental sync — `/export`

`GET /export?since=<iso>` is the **lossless mirror / forward** primitive: it returns every
new row across *all* stores captured after `since`, in ascending time order, plus a
watermark to advance.

```jsonc
{ "since": "2026-06-25T20:00:00Z", "next_since": "2026-06-25T21:13:32Z", "more": false,
  "limit": 500,
  "observations": [ ... ], "forecasts": [ ... ], "alerts": [ ... ],
  "alert_details": [ ... ], "almanac": [ ... ], "transcripts": [ ... ] }
```

Drain the whole store by looping until `more` is false, advancing `since` each time:

```bash
since="2026-06-25T00:00:00Z"
while :; do
  resp=$(curl -s "$H/export?since=$since&limit=1000")
  echo "$resp" | jq '{observations:(.observations|length), forecasts:(.forecasts|length)}'
  more=$(echo "$resp" | jq -r '.more'); since=$(echo "$resp" | jq -r '.next_since')
  [ "$more" = "true" ] || break
done
```

This is exactly what a Meshtastic publisher or a backup mirror uses: persist `next_since`,
re-request from it next time, and you never miss or re-fetch a row. `since` is **exclusive**.

---

## 6. Live push

Two server-initiated paths. Both preserve the offline guarantee except the explicitly
opt-in webhook.

### SSE `/stream` (LAN-safe — consumers connect *in*)

```bash
curl -s -N "$H/stream"                 # live event stream; -N = no buffering
curl -s -N "$H/stream?since=2026-06-25T21:00:00Z"   # replay recent, then live
```

Emits Server-Sent Events as new data lands:

```
event: alert
data: {"id":"...","event":"TOR","event_label":"Tornado Warning", ...}

event: observation
data: {"city":"Muncie","condition":"temperature_f","value":81, ...}

event: forecast
data: {"city":"Muncie","period":"Tonight","low_f":61, ...}

: ping          ← keepalive every WX_STREAM_POLL_S seconds
```

Browser: `new EventSource('http://<host>:8080/stream')` then `es.addEventListener('alert', …)`.

### Webhook (opt-in, outbound)

Off by default. Set `WX_WEBHOOK_URL` and each new SAME alert is POSTed as JSON:

```bash
WX_WEBHOOK_URL=http://mesh-gateway.lan/wx-alert python3 -m wxparser.main
```

```jsonc
POST http://mesh-gateway.lan/wx-alert
{ "event": "alert",
  "data": { "id": "...", "captured_at": "...", "event": "TOR",
            "event_label": "Tornado Warning", "areas": [...], "counties": [...],
            "expires_at": "...", "station": "KJY93" } }
```

Fired on a daemon thread with a timeout (`WX_WEBHOOK_TIMEOUT`, default 5s); a slow or absent
endpoint never blocks capture, and a failed POST is logged but never crashes the pipeline.
Setting this URL is the **only** thing that makes the box reach outbound.

---

## 6a. Forecast verification — `/verify`

How good were the forecasts, scored against what this station later observed —
computed over the **entire stored record** on every request (it sharpens as the
archive grows). Highs/lows verify against the observed window extremes (local
wall-clock: day 06:00–21:00, night 18:00–08:30), sky against a 4-step
cloudiness ladder, and chance-of-rain against a daily rain series recovered by
differencing the almanac's year-to-date precipitation.

```jsonc
{ "city": "Muncie", "tz": "America/Indiana/Indianapolis",
  "temperature": { "high": {"n": 4999, "bias_f": 0.5, "mae_f": 3.1},
                   "low":  {"n": 4955, "bias_f": -4.6, "mae_f": 4.9},
                   "mae_by_lead_days": {"0": 3.2, "1": 3.4, "...": 0} },
  "sky":  { "n": 9994, "exact_pct": 43.0, "within_one_step_pct": 79.0 },
  "rain": { "days_measured": 12, "wet_days": 3, "total_in": 1.51,
            "brier_day_before": 0.2, "base_rate": 0.27, "brier_skill": -0.01,
            "scorecard": [ {"day": "2026-07-04", "pop_day_before": 60.0,
                            "rained": true, "inches": 1.15} ] } }
```

Notes: a negative low bias is expected (a zone forecast targets the coolest
rural spots; this is one warm ob site). `brier_skill` > 0 means the PoPs beat
always-guessing the base rate — it needs weeks of record before it stabilizes.
This endpoint re-scans every stored issuance per request, so poll it gently
(it is meant for a dashboard refresh, not a tight loop).

---

## 7. Health & monitoring

`GET /health` reflects the **actual pipeline state**, not just "the API is up" — the capture
process publishes a heartbeat the API reads.

```jsonc
{ "status": "ok",                      // ok | degraded | down
  "checks": ["all signals nominal"],
  "station": "KJY93",
  "heartbeat_age_min": 0.1, "audio_silent_min": 0.1, "last_stt_ok_min": 0.3,
  "last_novel_min": 2.4,
  "pipeline": { "last_segment_at": "...", "last_novel_at": "...", "last_stt_ok_at": "...",
                "last_extraction_at": "...", "segments": 412, "novel": 38, "repeat": 374,
                "queue_depth": 0, "capture_restarts": 0, "stt_errors": 0 },
  "conditions": 7, "cities": 19, "active_alerts": 0, "total_alerts": 3,
  "forecast_cities": 1, "almanac_fields": 7 }
```

| status | meaning | HTTP |
|---|---|---|
| `ok` | segments flowing, worker draining | 200 |
| `degraded` | audio silent for `WX_HEALTH_AUDIO_SILENT_MIN` (default 5) min — **deaf radio** — no novel segment for `WX_HEALTH_NOVEL_STALE_MIN` (default 60) min — **static/dead carrier** — or the STT worker is wedged (backlog with no recent success) | 503 |
| `down` | heartbeat older than `WX_HEALTH_HEARTBEAT_STALE_MIN` (default 3) min — the **capture process isn't running** | 503 |

Because non-ok returns **HTTP 503**, a dumb monitor alarms on status code alone:

```bash
curl -fsS $H/health >/dev/null || alert "wxparser unhealthy"
# or watch the queue for an STT model that's too slow:
curl -s $H/health | jq '.pipeline.queue_depth'
```

The heartbeat itself travels through the database (the `pipeline_health` row, upserted on
every segment), so `/health` works even when the API runs on a **different machine** than
the capture box; the `health.json` file in the pipeline's out_dir is the same-box fallback
and what the AGC timer reads. If the pipeline loses the database, the row goes stale and
`/health` reports `down` — which is the truth a monitor should act on.

---

## 8. Trust & the advisory/authoritative model

The system's trust model is explicit in the data: **digital SAME alerting is authoritative;
everything transcribed from the voice loop is advisory.** Every payload says which it is.

- **Transcribed** readings/forecasts carry `"source": "stt", "advisory": true`.
- **SAME** alerts carry `"source": "same", "authoritative": true`.

Transcribed *conditions* also carry a trust block derived from the repeat-vote signals
(whisper doesn't expose token confidence):

```jsonc
{ "value": 81, "votes": 2, "total": 2, "sightings": 35, "stale": false,
  "trust": 1.0, "confidence": "high", "agreement": 1.0 }
```

- `agreement` = `votes / total` — how strongly the majority vote agreed.
- `trust` = `agreement × min(1, sightings/6)`, **halved if `stale`** → `confidence`
  `high` (≥0.66) / `medium` (≥0.33) / `low`.

So a value heard many times with unanimous votes scores `high`; a one-off or a stale value
scores low. Rank or filter by `trust`/`confidence`, and always treat an `advisory` field as
enrichment — never as the life-safety source (that's the SAME alert / the radio itself).

Two more guards feed this model upstream. A transcript whose mean STT token-confidence falls
below `WX_STT_CONF_FLOOR` (default 0.5) is stored raw but **never voted** into
conditions/forecast/almanac. And a voted reading whose winning value holds less than
`WX_CONFIDENCE_MIN` (default 0.6) of the recent airings is listed in the payload's
`uncertain` array — the airings disagree, likely an STT mishear.

---

## 9. Operations — maintenance timers & the reprocess toolkit

Four `oneshot` systemd timers keep the input and the store healthy (all idempotent, safe to
run by hand):

| Timer | When | Job |
|---|---|---|
| `wxparser-agc` | every 3 min | keep the capture input level in the decoder's sweet spot — the analog level drifts, and a too-quiet feed silently goes deaf (`agc.py`; adjusts the ALSA mixer from the level the pipeline publishes, persists with `alsactl store`) |
| `wxparser-fixspelling` | 00:00 | merge STT-misheard city names → canonical (`fix_city_spellings.py`) |
| `wxparser-fixterms` | 00:30 | fix STT term mis-hearings in stored transcripts (`fix_stt_terms.py`) |
| `wxparser-reprocess` | 00:00 | rebuild the structured tables by replaying every stored transcript through the current corrections + extraction (`python3 -m wxparser.reprocess`). Runs with capture LIVE — see the unit comment for why. ~15 min for ~20k transcripts |
| `wxparser-prune` | 01:00 | drop non-home cities not heard in >`WX_STALE_PRUNE_HOURS` (24h) (`prune_stale_readings.py`) |

Run one by hand:

```bash
python3 deploy/prune_stale_readings.py     # ages out stale out-of-state readings
```

The prune only touches the "latest value" view (`city_conditions`); the append-only history
(`city_observations`) is never deleted, and a pruned city reappears the moment it's heard
again.

Because the raw transcripts are the source of truth and the structured tables are a pure
projection of them, improving a correction table or an extraction regex can be applied
**retroactively** to the whole record:

| Tool | Role |
|---|---|
| `deploy/reprocess.sh` (→ `wxparser.reprocess`) | rebuild the structured DB by replaying every stored transcript through the current pipeline |
| `deploy/propose_corrections.py` | mine the transcripts for consistent STT garbles and **propose** `stt_terms` corrections (review tool — no runtime effect) |
| `deploy/audit_data.py` | read-only data + transcript integrity audit; ends with `AUDIT: PASS|FAIL (<n> issues)` for monitoring |
| `deploy/revote_forecast.py` | one-off: recompute the latest forecast issuance from the consensus of recent airings |

---

## 10. Multi-transmitter

One instance = one transmitter. To cover several stations, run **one instance per
transmitter**, each isolated by env vars, and aggregate at the API layer — every `/now`
and `/health` carries its `station`:

```bash
WX_STATION=KJY93 WX_PRIMARY_CITY=Muncie WX_ALSA_DEVICE=plughw:0,0 \
  WX_PG_DATABASE=wxparser_kjy93 WX_OUT_DIR=transcripts/kjy93 WX_API_PORT=8080 \
  python3 -m wxparser.main          # + wxparser.api on :8080

WX_STATION=WXK42 WX_PRIMARY_CITY=Anderson WX_ALSA_DEVICE=plughw:1,0 \
  WX_PG_DATABASE=wxparser_wxk42 WX_OUT_DIR=transcripts/wxk42 WX_API_PORT=8081 \
  python3 -m wxparser.main          # + wxparser.api on :8081
```

A consumer fans out to each `:PORT/now` (or pages `/export?since=` per instance) and merges
by `station`. No shared state between instances — separate capture device, database,
out_dir, and port each.

---

## 11. STT model selection

The shipped default is **`small.en-q5_1`** — more accurate than `base.en` on the proper nouns
and decade words the correctors used to patch, but ~3.4× slower per segment. It keeps up
because the novelty gate drops most repeated audio before STT runs. The model is just
`WX_WHISPER_MODEL` — the pipeline sizes the encoder context per segment and the vocabulary
prompt is already wired, so swapping is a download + one env var.

**Watch `/health` on weaker hardware:** if `pipeline.queue_depth` climbs and stays up, the
transcriber is falling behind real airings — fall back to the faster model:

```bash
bash ~/whisper.cpp/models/download-ggml-model.sh base.en-q5_1
WX_WHISPER_MODEL=~/whisper.cpp/models/ggml-base.en-q5_1.bin python3 -m wxparser.main
```

Unlike `tiny.en`, both `base.en` and `small.en` absorb the vocabulary prompt cleanly, so keep
`WX_STT_PROMPT` on. After a model upgrade, `deploy/reprocess.sh` will NOT re-transcribe old
audio (the WAVs aren't kept) — the better model improves the record from that point forward.
