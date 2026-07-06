#!/usr/bin/env python3
"""One-time backfill: import the legacy transcripts/reports.jsonl into db.raw_reports.

Before the raw transcript store moved into Postgres (db.raw_reports), every report
was appended as one JSON line to transcripts/reports.jsonl. This loads that file
into the new table so the Postgres raw store is complete back to the first capture.

Idempotent: insert_raw_report upserts by report id, so re-running is safe and a
report already present is simply overwritten with the same content. Records with no
id (shouldn't happen) or unparseable lines are skipped and counted.

Run once on the box, after the code carrying the raw_reports schema is deployed:
    python3 deploy/backfill_raw_reports.py
    python3 deploy/backfill_raw_reports.py --reports /path/to/reports.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Run-by-path puts deploy/ on sys.path[0], not the repo root; add the root so
# `import wxparser` resolves to the same package the capture service uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxparser.config import CONFIG  # noqa: E402
from wxparser.db import Database  # noqa: E402


def backfill(db: Database, path: Path) -> dict:
    stats = {"inserted": 0, "skipped_no_id": 0, "bad_json": 0}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats["bad_json"] += 1
                continue
            if not rec.get("id") or not rec.get("captured_at"):
                stats["skipped_no_id"] += 1
                continue
            db.insert_raw_report(rec)
            stats["inserted"] += 1
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill reports.jsonl into raw_reports.")
    ap.add_argument("--reports", type=Path, default=CONFIG.reports_jsonl,
                    help="path to the legacy reports.jsonl (default: the configured one)")
    args = ap.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not args.reports.exists():
        print(f"[{stamp}] no reports.jsonl at {args.reports}; nothing to backfill.")
        return 0
    db = Database(CONFIG)
    try:
        stats = backfill(db, args.reports)
    finally:
        db.close()
    print(f"[{stamp}] backfill complete from {args.reports}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
