# wxparser roadmap

Future-consideration backlog distilled from a skeptic's review of the system as
**software** (hardware/deployment concerns — redundancy, UPS, antenna — are the
integrator's responsibility, out of scope here).

Current grade as software: **engineering quality A−**, **emergency feature-
completeness ~A−** (was C+/B− — the push, fail-loud, and trust items below are now
built). Nothing here was an architecture flaw — the trust model (digital SAME
alerting is authoritative; STT is advisory enrichment) is sound.

## High leverage (most move the "useful in an emergency" grade)

- [x] **Outbound notification / push.** Pull side complete (`/now`, paginated
  history with totals, `/export?since=` watermark feed). Server-initiated delivery
  shipped: opt-in `WX_WEBHOOK_URL` POSTs each new SAME alert, and a LAN-safe SSE
  `/stream` pushes alerts/observations/forecasts to inbound consumers. _Optional
  remaining: an MQTT sink for brokered fan-out._
- [x] **Fail-loud health + watchdog.** The pipeline publishes a heartbeat
  (`out_dir/health.json`: last segment/novel/STT-ok/extraction, queue depth,
  capture restarts, STT errors); `/health` derives `ok` / `degraded` (deaf radio
  or wedged worker) / `down` (heartbeat stale) and returns **HTTP 503** on non-ok
  so a monitor can alarm on status code alone.
- [x] **STT trust/confidence layer.** Every transcribed reading is tagged
  `source:"stt", advisory:true` with a trust block (vote agreement × sightings,
  halved when stale → high/medium/low); SAME data is tagged authoritative. whisper
  doesn't expose token confidence, so trust is derived from the repeat-vote
  signals. _Could still fold avg_confidence in if a future model populates it._

## Platform / scale

- [x] **Multi-transmitter (documented pattern).** Run one instance per
  transmitter, each isolated by `WX_STATION`/`WX_ALSA_DEVICE`/`WX_PG_DATABASE`/
  `WX_OUT_DIR`/`WX_API_PORT`; `/now` and `/health` self-identify by `station` so a
  consumer fans out and merges. Documented in the README. _A first-class
  single-process multi-station mode remains optional future work._

## Honest ceiling (design truths, not bugs)

- It is a **structuring layer over one broadcast** — it can only know what was
  said, when it was said. Freshness tracks broadcast cadence, not real time.
- It will never be *the* authoritative life-safety source (the radio and the
  SAME stream are). Realistic best version: **the definitive offline, structured,
  queryable front end to a NWR transmitter**, with reliable digital alerting and
  advisory transcription + push — now substantially reached: the notification,
  fail-loud, and trust-layer items are built. What's left is polish (MQTT sink,
  first-class multi-station mode, an STT accuracy bump) rather than missing
  capability.

## Smaller follow-ups noted in passing

- [~] Extend `data/stt_terms.py` / `place_names.py` as new consistent mis-hearings
  surface — ongoing; substantially expanded (Chants→Chance, decade-word garbles,
  many place variants).
- [x] `small.en` trial — documented and wired (model is just `WX_WHISPER_MODEL`,
  prompt-safe unlike `tiny.en`); the actual switch stays a hardware call, watched
  via `/health` queue depth.
- [x] Age out very stale out-of-state city readings — `prune_stale_readings.py`
  drops non-home cities not heard in >24h nightly (history kept).
