# wxparser development guide

Setup, tests, coverage policy, and the CI/CD pipeline. For *using* the running
system see [`USAGE.md`](USAGE.md); for standing up a node from scratch see
[`DEPLOY.md`](DEPLOY.md).

- [1. Local setup](#1-local-setup)
- [2. Running the tests](#2-running-the-tests)
- [3. Coverage policy (100% line + branch)](#3-coverage-policy)
- [4. Test layout](#4-test-layout)
- [5. CI](#5-ci)
- [6. CD — pull-based auto-deploy](#6-cd--pull-based-auto-deploy)
- [7. Deploy / ops scripts](#7-deploy--ops-scripts)
- [8. Package layout](#8-package-layout)

---

## 1. Local setup

Runtime deps are tiny and permissive (`numpy`, `pg8000`); the audio/STT tools
(`arecord`, `whisper-cli`) are subprocesses and are **mocked in tests**, so you
don't need them to run the suite — only PostgreSQL.

```bash
git clone https://github.com/campbell-r-e/wxparser && cd wxparser
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'          # numpy, pg8000 + pytest, coverage
createdb wxparser wxparser_test   # the store DB + the throwaway test DB (local trust)
```

The DB connection honours `WX_PG_*` env vars (default `127.0.0.1:5432`, user
`wxparser`, no password / local trust).

---

## 2. Running the tests

```bash
coverage run -m pytest            # whole suite (244 tests); branch mode via .coveragerc
coverage report -m                # per-file table; fails under 100%
coverage html && open htmlcov/index.html   # annotated source
pytest tests/test_extract.py -q   # a single module
```

`pytest` discovers every `test_*` function. `tests/conftest.py` has one autouse
fixture that resets the capture loop's `_STOP` global around each test so a
`run_live`/worker test can't leak shutdown state into the next.

---

## 3. Coverage policy

**100% line *and* branch coverage is enforced** (`.coveragerc`: `branch = True`,
`fail_under = 100`). Both sides of every conditional must be exercised — the CI
and the box's self-deploy both fail the build otherwise.

The only excluded code is genuine glue/defensive branches, each marked inline:

| Pragma | Where | Why |
|---|---|---|
| `# pragma: no cover` | `api.serve`/`api.main`, `main._on_alert`, SSE keepalive cadence, defensive reconnect/except paths, `stream_windows` | blocking server bootstrap, CLI entry, live-SAME-only callback, network/timing glue, Phase-1 dead code |
| `# pragma: no branch` | degenerate mel-bin guards, SAME demod-offset, "voter always has a sample", "forecast always has issued_at" | the off-branch is unreachable with real inputs |

When adding code, add the test for **both** branches; reach for a pragma only
when a branch is truly unreachable or pure I/O glue, and say why in the comment.

---

## 4. Test layout

| File | Covers |
|---|---|
| `test_extract.py` | multi-city conditions extraction, forecast parsing, repeat-voting |
| `test_db.py` | PostgreSQL store, history, pagination, alert/since readers |
| `test_same.py` | SAME encode/decode round-trip, FIPS lookup, the live monitor |
| `test_store.py` | product classification, report building, raw-store query/sync/count |
| `test_dedup.py` | text normalization + rolling-window fuzzy dedup |
| `test_segment.py` | energy-VAD segmentation over synthetic frames |
| `test_fingerprint.py` | mel-spectral fingerprint + novelty gate |
| `test_stt.py` | whisper-cli wrapper (subprocess mocked) |
| `test_capture.py` | arecord command, WAV writing, framed streaming (mocked) |
| `test_api.py` | the HTTP handler end-to-end against a live server + test DB |
| `test_main.py` | pipeline helpers + a `--once` run with capture/STT mocked |
| `test_pipeline.py` | the shared transcript → structured-data step |
| `test_enhance.py` | the optional pre-STT DSP chain |
| `test_verify.py` | forecast-vs-observed scoring (`/verify`) |
| `test_profile.py` | station-profile loading + validation |
| `test_reprocess.py` | DB rebuild from the raw transcript store |
| `test_health.py` | fail-loud heartbeat + status assessment |
| `test_trust.py` | STT trust scoring |
| `test_notify.py` | opt-in webhook push |
| `test_formats.py` | EmComm bulletin / sitrep / APRS formats |
| `test_edges.py` / `test_branches.py` | edge-case and both-sides-of-branch fills |

---

## 5. CI

`.github/workflows/ci.yml` runs on every push and PR to `main`:

- matrix **Python 3.11 / 3.12**, with a **PostgreSQL 16 service container** (local-trust),
- creates `wxparser_test` (locally the test suite creates it itself on first run),
- **ruff** `check .` — full pycodestyle **E/W at line-length 99** plus the fatal F-classes;
  the two accepted house-style deviations (E702 compound statements, E402 deploy-script
  bootstraps) are documented ignores in `pyproject.toml`, so a plain ruff run is the gate,
- `coverage run -m pytest` then `coverage report` — **gated at 100% line + branch**.

A red build means a test failed or coverage dropped below 100%.

---

## 6. CD — pull-based auto-deploy

The weather box is LAN-only, so cloud runners can't reach it; deployment is
**pull-based** instead. `deploy/wxparser-deploy.timer` runs `deploy/auto_deploy.sh`
every 10 minutes, which:

1. `git fetch` — if `origin/main` is unchanged, exits (no-op);
2. fast-forwards `main` and ensures `wxparser_test` exists;
3. re-runs the **full suite with the 100% gate** in a persistent venv
   (`~/wxparser-testenv`);
4. on green → `systemctl restart wxparser wxparser-api`; on red → `git reset --hard`
   back to the previous commit and **leaves the running services untouched**.

So a bad push can never take the capture box down — it tests first and rolls back.
A change confined to `wxparser/api.py`/`verify.py`, tests, and docs restarts only the
API. On a split deployment (DEPLOY.md §13) each machine runs its own timer with
`WX_DEPLOY_SERVICES` naming the units it owns (default: both).
Activity is logged to `~/wxparser-deploy.log`. Install on the box with:

```bash
sudo cp deploy/wxparser-deploy.service deploy/wxparser-deploy.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now wxparser-deploy.timer
```

---

## 7. Deploy / ops scripts

Everything in `deploy/` is tracked and deployable via `git pull`:

| Unit / script | Role |
|---|---|
| `wxparser.service` | the capture → transcribe → store pipeline |
| `wxparser-api.service` | the LAN query API |
| `wxparser-agc.{service,timer}` | every 3 min — capture AGC: keep the input level in the decoder's sweet spot (`agc.py`) |
| `wxparser-fixspelling.{service,timer}` | nightly 00:00 — merge misheard city names (`fix_city_spellings.py`) |
| `wxparser-fixterms.{service,timer}` | nightly 00:30 — fix STT term mis-hearings in transcripts (`fix_stt_terms.py`) |
| `wxparser-prune.{service,timer}` | nightly 01:00 — age out stale out-of-state readings (`prune_stale_readings.py`) |
| `wxparser-deploy.{service,timer}` | every 10 min — the CD auto-deploy above |
| `reprocess.sh` | rebuild the structured DB by replaying the raw transcripts through the current pipeline |
| `propose_corrections.py` | mine transcripts for consistent STT garbles → proposed `stt_terms` entries (review tool) |
| `audit_data.py` | read-only integrity audit; final `AUDIT: PASS\|FAIL` line for monitoring |
| `revote_forecast.py` | one-off: recompute the latest forecast issuance from recent-airing consensus |
| `setup-postgres.sh` | one-time PostgreSQL install + role/db init |

Unit changes need `sudo cp …/*.service /etc/systemd/system/ && sudo systemctl daemon-reload`;
code-only changes just need the service restart (the CD does both automatically).

---

## 8. Package layout

| Module | Role |
|---|---|
| `capture.py` | persistent `arecord` → frames; retries on device-busy |
| `segment.py` | energy-VAD segmentation |
| `fingerprint.py` | mel-spectral fingerprint + cosine novelty gate |
| `enhance.py` | optional pre-STT DSP chain (off by default, `WX_STT_ENHANCE`) |
| `stt.py` | whisper.cpp `whisper-cli` wrapper + repetition-loop guard |
| `dedup.py` | text-level dedup + supersede chains |
| `extract.py` | conditions + forecast + almanac extraction, repeat-voting, alert-detail parsing |
| `pipeline.py` | shared transcript → structured-data step (live worker + reprocess) |
| `same.py` | SAME AFSK decoder + FIPS/event lookups + live burst monitor |
| `store.py` | report building + product classification (persists via `db.raw_reports`) |
| `db.py` | PostgreSQL store (pg8000) + all query readers |
| `api.py` | stdlib-http LAN query API (incl. snapshot, export, SSE, EmComm formats) |
| `verify.py` | forecast-vs-observed scoring over the full record (`/verify`) |
| `health.py` | pipeline heartbeat (write-through: `pipeline_health` row + `health.json`) + fail-loud status |
| `trust.py` | STT trust scoring (advisory vs authoritative) |
| `notify.py` | opt-in outbound webhook |
| `formats.py` | EmComm bulletin / sitrep / APRS renderers |
| `reprocess.py` | rebuild the structured DB as a projection of the raw transcripts |
| `profile.py` / `profiles/` | station-profile loader + bundled KJY93 profile (`WX_PROFILE`) |
| `main.py` | the producer/worker wiring + service loop |
| `config.py` | env-overridable settings |
| `data/` | bundled FIPS table, SAME codes, STT correction tables |
