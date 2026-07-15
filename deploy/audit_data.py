#!/usr/bin/env python3
"""Full data + transcript integrity audit (read-only). Prints a concise report and
a final summary line `AUDIT: PASS|FAIL (<n> issues)` for monitoring.

Run on the box:  python3 deploy/audit_data.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import wxparser
import pg8000.native as pg  # noqa: E402
from wxparser.data.same_events import EVENT_CODES  # noqa: E402
from wxparser.stt import HALLUCINATIONS, NON_SPEECH_MARKERS  # noqa: E402

c = pg.Connection(user=os.environ.get("WX_PG_USER", "wxparser"),
                  host=os.environ.get("WX_PG_HOST", "127.0.0.1"),
                  port=int(os.environ.get("WX_PG_PORT", "5432")),
                  database=os.environ.get("WX_PG_DATABASE", "wxparser"),
                  password=os.environ.get("WX_PG_PASSWORD") or None)
issues: list[str] = []


def q(sql):
    return c.run(sql)


# ---- DB: range / logic violations (impossible stored values) ----
CHECKS = {
    "obs temp <-40|>120":   "SELECT count(*) FROM city_observations WHERE "
                            "condition='temperature_f' AND (value_num<-40 OR value_num>120)",
    "obs dewpt <-50|>90":   "SELECT count(*) FROM city_observations WHERE "
                            "condition='dewpoint_f' AND (value_num<-50 OR value_num>90)",
    "obs humid <0|>100":    "SELECT count(*) FROM city_observations WHERE "
                            "condition='humidity_pct' AND (value_num<0 OR value_num>100)",
    "obs press <25|>33":    "SELECT count(*) FROM city_observations WHERE "
                            "condition='pressure_in' AND (value_num<25 OR value_num>33)",
    "obs wind <0|>120":     "SELECT count(*) FROM city_observations WHERE "
                            "condition='wind_speed_mph' AND (value_num<0 OR value_num>120)",
    "fc high <-30|>120":    "SELECT count(*) FROM forecasts WHERE "
                            "high_f IS NOT NULL AND (high_f<-30 OR high_f>120)",
    "fc low <-40|>90":      "SELECT count(*) FROM forecasts WHERE "
                            "low_f IS NOT NULL AND (low_f<-40 OR low_f>90)",
    "fc precip <0|>100":    "SELECT count(*) FROM forecasts WHERE "
                            "precip_pct IS NOT NULL AND (precip_pct<0 OR precip_pct>100)",
    "fc high<low":          "SELECT count(*) FROM forecasts WHERE "
                            "high_f IS NOT NULL AND low_f IS NOT NULL AND high_f<low_f",
    "fc night low>=85":     "SELECT count(*) FROM forecasts WHERE "
                            "period ILIKE '%night%' AND low_f>=85",
}
print("=== DB range/logic violations ===")
for name, sql in CHECKS.items():
    n = q(sql)[0][0]
    print("  %-22s %s" % (name, "ok" if n == 0 else "** %d **" % n))
    if n:
        issues.append("%d %s" % (n, name))

# ---- DB: garbage surfaced cities (sightings>=2 but non-wordlike) ----
rows = q("SELECT city FROM city_conditions WHERE condition='temperature_f' AND sightings>=2")
bad_cities = [r[0] for r in rows if len(r[0]) <= 3 or not re.match(r"^[A-Z][a-zA-Z .'-]+$", r[0])]
print("=== surfaced cities: %d, suspicious: %s ===" % (len(rows), bad_cities or "none"))
if bad_cities:
    issues.append("suspicious cities: %s" % bad_cities)

# ---- DB: alert event codes + FIPS ----
arows = q("SELECT event, areas FROM alerts")
bad_ev = sorted({r[0] for r in arows if r[0] and r[0].upper() not in EVENT_CODES})
bad_fips = [f for r in arows for f in (r[1] or []) if not re.match(r"^0\d{5}$", str(f))]
print("=== alerts: %d, bad event codes: %s, bad FIPS: %s ==="
      % (len(arows), bad_ev or "none", bad_fips or "none"))
if bad_ev:
    issues.append("bad event codes %s" % bad_ev)
if bad_fips:
    issues.append("bad FIPS %s" % bad_fips)

# ---- DB: forecast values flagged uncertain (low vote agreement) ----
unc = q("SELECT period, confidence FROM forecasts "
        "WHERE issued_at=(SELECT max(issued_at) FROM forecasts) AND confidence IS NOT NULL")
uncertain = []
for period, conf in unc:
    cf = conf if isinstance(conf, dict) else json.loads(conf or "{}")
    for f, a in cf.items():
        if a < 0.6:
            uncertain.append("%s.%s=%.2f" % (period, f, a))
print("=== forecast fields flagged uncertain (<0.6 agreement): %s ===" % (uncertain or "none"))

# ---- transcripts: integrity (from the raw_reports store) ----
n = blank = 0
prod = Counter()
# same junk-transcript catalogue the pipeline uses (stt.py), so the audit's
# idea of "hallucination" can't drift from the code that filters them; the
# bracketed markers are compared alnum-stripped like the loop below strips.
HALLUC = HALLUCINATIONS | {
    re.sub(r"[^a-z0-9 ]", "", m).strip() for m in NON_SPEECH_MARKERS}
for pt, ty, text in q("SELECT product_type, type, text FROM raw_reports"):
    n += 1
    prod[pt or ty or "?"] += 1
    t = (text or "").strip().lower()
    if t:
        core = re.sub(r"[^a-z0-9 ]", "", t).strip()
        if not core or core in HALLUC:
            blank += 1
unk_rate = 100.0 * prod.get("unknown", 0) / max(1, n)
print("=== transcripts: %d records, blank/halluc=%d, unknown=%.0f%% ===" % (n, blank, unk_rate))

c.close()
print("AUDIT: %s (%d issues)%s" % ("FAIL" if issues else "PASS", len(issues),
                                   "  -> " + "; ".join(issues) if issues else ""))
sys.exit(1 if issues else 0)
