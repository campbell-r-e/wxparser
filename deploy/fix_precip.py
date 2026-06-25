#!/usr/bin/env python3
"""Backfill forecast precip_pct that earlier extraction missed.

Until extract.py learned to accept "%" (it only matched the spelled-out word
"percent"), zone-forecast segments like "Chance of rain 40%" were stored with a
NULL precip_pct. This job replays the stored transcripts through the *current*
ForecastAggregator (which now extracts both "%" and "percent") and fills in
precip_pct on forecast rows that are still NULL.

Rows are keyed by (issued_at, city, period). issued_at == the report's
captured_at: the live service derives both from `_utc_now_iso()` (second
precision) microseconds apart while processing the same segment, so for any
saved forecast report the matching forecast rows share its captured_at.

Only NULL precip cells are touched — an existing value is never overwritten, and
a snapshot period that maps to no row simply updates nothing. Safe to re-run; it
does not import or interrupt the capture service.

Run it by hand:
    python3 deploy/fix_precip.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Run-by-path puts deploy/ on sys.path[0]; add the repo root so the imported
# ForecastAggregator is the same one the capture service runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pg8000.native  # noqa: E402

from wxparser.config import CONFIG  # noqa: E402
from wxparser.db import _parse_iso  # noqa: E402
from wxparser.extract import ForecastAggregator  # noqa: E402


def _connect() -> pg8000.native.Connection:
    return pg8000.native.Connection(
        user=os.environ.get("WX_PG_USER", "wxparser"),
        host=os.environ.get("WX_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("WX_PG_PORT", "5432")),
        database=os.environ.get("WX_PG_DATABASE", "wxparser"),
        password=os.environ.get("WX_PG_PASSWORD") or None,
    )


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path: Path = CONFIG.reports_jsonl
    if not path.exists():
        print(f"[{stamp}] no reports.jsonl at {path}; nothing to do.")
        return

    conn = _connect()
    fc = ForecastAggregator()
    filled = 0
    try:
        # reports.jsonl is append-only and chronological, so iterating the file
        # replays transcripts in the same order the live service saw them.
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = rec.get("text")
                captured = rec.get("captured_at")
                if not isinstance(text, str) or not captured:
                    continue
                if not fc.update(text):
                    continue  # not a forecast-bearing segment
                issued = _parse_iso(captured)
                city = fc.city
                for p in fc.snapshot():
                    pp = p.get("precip_pct")
                    if pp is None:
                        continue
                    rows = conn.run(
                        "UPDATE forecasts SET precip_pct=:pp "
                        "WHERE issued_at=:ia AND city=:city AND period=:pd "
                        "AND precip_pct IS NULL RETURNING period",
                        pp=pp, ia=issued, city=city, pd=p["period"],
                    )
                    filled += len(rows or [])
    finally:
        conn.close()

    if filled:
        print(f"[{stamp}] done — filled precip_pct on {filled} forecast row(s).")
    else:
        print(f"[{stamp}] done — no NULL precip rows to fill.")


if __name__ == "__main__":
    main()
