# Changelog

All notable changes to wxparser are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release is also published on
[GitHub Releases](https://github.com/campbell-r-e/wxparser/releases) with the
same notes. Every version below passed CI: full test suite, 100% line + branch
coverage, and `ruff check .` clean.

## [1.0.11] — 2026-07-17

### Fixed

- **Forecast validity windows now use the station's local wall-clock.** 1.0.4
  fixed which local *day* a forecast period lands on, but the `valid_from` /
  `valid_to` **hours** were still emitted as UTC-of-that-day — ~4h early, so a
  consumer saw "Today" expire at 18:00Z (2 pm EDT), before the afternoon it
  forecasts. Boundaries are now built in the station zone and converted to UTC
  (06:00–18:00 EDT = 10:00Z–22:00Z), matching what NWS publishes. Callers
  without a timezone (and their tests) are unchanged. `/verify` keeps its own
  wider observed-extreme window on purpose.
- **The AGC no longer resurrects a stopped pipeline.** `wxparser-agc.service`
  dropped `Wants=wxparser.service`. The AGC timer fires every 3 min, and that
  `Wants` pulled a deliberately-stopped pipeline back up within one cycle — so
  `systemctl stop wxparser` for maintenance silently came back and thrashed
  against any manual `arecord`. `After=` is kept for boot ordering; the pipeline
  stays up on its own via `Restart=always`, and the AGC already makes no gain
  change when the heartbeat is stale.

## [1.0.10] — 2026-07-17

### Fixed

- **Conditions freeze — the core bug.** `/now` had been serving readings hours
  stale (10.5 h at worst, 14.6°F off NWS). Root cause: the mel-spectral audio
  fingerprint is **content-blind** — measured on 3,661 real repeat pairs, a true
  re-air one loop back (~13.5 min) and an adjacent *different* segment both score
  0.988 (separation +0.000). It measures "same TTS voice", not "same words", so
  the novelty gate's `similarity ≥ threshold → drop` was dropping the conditions
  ob at random, catching it only by luck.
- **STT backpressure replaces the fingerprint as the load cap.** The producer now
  sheds a routine segment once the STT queue holds `WX_STT_MAX_QUEUE` (default
  4); alert narratives are never shed and keep priority. STT runs at full
  capacity, the queue stays bounded, and a segment flows whenever there's room,
  so the ob reaches STT within a cycle or two. The content-aware text dedup drops
  duplicate transcripts downstream — the job the audio fingerprint was wrongly
  being asked to do. Verified live against NWS: 76°F vs 75.2°F, 3 min fresh.

### Added

- `shed` counter in the pipeline heartbeat, alongside `novel` / `repeat`.

## [1.0.9] — 2026-07-17

> Superseded by 1.0.10.

### Changed

- Raised `WX_FP_SIMILARITY` 0.97 → 0.995 to pass more segments (STT had ~8×
  headroom at the old 9% pass rate). An offline simulation predicted ~44% pass;
  **live it passed ~100%** — nothing ever reached 0.995 — and the STT queue ran
  away. The prediction failed because the sim replayed dense history the live
  45-min TTL never builds. The real fix (backpressure) landed in 1.0.10; the
  0.995 default stayed as a now-harmless value.

## [1.0.8] — 2026-07-16

### Added

- **Opt-in fingerprint dump for offline gate tuning** (`WX_FP_DUMP`). Appends
  every segment's fingerprint — gated or not — at ~4 KB/segment, so the gate's
  similarity metric can be judged against *real* repeats across many ~13.5-min
  loop cycles without taking the radio offline. Fixed-width binary records (a
  float32 vector's bytes contain newlines and commas, so any delimiter would
  corrupt). No behavior change unless set. This is what produced the evidence
  behind 1.0.9–1.0.10.

## [1.0.7] — 2026-07-16

### Fixed

- **`/health` staleness keyed on the ob's canary, not an aggregate.** 1.0.6
  aggregated `last_seen` across every condition, which hid the very failure it
  was meant to catch — one `sky` reading landing reset the clock while
  temperature sat 642 min stale. It now keys on the primary city's temperature,
  which airs every cycle.

## [1.0.6] — 2026-07-16

### Added

- **`/health` now watches the product, not just the plumbing.** Every prior
  signal (heartbeat, audio, STT, queue) described the pipeline; none asked
  whether anything reached the store. `/health` now reports `degraded` (HTTP
  503) past `WX_HEALTH_READINGS_STALE_MIN` (default 180) when conditions stop
  landing, with a `readings_stale_min` field. A fresh/empty store is not a fault;
  stale data never softens an existing `down`. Caught a real 10.5 h flatline that
  had been reporting "all signals nominal".

## [1.0.5] — 2026-07-16

### Fixed

- **Novelty-gate history now expires in wall-clock time** (`WX_GATE_TTL_MIN`,
  default 45). The gate only remembers what it passes (~12/h), so a 400-deep
  history reached back over a day, and NWR's hourly conditions re-read (same
  audio, new numbers) was suppressed indefinitely — freezing `/now`. (A correct
  fix for a real problem, but the deeper content-blindness of the fingerprint was
  not addressed until 1.0.10.)

## [1.0.4] — 2026-07-16

### Fixed

- **Current-conditions vote window bounded in broadcast time, not transcript
  count.** The novelty gate makes transcripts arrive in bursts, so a fixed-length
  vote pool spanned hours and a morning value out-voted the current ob (+9.2°F
  seen against KMIE). Now bounded to `WX_VOTE_STALE_MIN` (default 45) minutes of
  broadcast time; reprocess still replays deterministically.
- **Forecast periods no longer filed a week out.** `period_window` resolved
  weekday names off the UTC day; a period aired at 10 pm Wednesday local (02:00Z
  Thursday) scored "Thursday" as +7 days. The day now resolves in the station
  zone. (Window *hours* were corrected later, in 1.0.11.)

## [1.0.3] — 2026-07-15

### Changed

- PEP 8 compliance pass: written line-length policy (99), lint gate widened to
  full pycodestyle E/W, public interface for `stt.py` junk sets, docstrings on
  all public `db.py` / `store.py` callables.

## [1.0.2] — 2026-07-15

### Changed

- Clean-architecture pass: lazy config, uniform flags, `PipelineState`, tunable
  knobs; packaging-output dirs ignored.

## [1.0.1] — 2026-07-15

### Fixed

- Post-1.0.0 fixes and packaging cleanup.

## [1.0.0] — 2026-07-15

### Added

- Initial release: fully-offline, radio-only local weather server. Listens to a
  NOAA Weather Radio broadcast, transcribes the voice loop (whisper.cpp), decodes
  SAME/EAS digital alerts, and serves structured, queryable weather data over a
  LAN HTTP/JSON API backed by PostgreSQL.

[1.0.11]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.11
[1.0.10]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.10
[1.0.9]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.9
[1.0.8]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.8
[1.0.7]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.7
[1.0.6]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.6
[1.0.5]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.5
[1.0.4]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.4
[1.0.3]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.3
[1.0.2]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.2
[1.0.1]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.1
[1.0.0]: https://github.com/campbell-r-e/wxparser/releases/tag/v1.0.0
