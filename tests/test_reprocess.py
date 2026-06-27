"""reprocess: rebuild the DB as a re-derivable projection of the transcript store."""

from __future__ import annotations

import json

from wxparser.config import Config
from wxparser.db import Database
from wxparser.reprocess import reprocess


def _cfg(tmp_path):
    return Config(out_dir=tmp_path, pg_database="wxparser_test")


def _write(tmp_path, lines):
    (tmp_path / "reports.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_reprocess_rebuilds_conditions_forecast_almanac_alert(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg)
    recs = [
        {"captured_at": "2026-06-24T12:00:00Z", "id": "t1", "product_type": "current_conditions",
         "text": "At Muncie, it was clear. The temperature was 70 degrees."},
        {"captured_at": "2026-06-24T12:01:00Z", "id": "t2", "product_type": "zone_forecast",
         "text": "Tonight, clear. Lows in the lower 60s."},
        {"captured_at": "2026-06-24T15:08:56Z", "type": "same_alert", "id": "a1",
         "alert": {"event": "RWT", "areas": ["018035"], "counties": ["Delaware County, IN"],
                   "purge_minutes": 360, "raw": "ZCZC"}},
        {"captured_at": "2026-06-24T12:02:00Z", "type": "observation", "id": "o1"},  # derived
        {"captured_at": "2026-06-24T12:03:00Z", "id": "t3", "text": ""},             # blank
    ]
    _write(tmp_path, [json.dumps(r) for r in recs])
    stats = reprocess(cfg, db)
    assert stats["transcripts"] == 2 and stats["alerts"] == 1
    assert stats["skipped_envelope"] == 1 and stats["blank"] == 1
    conds = {c["condition"]: c["value"] for c in db.all_conditions_for_city("Muncie", 1)}
    assert conds["temperature_f"] == 70
    assert db.latest_forecasts()[0]["periods"]
    total, rows = db.alerts_history(None, None, None, 10, 0)
    assert total == 1 and rows[0]["event"] == "RWT"


def test_reprocess_applies_corrections_retroactively(tmp_path):
    # the garbled home header "Edmondsee" is fixed by the extractor AT REPROCESS
    # TIME over the raw stored text — the whole point of the projection model.
    cfg = _cfg(tmp_path)
    db = Database(cfg)
    _write(tmp_path, [json.dumps({
        "captured_at": "2026-06-24T23:00:00Z", "id": "t1", "product_type": "current_conditions",
        "text": "At 7 p.m., Edmondsee, light rain and fog were reported. "
                "The temperature was 68 degrees."})])
    reprocess(cfg, db)
    conds = {c["condition"]: c["value"] for c in db.all_conditions_for_city("Muncie", 1)}
    assert conds["temperature_f"] == 68


def test_reprocess_missing_file_clears(tmp_path):
    cfg = _cfg(tmp_path)  # no reports.jsonl written
    db = Database(cfg)
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 50},
                      "2026-06-24T12:00:00Z")
    stats = reprocess(cfg, db)
    assert stats == {} and db.latest_readings() == []   # cleared, nothing to rebuild


def test_reprocess_skips_blank_and_malformed_lines(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg)
    (tmp_path / "reports.jsonl").write_text(
        json.dumps({"captured_at": "2026-06-24T12:00:00Z", "id": "t1",
                    "text": "At Muncie, it was clear. The temperature was 70 degrees."})
        + "\n\nNOT JSON\n", encoding="utf-8")   # blank line + malformed json line
    stats = reprocess(cfg, db)
    assert stats["transcripts"] == 1
