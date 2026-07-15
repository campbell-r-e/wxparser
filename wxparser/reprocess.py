"""Rebuild the structured DB as a pure projection of the raw transcript store.

The raw transcripts (db.raw_reports) are the source of truth; everything in the
structured tables is derived from them. This replays the stored transcripts through
the SAME extraction step the live pipeline uses (pipeline.apply_readings), so
improving any correction (place_names, stt_terms, an extraction regex) and
re-running this retroactively fixes ALL history — no per-record hand-patching. It
also rebuilds the SAME alerts (stored as type=same_alert records) and the spoken
alert details.

Because corrections live in the extractor and in stt_terms, reprocess re-applies
correct_terms to the stored text too, so newly-added word fixes take effect on old
transcripts as well.

Usage (run with the capture service stopped so it can't write mid-rebuild):
    python3 -m wxparser.reprocess            # rebuild the configured DB in place
    python3 -m wxparser.reprocess --into wxparser_rebuild   # build into another DB
"""
from __future__ import annotations

import argparse
from collections import Counter

from .config import Config
from .data.stt_terms import correct_terms
from .db import Database
from .extract import AlmanacAggregator, CityConditionsAggregator, ForecastAggregator
from .pipeline import PipelineState, apply_readings, write_alert_detail_if_any


def reprocess(cfg: Config, db: Database, source_db: Database | None = None) -> dict:
    """Clear `db`'s structured tables and rebuild them by replaying the raw
    transcript store in capture order. Raw is read from `source_db` (defaults to
    `db`), so `--into` can project a fresh DB from the configured DB's raw store.
    Returns a stats counter."""
    records = (source_db or db).iter_raw_reports()
    records.sort(key=lambda r: r.get("captured_at", ""))  # capture order = vote order

    state = PipelineState(
        aggregator=CityConditionsAggregator(primary_city=cfg.primary_city,
                                            peer_min=cfg.peer_min_cities,
                                            peer_max_dev=cfg.peer_max_dev_f),
        forecast=ForecastAggregator(), almanac=AlmanacAggregator(), db=db)
    db.clear()
    stats: Counter = Counter()
    for rec in records:
        rtype = rec.get("type")
        if rtype == "same_alert":
            db.write_alert({"id": rec["id"], "captured_at": rec["captured_at"],
                            "alert": rec.get("alert", {})})
            stats["alerts"] += 1
        elif rtype == "observation":
            stats["skipped_envelope"] += 1   # a derived snapshot, not a source
        else:
            text = correct_terms(rec.get("text") or "")  # latest word fixes, retroactively
            if not text.strip():
                stats["blank"] += 1
                continue
            ca = rec.get("captured_at")
            conf = (rec.get("stt") or {}).get("avg_confidence")
            summary = apply_readings(text, ca, state, confidence=conf,
                                     confidence_floor=cfg.stt_confidence_floor)
            if summary.get("low_confidence"):
                stats["low_conf_skipped"] += 1
            write_alert_detail_if_any(text, ca, rec.get("id"), rec.get("product_type"), db)
            stats["transcripts"] += 1
    return dict(stats)


def main(argv=None) -> int:  # pragma: no cover - CLI entry
    ap = argparse.ArgumentParser(description="Rebuild the wxparser DB from transcripts.")
    ap.add_argument("--into", help="target database name (default: the configured DB, "
                    "rebuilt in place — run with the capture service stopped)")
    args = ap.parse_args(argv)
    cfg = Config()
    if args.into:
        # Raw lives in the configured DB; project it into a fresh target DB.
        source = Database(cfg)
        target = Database(cfg, database=args.into)
        stats = reprocess(cfg, target, source_db=source)
        source.close()
        target.close()
    else:
        db = Database(cfg)
        stats = reprocess(cfg, db)
        db.close()
    print(f"reprocess complete ({args.into or cfg.pg_database}): {stats}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
