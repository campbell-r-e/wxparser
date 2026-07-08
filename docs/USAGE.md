# wxparser usage guide

How to use everything the query layer exposes. The API is read-only, LAN-only,
stdlib HTTP/JSON, served by `wxparser-api` (default `:8080`). All timestamps are
ISO-8601 UTC (`2026-06-25T21:39:49Z`). Examples assume `H=http://<host>:8080`.

- [1. Quick start](#1-quick-start)
- [2. The snapshot — `/now`](#2-the-snapshot--now)
- [2a. EmComm formats — `/bulletin`, `/sitrep`, `/aprs`](#2a-emcomm-formats)
- [3. Discovery — `/cities`, `/city`, `/conditions`](#3-discovery)
- [4. History & pagination](#4-history--pagination)
- [5. Incremental sync — `/export`](#5-incremental-sync--export)
- [6. Live push — SSE `/stream` and the webhook](#6-live-push)
- [7. Health & monitoring — `/health`](#7-health--monitoring)
- [8. Trust & the advisory/authoritative model](#8-trust--the-advisoryauthoritative-model)
- [9. Operations — nightly cleanup jobs](#9-operations--nightly-cleanup-jobs)
- [10. Multi-transmitter](#10-multi-transmitter)
- [11. Trialing a better STT model (small.en)](#11-trialing-a-better-stt-model-smallen)

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

---

## 9. Operations — nightly cleanup jobs

Three `oneshot` systemd timers keep the store clean (all idempotent, safe to run by hand):

| Timer | When | Job |
|---|---|---|
| `wxparser-fixspelling` | 00:00 | merge STT-misheard city names → canonical (`fix_city_spellings.py`) |
| `wxparser-fixterms` | 00:30 | fix STT term mis-hearings in stored transcripts (`fix_stt_terms.py`) |
| `wxparser-prune` | 01:00 | drop non-home cities not heard in >`WX_STALE_PRUNE_HOURS` (24h) (`prune_stale_readings.py`) |

Run one by hand:

```bash
python3 deploy/prune_stale_readings.py     # ages out stale out-of-state readings
```

The prune only touches the "latest value" view (`city_conditions`); the append-only history
(`city_observations`) is never deleted, and a pruned city reappears the moment it's heard
again. One-off backfills (`fix_precip.py`, `fix_classify.py`) live in `deploy/` too.

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
transcript log, and port each.

---

## 11. Trialing a better STT model (small.en)

The model is just `WX_WHISPER_MODEL` — the pipeline sizes the encoder context per segment and
the vocabulary prompt is already wired, so trialing a bigger model is a download + one env var:

```bash
bash ~/whisper.cpp/models/download-ggml-model.sh small.en      # or small.en-q5_1 for a slow box
WX_WHISPER_MODEL=~/whisper.cpp/models/ggml-small.en.bin python3 -m wxparser.main
```

`small.en` is more accurate than the default `base.en-q5_1` (it would reduce the mis-hearings
the term/place correctors patch) but ~2-3× slower per segment. **Watch `/health`:** if
`pipeline.queue_depth` climbs and stays up, the transcriber is falling behind real airings —
revert to `base.en-q5_1`. Unlike `tiny.en`, `small.en` does not degenerate with the prompt, so
keep `WX_STT_PROMPT` on.
