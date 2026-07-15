# Porting wxparser to a new region

Porting is a **profile, not a code edit** — but a new deployment should know exactly
which pieces are generic, which it supplies, and which it will grow into over the
first couple of weeks. This is that guide.

## What's generic vs. what you supply

Generic — works anywhere in the US with no changes:

- the capture → segment → STT → extraction → voting pipeline and the query API
- the SAME/EAS decoder: national FIPS county table (`wxparser/data/fips.json`) and
  event codes, so digital alerts are correct on day one in any county
- `wxparser/data/stt_terms.py` — corrections for whisper's garbles of NWR's
  *templated weather vocabulary* ("Pies around 80" → "Highs", "Chants of Brain" →
  "Chance of Rain"). These are properties of the vocabulary, not of a region;
  leave them alone and add to them if your station's voice surfaces new ones.

You supply — all of it in one profile JSON (see the table below):

- station callsign, frequency, home city, timezone
- the whisper vocabulary prompt (your NWS office's counties + towns)
- the place-name correction table and (optionally) the roundup roster/slot maps
  for your coverage cities

## Hardware

1. A weather radio (or SDR) tuned to your local NWR channel (162.400–162.550 MHz),
   line/USB audio into the box. Antenna quality matters more than radio quality —
   a weak, hissy signal is what drives STT mishears.
2. Almost any PC. The reference deployment runs whisper `small.en-q5_1` in real
   time on a 2009 Core 2 Duo (no AVX) using an OpenBLAS build of whisper.cpp;
   anything newer is comfortable. If STT can't keep up, drop to `base.en-q5_1`
   (`WX_WHISPER_MODEL`) — extraction still works, with somewhat more mishears.
   A standing STT queue of a few-to-~25 segments during active weather is normal
   and bounded; it drains in quiet hours.

## The profile

Copy `wxparser/profiles/kjy93_muncie.json` → `<yourstation>.json` and select it
with `WX_PROFILE` (a bundled name, or a path to a `.json` anywhere). Required keys:

| Key | What it is |
|---|---|
| `station` | NWR callsign, e.g. `KJY93` (`WX_STATION` overrides) |
| `frequency_mhz` | your NWR channel, 162.400–162.550 (`WX_FREQ_MHZ`) |
| `primary_city` | the home/station city — standalone obs attach here (`WX_PRIMARY_CITY`) |
| `stt_prompt` | whisper vocabulary prompt: your NWS office's counties + towns (~50 tokens, keep it short) |
| `place_corrections` | canonical city → list of ways whisper mishears it (seed with empty lists) |

Optional keys — absent keys just disable the corresponding pass:

| Key | What it is |
|---|---|
| `tz` | IANA timezone of the station, e.g. `America/Chicago` (`WX_TZ` overrides). **Set this**: the code default is `America/Indiana/Indianapolis`, and forecast valid-windows and `/verify` day-boundaries anchor to it. |
| `roundup_cities` | canonical roster of cities your station's regional roundup recites. Enables the unknown-city gate: names not in the roster get slot recovery instead of polluting the store. |
| `slot_anchors` | canonical city → the city that always follows it in the roundup. Recovers a garbled name by position, keyed off names whisper decodes reliably. |
| `roundup_leadins` | lead-in phrase (lowercase) → the city it introduces, e.g. `"just outside indiana": "Lima"`. Second recovery path when the anchor city itself was garbled. |

Tips:

- **`stt_prompt`** is the highest-leverage knob. List the counties and towns your
  office actually names on air — it biases whisper toward spelling them right in
  the first place, which shrinks `place_corrections` work later.
- **`place_corrections`** starts loose. You cannot guess how whisper will mishear
  your cities in your station's synthesized voice; mine it from real data instead
  (workflow below).
- **`slot_anchors`/`roundup_leadins`** need a few days of listening: note the fixed
  order your roundup recites cities in, then key anchors off the names that come
  through reliably.

## Region-specific bits *outside* the profile

Two maintenance scripts in `deploy/` carry the reference deployment's data baked
in — they are deployment-local cleanup, not pipeline logic:

- **`deploy/fix_city_spellings.py`** (runs on a timer): its `CORRECTIONS` and
  `JUNK_CITIES` dicts are the Muncie deployment's catalogue of city garbles and
  STT junk tokens. A new deployment should empty these and grow its own (or not
  install `wxparser-fixspelling.timer` at all — write-time `place_corrections`
  in the profile do the same job going forward; this script only retro-fixes rows
  stored before a correction existed).
- **`deploy/fix_stt_terms.py`** retro-applies `stt_terms.py` to already-stored
  transcripts. Generic, safe anywhere.

One code-level note: extraction (`wxparser/extract.py`) was developed against the
Indianapolis office's broadcast templates. NWR phrasing is standardized enough
nationally that the grammar is expected to carry — and where a template phrase is
matched, there's a generic fallback: e.g. roundup-start detection tries four
detectors, and the position-independent `NN at <City>` pattern fires regardless of
the regional lead-in wording ("across Indiana…" is a fast-path, not a dependency).
If your office phrases something the extractors miss, the transcripts are all in
`raw_reports` — add a pattern and `python -m wxparser.reprocess` re-projects the
whole record through it retroactively. Nothing is lost while you tune.

## First-week workflow

1. Set `WX_PROFILE`, start the services, and watch `/health` — it should go `ok`
   within a minute of audio landing, and the `novel` counter should advance
   steadily. A wall of `repeat sim≈0.99` in the logs with a frozen novel counter
   means static/dead carrier, not broadcast: fix the radio, not the pipeline.
2. Read `/transcripts` for a day. Spelled-wrong cities and missed extractions show
   up here first.
3. Run `deploy/propose_corrections.py` — it mines the stored transcripts for
   consistent STT garbles and proposes `place_corrections` entries. Fold the safe
   ones into your profile.
4. `python -m wxparser.reprocess` — replays every raw transcript through the
   corrected pipeline and rebuilds the structured tables (the raw store is the
   source of truth; reprocessing is always safe).
5. `deploy/audit_data.py` — read-only integrity check (value ranges, city roster,
   alert codes); should print `AUDIT: PASS`.
6. Validate against ground truth: compare `/now` and `/forecast` with
   `api.weather.gov` observations/forecasts for your cities. The reference
   deployment verifies at ~1.5–2 °F mean absolute error on regional temps and
   0–2 °F on forecast highs/lows; if you're far off that, something above needs
   another pass.

Expect steps 2–5 to repeat a few times over the first two weeks as your station
voice's quirks surface. The volume of new corrections drops off quickly — the
reference deployment's table stabilized at ~13 entries.
