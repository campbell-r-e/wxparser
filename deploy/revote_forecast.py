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

import os
from collections import Counter

import pg8000.native as pg

WINDOW_HOURS = int(os.environ.get("WX_REVOTE_WINDOW_H", "18"))
FIELDS = ("high_f", "low_f", "precip_pct", "sky")

c = pg.Connection(user=os.environ.get("WX_PG_USER", "wxparser"),
                  host=os.environ.get("WX_PG_HOST", "127.0.0.1"),
                  database=os.environ.get("WX_PG_DATABASE", "wxparser"))


def mode(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    counts = Counter(vals)
    top = max(counts.values())
    # tie -> most recent (values passed newest-last); mirrors _FieldVoter
    tied = {v for v, n in counts.items() if n == top}
    return next(v for v in reversed(vals) if v in tied)


cities = [r[0] for r in c.run("SELECT DISTINCT city FROM forecasts")]
changes = 0
for city in cities:
    latest = c.run("SELECT max(issued_at) FROM forecasts WHERE city=:c", c=city)[0][0]
    periods = [r[0] for r in c.run(
        "SELECT period FROM forecasts WHERE city=:c AND issued_at=:i", c=city, i=latest)]
    for period in periods:
        for f in FIELDS:
            rows = c.run(
                "SELECT " + f + " FROM forecasts "
                "WHERE city=:c AND period=:p AND issued_at > :i - (:w || ' hours')::interval "
                "ORDER BY issued_at",
                c=city, p=period, i=latest, w=str(WINDOW_HOURS))
            consensus = mode([r[0] for r in rows])
            cur = c.run("SELECT " + f + " FROM forecasts "
                        "WHERE city=:c AND period=:p AND issued_at=:i",
                        c=city, p=period, i=latest)[0][0]
            if consensus is not None and consensus != cur:
                c.run("UPDATE forecasts SET " + f + "=:v "
                      "WHERE city=:c AND period=:p AND issued_at=:i",
                      v=consensus, c=city, p=period, i=latest)
                print(f"  {city} {period:16} {f:10} {cur} -> {consensus}")
                changes += 1

print(f"revote complete: {changes} field(s) corrected to consensus")
c.close()
