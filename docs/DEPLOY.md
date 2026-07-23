# Deploying wxparser

From a bare Linux box to a running, self-maintaining NWR node. The reference
deployment is Fedora on a 2-core Core2 Duo with 4 GB RAM — anything newer is
comfortable; the pipeline was sized to keep up on weak hardware because the
novelty gate drops most repeated audio before STT runs.

For day-2 usage see [`USAGE.md`](USAGE.md); for the test/CI side see
[`DEVELOPMENT.md`](DEVELOPMENT.md).

> **Prefer to be asked questions instead?** [`deploy/install.sh`](../deploy/install.sh)
> is an interactive installer for **Fedora/RHEL (dnf) and Debian/Ubuntu/Pi OS (apt)**:
> run it on each machine, pick the role(s) that machine
> plays (**db / radio / api**), answer the prompts, and it does §2–§12 for you —
> packages, the clone, whisper.cpp, PostgreSQL (including network auth for a split
> deployment), `/etc/wxparser.env`, patched systemd units, firewall, and CD. It clones
> the repo itself, so it just needs to reach the new machine — copy it over from any
> existing clone (or, if the repo is public to you, fetch the raw file with `gh`/curl):
>
> ```bash
> scp deploy/install.sh newbox:   # from any machine with a clone
> ssh newbox bash install.sh
> ```
>
> The sections below are the manual path and the reference for what it does.

