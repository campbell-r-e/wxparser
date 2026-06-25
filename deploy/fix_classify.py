#!/usr/bin/env python3
"""Backfill product_type on already-stored transcripts.

The classifier (store.classify) was keyword-only, so VAD fragments that lacked a
literal product name were typed "unknown" (~73% of reports). It now types the
routine products from the same structured extraction the pipeline runs. New
reports get the better type at write time; this one-off re-runs the *current*
classifier over the stored reports so historical transcript queries are accurate
too.

Append-safe like deploy/fix_stt_terms.py: snapshot the file, rewrite the records
whose type changed, then re-attach any lines the live service appended while we
worked. Only product_type is touched; transcript text is left untouched.

Run it by hand:
    python3 deploy/fix_classify.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxparser.config import CONFIG  # noqa: E402
from wxparser.store import classify  # noqa: E402


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path: Path = CONFIG.reports_jsonl
    if not path.exists():
        print(f"[{stamp}] no reports.jsonl at {path}; nothing to do.")
        return

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
            out_parts.append(raw)
            continue
        text = rec.get("text")
        if isinstance(text, str):
            new_type = classify(text)
            if new_type != rec.get("product_type"):
                rec["product_type"] = new_type
                changed += 1
                out_parts.append(json.dumps(rec, ensure_ascii=False) + "\n")
                continue
        out_parts.append(raw)

    if not changed:
        print(f"[{stamp}] done — nothing to reclassify.")
        return

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
    print(f"[{stamp}] done — reclassified {changed} report(s) in {path}.")


if __name__ == "__main__":
    main()
