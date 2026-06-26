#!/usr/bin/env python3
"""Backfill the climate/almanac store from already-saved transcripts.

The climate recap (sunrise/sunset, year-to-date precipitation and its departure
from normal, degree days) airs every loop but was never captured until the
AlmanacAggregator landed. This job replays the stored transcripts through the
*current* extractor and populates both almanac tables, so the history is filled
in immediately rather than waiting for the live service to re-hear every field.

It connects directly to Postgres (it never imports or interrupts the capture
service). It is idempotent: history rows are keyed by (captured_at, field) and
inserted ON CONFLICT DO NOTHING, and the latest-value table is rewritten from the
full replay each run, so re-running produces the same state (no sightings drift).

Run it by hand on the box:
    python3 deploy/backfill_almanac.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Run-by-path puts deploy/ on sys.path[0]; add the repo root so the imported
# extractor is the same one the capture service runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pg8000.native  # noqa: E402

from wxparser.config import CONFIG  # noqa: E402
from wxparser.db import _parse_iso  # noqa: E402
from wxparser.extract import ALMANAC_NUMERIC, AlmanacAggregator, extract_almanac  # noqa: E402

_DDL = [
    """CREATE TABLE IF NOT EXISTS almanac (
        field TEXT PRIMARY KEY, value_num DOUBLE PRECISION, value_text TEXT,
        votes INTEGER, total INTEGER, sightings INTEGER NOT NULL DEFAULT 0,
        first_seen TIMESTAMPTZ, last_seen TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS almanac_observations (
        captured_at TIMESTAMPTZ NOT NULL, field TEXT NOT NULL,
        value_num DOUBLE PRECISION, value_text TEXT, votes INTEGER, total INTEGER,
        PRIMARY KEY (captured_at, field))""",
]


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
    for ddl in _DDL:
        conn.run(ddl)

    agg = AlmanacAggregator()
    sightings: Counter = Counter()
    first_seen: dict[str, datetime] = {}
    last_seen: dict[str, datetime] = {}
    hist = 0
    try:
        # reports.jsonl is append-only and chronological, so iterating the file
        # replays transcripts in the order the live service saw them.
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
                fields = extract_almanac(text)
                if not fields:
                    continue
                ca = _parse_iso(captured)
                agg.update(text)  # keep the voted snapshot current
                for field, value in fields.items():
                    sightings[field] += 1
                    first_seen.setdefault(field, ca)
                    last_seen[field] = ca
                    num = float(value) if field in ALMANAC_NUMERIC else None
                    txt = None if field in ALMANAC_NUMERIC else str(value)
                    rows = conn.run(
                        "INSERT INTO almanac_observations"
                        "(captured_at,field,value_num,value_text,votes,total) "
                        "VALUES(:ca,:f,:num,:txt,NULL,NULL) "
                        "ON CONFLICT (captured_at,field) DO NOTHING RETURNING field",
                        ca=ca, f=field, num=num, txt=txt,
                    )
                    hist += len(rows or [])

        snap = agg.snapshot()
        for field, voted in snap.items():
            value = voted["value"]
            num = float(value) if field in ALMANAC_NUMERIC else None
            txt = None if field in ALMANAC_NUMERIC else str(value)
            conn.run(
                "INSERT INTO almanac"
                "(field,value_num,value_text,votes,total,sightings,first_seen,last_seen) "
                "VALUES(:f,:num,:txt,:votes,:total,:s,:fs,:ls) "
                "ON CONFLICT (field) DO UPDATE SET "
                "value_num=EXCLUDED.value_num, value_text=EXCLUDED.value_text, "
                "votes=EXCLUDED.votes, total=EXCLUDED.total, "
                "sightings=EXCLUDED.sightings, first_seen=EXCLUDED.first_seen, "
                "last_seen=EXCLUDED.last_seen",
                f=field, num=num, txt=txt, votes=voted["votes"], total=voted["total"],
                s=sightings[field], fs=first_seen[field], ls=last_seen[field],
            )
    finally:
        conn.close()

    if snap:
        fields = ", ".join(f"{f}={snap[f]['value']}" for f in sorted(snap))
        print(f"[{stamp}] done — {len(snap)} almanac field(s), "
              f"{hist} new history row(s). Latest: {fields}")
    else:
        print(f"[{stamp}] done — no almanac content found in stored transcripts.")


if __name__ == "__main__":
    main()
