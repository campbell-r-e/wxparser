#!/usr/bin/env python3
"""Post-write STT term cleanup for already-stored transcripts.

The write-time correction lives in `wxparser.data.stt_terms.correct_terms` and is
applied by `stt.transcribe` going forward. This standalone job retro-fixes raw
transcripts stored *before* a correction was added — or while the running capture
service still has the old correction map in memory — so a new mis-hearing fix can
ship without bouncing (and re-syncing) the capture service.

It corrects the raw transcript store in place (db.raw_reports): for each stored
report it applies `correct_terms` to the top-level "text" and every per-segment
"text", and upserts the record when anything changed. Postgres serializes the
concurrent live append, so — unlike the old reports.jsonl rewrite — there's no
snapshot/re-attach race to manage. Run `python3 -m wxparser.reprocess` afterwards
to re-project the structured tables from the corrected raw text.

Run it by hand to see what it would do:
    python3 deploy/fix_stt_terms.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Run-by-path puts deploy/ on sys.path[0], not the repo root; add the root so
# `import wxparser` resolves to the same package the capture service uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxparser.config import CONFIG  # noqa: E402
from wxparser.data.stt_terms import correct_terms  # noqa: E402
from wxparser.db import Database  # noqa: E402


def _correct_record(rec: dict) -> bool:
    """Apply correct_terms to rec['text'] and each segment's text in place. Returns
    True if anything changed. Matches the write-time invariant (stt.transcribe):
    a fixed record's joined "text" and per-segment "text" stay consistent."""
    dirty = False
    text = rec.get("text")
    if isinstance(text, str):
        fixed = correct_terms(text)
        if fixed != text:
            rec["text"] = fixed
            dirty = True
    for seg in rec.get("segments") or []:
        st = seg.get("text") if isinstance(seg, dict) else None
        if isinstance(st, str):
            fixed = correct_terms(st)
            if fixed != st:
                seg["text"] = fixed
                dirty = True
    return dirty


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = Database(CONFIG)
    changed = 0
    try:
        for rec in db.iter_raw_reports():
            if _correct_record(rec):
                db.insert_raw_report(rec)  # upsert by id — updates text + payload
                changed += 1
    finally:
        db.close()
    if changed:
        print(f"[{stamp}] done — corrected {changed} transcript(s) in raw_reports.")
    else:
        print(f"[{stamp}] done — nothing to correct.")


if __name__ == "__main__":
    main()