- [0. What you're deploying](#0-what-youre-deploying)
- [1. Hardware](#1-hardware)
- [2. OS packages](#2-os-packages)
- [3. Radio → sound card](#3-radio--sound-card)
- [4. whisper.cpp + model](#4-whispercpp--model)
- [5. PostgreSQL](#5-postgresql)
- [6. The application](#6-the-application)
- [7. Station profile](#7-station-profile)
- [8. systemd services](#8-systemd-services)
- [9. Firewall](#9-firewall)
- [10. Verify it's alive](#10-verify-its-alive)
- [11. Maintenance timers](#11-maintenance-timers)
- [12. Optional: pull-based CD](#12-optional-pull-based-cd)
- [13. Splitting across three machines](#13-splitting-across-three-machines)

---

## 0. What you're deploying

Two long-running services plus a set of `oneshot` maintenance timers, all plain
systemd units in `deploy/`:

| Unit | Role |
|---|---|
| `wxparser.service` | capture → segment → novelty gate → STT → extract → store |
| `wxparser-api.service` | the LAN HTTP/JSON query API on `:8080` |
| `wxparser-agc.timer` | every 3 min — keep the capture input level in the decoder's sweet spot |
| `wxparser-fixspelling.timer` / `wxparser-fixterms.timer` / `wxparser-prune.timer` | nightly store cleanup |
| `wxparser-reprocess.timer` | nightly 00:00 — rebuild the structured tables from the raw transcripts |
| `wxparser-deploy.timer` | optional pull-based CD (§12) |

Everything runs as an ordinary user account; nothing needs root at runtime.
The box makes **no outbound network calls** unless you opt into the webhook.
The two services communicate **only through PostgreSQL** (see "Architecture" in
the [README](../README.md)) — which is why they also split onto separate
machines with no code changes (§13).

## 1. Hardware

- **A dedicated NWR receiver** with a line/headphone output (the reference is a
  Reecom R-1630). RF reception is handled in hardware — wxparser only sees audio.
- **A line-in or mic-in jack** on the PC (or a cheap USB sound card).
- Any Linux box with 2+ cores and ~2 GB free RAM. STT is the only heavy stage;
  see §4 for the model-vs-CPU tradeoff.

## 2. OS packages

`arecord`, `amixer`, and `alsactl` come from `alsa-utils` and are called as
subprocesses — they are required at runtime.

```bash
# Fedora/RHEL:
sudo dnf -y install git python3 python3-numpy python3-pg8000 alsa-utils \
                    gcc-c++ cmake make        # compilers only needed to build whisper.cpp

# Debian/Ubuntu/Raspberry Pi OS:
sudo apt -y install git python3 python3-numpy python3-pip alsa-utils \
                    build-essential cmake
sudo python3 -m pip install --break-system-packages 'pg8000>=1.31'
```

The systemd units run `/usr/bin/python3` directly (no venv), so the two runtime
Python deps (`numpy`, `pg8000` — both BSD) must live system-wide, as above.
Debian note: the `python3-pg8000` **apt package is too old** (1.10 — no
`pg8000.native`); pg8000 must come from pip, and `--break-system-packages` is
deliberate because the services use the system interpreter.

## 3. Radio → sound card

Wire the radio's line/headphone out into the PC's mic/line-in, tune it to your
NWR station, and leave it on with the squelch open (continuous audio).

Find and test the capture device:

```bash
arecord -l                                        # list capture devices
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 5 test.wav && aplay test.wav
```

If your device isn't card 0, set `WX_ALSA_DEVICE` (e.g. `plughw:1,0`) in the
service unit (§8). Set the input gain so speech peaks well below clipping —
roughly, speech should sit around −20 dBFS. Don't agonize over the exact level:
once the AGC timer (§11) is running it nudges the mixer into the decoder's
sweet spot automatically and persists it with `alsactl store`.

Two analog gotchas the health checks were built around: a level that drifts too
quiet makes the box silently go "deaf" (no segments form), and a dead/off-tune
radio produces static that still clears the VAD gate. Both now surface as
`degraded` on `/health` — see §10.

## 4. whisper.cpp + model

Build [whisper.cpp](https://github.com/ggml-org/whisper.cpp) and fetch the
default model:

```bash
cd ~
git clone https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build && cmake --build build -j --config Release
bash models/download-ggml-model.sh small.en-q5_1
```

wxparser expects `~/whisper.cpp/build/bin/whisper-cli` and
`~/whisper.cpp/models/ggml-small.en-q5_1.bin`; override with `WX_WHISPER_BIN` /
`WX_WHISPER_MODEL` if yours differ (e.g. a BLAS build in `build-blas/`).

**Model choice:** `small.en-q5_1` is the shipped default. On a very old CPU it
runs well behind real-time per segment and keeps up only because most audio is
gated out as repeats. After a day of running, check `pipeline.queue_depth` on
`/health` — if it climbs and stays up, drop to the faster model:

```bash
bash models/download-ggml-model.sh base.en-q5_1
# then set WX_WHISPER_MODEL=~/whisper.cpp/models/ggml-base.en-q5_1.bin in the unit
```

## 5. PostgreSQL

One idempotent script installs the server, initializes the cluster, creates the
`wxparser` role plus the `wxparser` and `wxparser_test` databases, and adds
local-trust `pg_hba` entries for the role on 127.0.0.1:

```bash
cd ~/wxparser        # after the clone in §6 — or run it from anywhere in the repo
deploy/setup-postgres.sh          # Fedora/RHEL
deploy/setup-postgres-debian.sh   # Debian/Ubuntu/Pi OS
```

No passwords are involved: auth is local trust, and the API/pipeline connect via
`WX_PG_*` defaults (`127.0.0.1:5432`, db `wxparser`, user `wxparser`). If you
want password auth instead, set `WX_PG_PASSWORD` and switch the `pg_hba` entries
to `scram-sha-256`.

## 6. The application

Clone to the service account's home (the units assume `~/wxparser`):

```bash
git clone https://github.com/campbell-r-e/wxparser ~/wxparser
```

There is no install step — the services run `python3 -m wxparser.main` /
`python3 -m wxparser.api` from the repo working directory, so an update is a
`git pull` + service restart (or let the CD timer do both, §12).

Smoke-test in the foreground before wiring up systemd:

```bash
cd ~/wxparser
python3 -m wxparser.main      # should log capture starting, then segments
# in another terminal:
python3 -m wxparser.api
curl -s localhost:8080/health | python3 -m json.tool
```

## 7. Station profile

Everything region-specific lives in a profile JSON — station callsign and
frequency, home city, timezone, the whisper vocabulary prompt, and the
place-name correction table. The bundled default is KJY93 (Muncie, IN). For any
other transmitter:

```bash
cp wxparser/profiles/kjy93_muncie.json ~/my_station.json
# edit: station, frequency_mhz, primary_city, tz,
#       stt_prompt  -> your NWS office's counties/towns (~50 tokens max),
#       place_corrections -> seed loosely; refine from real airings later
```

Point the services at it with `Environment=WX_PROFILE=/home/<user>/my_station.json`
in **both** unit files (§8). Expect the first days to surface STT garbles of
your local place names — `deploy/propose_corrections.py` mines the stored
transcripts and proposes corrections, and `deploy/reprocess.sh` re-derives the
whole structured store after you adopt them, so the record heals retroactively.

## 8. systemd services

The units in `deploy/` hardcode the reference box's account (`User=creed`,
`WorkingDirectory=/home/creed/wxparser`). Adjust those two lines — plus any
`Environment=` overrides you need (`WX_ALSA_DEVICE`, `WX_PROFILE`,
`WX_WHISPER_MODEL`, …) — then install:

```bash
sudo cp deploy/wxparser.service deploy/wxparser-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wxparser wxparser-api
```

Notes baked into the units: the pipeline runs `Nice=5` and the API `Nice=10`
(STT may pin the CPU; interactive use of the box stays responsive), the
pipeline joins the `audio` supplementary group, and both restart automatically
(`Restart=always`).

## 9. Firewall

The API binds `0.0.0.0:8080` and has no auth — it is designed to be **LAN-only**.
Open the port to your LAN and nothing else:

```bash
sudo firewall-cmd --permanent --add-port=8080/tcp && sudo firewall-cmd --reload
```

Do not port-forward it to the internet. For remote access use an overlay like
Tailscale/WireGuard.

## 10. Verify it's alive

```bash
curl -fsS localhost:8080/health | python3 -m json.tool
journalctl -u wxparser -f          # watch segments: "[N new / M repeat]"
```

Expectations on a fresh start: `/health` may report `degraded ("audio silent")`
for a minute until the first segment lands — that's startup, not a fault. The
first transcripts appear within a few minutes; conditions/forecast fill in as
products air (a value must be heard twice before the API surfaces it —
`WX_MIN_SIGHTINGS`). `/health` returns HTTP 503 whenever it's not `ok`, so a
dumb monitor can alarm on status code alone:

- `degraded` — audio silent (deaf input), no novel speech for an hour
  (static/dead carrier), or the STT worker is wedged;
- `down` — the capture process itself isn't running.

Then take the snapshot: `curl -s localhost:8080/now`, or point
[`demo/wx.sh`](../demo/README.md) at the box for a live dashboard.

## 11. Maintenance timers

All optional but recommended; each is a `oneshot` service + timer pair, and
every job is idempotent:

```bash
sudo cp deploy/wxparser-{agc,fixspelling,fixterms,prune,reprocess}.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wxparser-agc.timer wxparser-fixspelling.timer \
                            wxparser-fixterms.timer wxparser-prune.timer \
                            wxparser-reprocess.timer
```

The AGC timer is the one you really want on a long-lived box: analog input
levels drift (volume knob, a card reset reverting the mic boost), and a
too-quiet feed is a silent failure. It reads the level the pipeline publishes,
nudges the ALSA mixer one small step at a time, and persists the result.

## 12. Optional: pull-based CD

If the box can't be reached by cloud CI runners (LAN-only), deployment can be
pull-based: `wxparser-deploy.timer` runs `deploy/auto_deploy.sh` every 10
minutes, which fetches `origin/main`, re-runs the **full test suite with the
package coverage gate** (100% of `wxparser/`, see DEVELOPMENT.md §3) in a persistent venv (`~/wxparser-testenv`, created on
first run), and only on green restarts the services — rolling the tree back on
red, so a bad push never takes the node down. Changes confined to
`wxparser/api.py`/`verify.py`, tests, and docs restart only the API, leaving
capture untouched.

Two prerequisites:

```bash
# the wxparser role must be able to (re)create the test database
sudo -u postgres psql -c "ALTER ROLE wxparser CREATEDB;"
# the repo needs a remote it can fetch without prompting (read-only deploy key or https)
```

Install and watch it:

```bash
sudo cp deploy/wxparser-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now wxparser-deploy.timer
tail -f ~/wxparser-deploy.log      # "DEPLOYED <sha>" / "TESTS FAILED — rolled back"
```

**Never leave uncommitted edits in the deployed repo** — a red deploy does
`git reset --hard`, which wipes them. Make changes in a separate clone, push,
and let the timer (or `sudo systemctl start wxparser-deploy.service` for an
immediate run) do the rest.

---

## 13. Splitting across three machines

Everything above assumes one box. When one low-power machine can't carry all
three roles, the system splits cleanly into tiers that talk **only through
PostgreSQL** — there is no service-to-service RPC anywhere. (The quick way:
run `deploy/install.sh` on each machine and pick its role — it asks these
questions and applies this whole section. What follows is the manual path.)

```
[radio box]  wxparser.service ──────────┐
             + wxparser-agc.timer       │  writes: raw_reports + structured
             (sound card, whisper.cpp)  ▼  tables + pipeline_health heartbeat
[db box]     PostgreSQL  ◄──────────────┤
                                        ▲  reads only
[api box]    wxparser-api.service ──────┘  (no audio, no whisper, no numpy-heavy DSP)
```

Architecturally this is CQRS: the pipeline is the sole writer, the API is a
read-only consumer, and the structured tables are a projection of the raw
transcript store. The pipeline's liveness heartbeat also flows through the DB
(the `pipeline_health` row), so `/health` works from any machine;
`health.json` remains on the radio box for the AGC timer and as a same-box
fallback.

### The DB box

Run `deploy/setup-postgres.sh` as usual, then open Postgres to the LAN with
password auth (the stock setup is localhost-trust):

```bash
sudo -u postgres psql -c "ALTER ROLE wxparser WITH LOGIN PASSWORD 'CHANGE-ME' CREATEDB;"
# /var/lib/pgsql/data/postgresql.conf:
#   listen_addresses = '*'
# /var/lib/pgsql/data/pg_hba.conf — ABOVE the localhost-trust lines, scoped to your LAN:
#   host wxparser,wxparser_test wxparser 192.168.68.0/22 scram-sha-256
sudo systemctl restart postgresql
sudo firewall-cmd --permanent --add-port=5432/tcp && sudo firewall-cmd --reload
```

(`CREATEDB` is what lets each machine's CD run recreate `wxparser_test`, §12.)

### The radio box

The only machine that needs the sound card, whisper.cpp, and `alsa-utils`.
Install `wxparser.service` + `wxparser-agc.timer` (§8/§11) and point them at
the DB box. Put the credentials in a root-owned env file rather than the unit:

```bash
sudo install -m 600 /dev/null /etc/wxparser.env
sudo tee /etc/wxparser.env >/dev/null <<'EOF'
WX_PG_HOST=192.168.68.x
WX_PG_PASSWORD=CHANGE-ME
EOF
# in each unit's [Service] section:  EnvironmentFile=/etc/wxparser.env
```

The nightly cleanup timers (§11) are DB jobs — run them here (or anywhere with
DB access), just once, not on every machine.

### The API box

The lightest role: `git`, `python3`, `python3-pg8000`, `python3-numpy`, the
repo, and `wxparser-api.service` with the same `EnvironmentFile`. No audio
stack, no whisper, no profile concerns beyond `WX_STATION` labeling. Open
`:8080` to the LAN (§9). You can run several of these against one DB if you
ever want redundancy.

### CD on a split deployment

Each machine runs its own `wxparser-deploy.timer` (each needs DB access for
the gated suite). Tell each one which units it owns via
`WX_DEPLOY_SERVICES` in `wxparser-deploy.service`:

```ini
# radio box:
Environment=WX_DEPLOY_SERVICES=wxparser
# api box:
Environment=WX_DEPLOY_SERVICES=wxparser-api
```

Unset, it defaults to both — the single-box behavior.
