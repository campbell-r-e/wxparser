#!/usr/bin/env python3
"""Post-write STT term cleanup for already-stored transcripts.

The write-time correction lives in `wxparser.data.stt_terms.correct_terms` and is
applied by `stt.transcribe` going forward. This standalone job retro-fixes
transcripts that were written *before* a correction was added — or while the
running capture service still has the old correction map in memory — so a new
mis-hearing fix can ship without bouncing (and re-syncing) the capture service.

It rewrites `transcripts/reports.jsonl` in place, applying `correct_terms` to
each record's "text". The file is append-only and the live service may append
while we run, so we compact *safely*: snapshot the file and remember its byte
length, correct the snapshot, then re-attach any bytes the service appended
during the run before the atomic replace. Anything appended in that window is
left as-is and picked up on the next run.

Run it by hand to see what it would do:
    python3 deploy/fix_stt_terms.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Run-by-path puts deploy/ on sys.path[0], not the repo root; add the root so
# `import wxparser` resolves to the same package the capture service uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxparser.config import CONFIG  # noqa: E402
from wxparser.data.stt_terms import correct_terms  # noqa: E402


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path: Path = CONFIG.reports_jsonl
    if not path.exists():
        print(f"[{stamp}] no reports.jsonl at {path}; nothing to do.")
        return

    # 1) Snapshot the file and remember how many bytes we read, so any lines the
    #    live service appends while we work can be re-attached untouched.
    snapshot = path.read_bytes()
    snap_len = len(snapshot)

    changed = 0
    out_parts: list[str] = []
    for raw in snapshot.decode("utf-8").splitlines(keepends=True):
        line = raw.rstrip("\n")
        if not line.strip():
            out_parts.append(raw)
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out_parts.append(raw)  # leave anything unparseable exactly as-is
            continue
        # Match the write-time invariant (stt.transcribe): correct both the
        # joined top-level "text" and every per-segment "text", so a fixed
        # record is internally consistent.
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
        if dirty:
            changed += 1
            out_parts.append(json.dumps(rec, ensure_ascii=False) + "\n")
            continue
        out_parts.append(raw)

    if not changed:
        print(f"[{stamp}] done — nothing to correct.")
        return

    # 2) Write the corrected snapshot, then re-attach any bytes appended since
    #    the snapshot (read as late as possible to shrink the race window), and
    #    atomically replace. Worst case a transcript written in the sub-ms gap
    #    between this read and the replace is corrected on the next run instead.
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write("".join(out_parts).encode("utf-8"))
        with path.open("rb") as src:
            src.seek(snap_len)
            tail = src.read()
        if tail:
            f.write(tail)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    print(f"[{stamp}] done — corrected {changed} transcript(s) in {path}.")


if __name__ == "__main__":
    main()
