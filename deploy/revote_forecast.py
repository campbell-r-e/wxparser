#!/usr/bin/env python3
"""Recompute the latest forecast issuance from the consensus of recent airings.

A one-off companion to the forecast-voting fix: the live aggregator now majority-
votes each (period, field), but rows written before the fix hold latest-airing-
wins values, so a single garbled airing can still be the value /forecast serves
(e.g. Sunday high 71 over the voted 85). This replaces the latest issuance's
values with the mode over a recent window of airings — the same answer the voting
aggregator would converge to — so the served forecast is correct immediately,
without waiting for the radio to re-air (handy when the radio is down).

Connects directly to Postgres; never imports or interrupts the capture service.
Idempotent: re-running recomputes the same consensus.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import timedelta

import pg8000.native as pg

WINDOW_HOURS = int(os.environ.get("WX_REVOTE_WINDOW_H", "18"))
FIELDS = ("high_f", "low_f", "precip_pct", "sky")

c = pg.Connection(user=os.environ.get("WX_PG_USER", "wxparser"),
                  host=os.environ.get("WX_PG_HOST", "127.0.0.1"),
                  port=int(os.environ.get("WX_PG_PORT", "5432")),
                  database=os.environ.get("WX_PG_DATABASE", "wxparser"),
                  password=os.environ.get("WX_PG_PASSWORD") or None)


def mode_and_agreement(values):
    """(mode, agreement) over non-null values; agreement = mode_count/total."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None
    counts = Counter(vals)
    top = max(counts.values())
    # tie -> most recent (values passed newest-last); mirrors _FieldVoter
    tied = {v for v, n in counts.items() if n == top}
    return next(v for v in reversed(vals) if v in tied), round(top / len(vals), 2)


cities = [r[0] for r in c.run("SELECT DISTINCT city FROM forecasts")]
changes = 0
for city in cities:
    latest = c.run("SELECT max(issued_at) FROM forecasts WHERE city=:c", c=city)[0][0]
    window_start = latest - timedelta(hours=WINDOW_HOURS)
    periods = [r[0] for r in c.run(
        "SELECT period FROM forecasts WHERE city=:c AND issued_at=:i", c=city, i=latest)]
    for period in periods:
        conf = {}
        for f in FIELDS:
            rows = c.run(
                "SELECT " + f + " FROM forecasts "
                "WHERE city=:c AND period=:p AND issued_at > :s "
                "ORDER BY issued_at",
                c=city, p=period, s=window_start)
            consensus, agreement = mode_and_agreement([r[0] for r in rows])
            if agreement is not None:
                conf[f] = agreement
            cur = c.run("SELECT " + f + " FROM forecasts "
                        "WHERE city=:c AND period=:p AND issued_at=:i",
                        c=city, p=period, i=latest)[0][0]
            if consensus is not None and consensus != cur:
                c.run("UPDATE forecasts SET " + f + "=:v "
                      "WHERE city=:c AND period=:p AND issued_at=:i",
                      v=consensus, c=city, p=period, i=latest)
                flag = " UNCERTAIN" if agreement is not None and agreement < 0.6 else ""
                print(f"  {city} {period:16} {f:10} {cur} -> {consensus}  (agree {agreement}){flag}")
                changes += 1
        c.run("UPDATE forecasts SET confidence=CAST(:cf AS jsonb) "
              "WHERE city=:c AND period=:p AND issued_at=:i",
              cf=json.dumps(conf) if conf else None, c=city, p=period, i=latest)

print(f"revote complete: {changes} field(s) corrected to consensus")
c.close()
