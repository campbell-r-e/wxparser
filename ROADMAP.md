# wxparser roadmap

Future-consideration backlog distilled from a skeptic's review of the system as
**software** (hardware/deployment concerns — redundancy, UPS, antenna — are the
integrator's responsibility, out of scope here).

Current grade as software: **engineering quality A−**, **emergency feature-
completeness C+/B−**. The gaps below are the path from B+ today toward A−/A.
Nothing here is an architecture flaw — the trust model (digital SAME alerting is
authoritative; STT is advisory enrichment) is sound. These are feature gaps.

## High leverage (most move the "useful in an emergency" grade)

- [ ] **Outbound notification / push (biggest gap).** Today it's a passive query
  API — consumers must poll. Add push so downstream systems/people are *told*:
  webhook POST on new SAME alert, a WebSocket/SSE stream of alerts+conditions,
  and/or MQTT. This is the single highest-value addition.
- [ ] **Fail-loud health + watchdog.** It can't currently report "I've gone
  deaf." Surface real liveness: audio-silent for N minutes, STT worker wedged,
  decode-error rate climbing, capture restarts. Make `/health` reflect actual
  pipeline state + expose a heartbeat so a monitor can alarm on degradation.
- [ ] **STT trust/confidence layer.** Known mis-hearings are corrected, but a
  *novel* warning's garble is unverifiable. Expose a per-field confidence/trust
  signal and clearly mark transcribed fields as **advisory** next to the
  authoritative SAME data. Partly present (vote counts, staleness, age) — needs
  to cohere into one trust score / explicit advisory tagging.

## Platform / scale

- [ ] **Multi-transmitter orchestration.** One instance = one station today.
  Either a first-class multi-station mode or a documented "run N instances +
  aggregate" pattern, so it's a platform rather than a single appliance.

## Honest ceiling (design truths, not bugs)

- It is a **structuring layer over one broadcast** — it can only know what was
  said, when it was said. Freshness tracks broadcast cadence, not real time.
- It will never be *the* authoritative life-safety source (the radio and the
  SAME stream are). Realistic best version: **the definitive offline, structured,
  queryable front end to a NWR transmitter**, with reliable digital alerting and
  advisory transcription + push. Current build is ~80% of that ceiling; the
  remaining ~20% is the notification, fail-loud, and trust-layer items above.

## Smaller follow-ups noted in passing

- [ ] Extend `data/stt_terms.py` as new consistent forecast-word mis-hearings
  surface (started with "Pies"→"Highs").
- [ ] Optional: move to `small.en` if/when hardware allows — the pipeline already
  has headroom and the vocabulary prompt is wired.
- [ ] Consider backfilling/aging out very stale out-of-state city readings rather
  than only flagging them.
