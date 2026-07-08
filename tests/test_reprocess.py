"""reprocess: rebuild the DB as a re-derivable projection of the raw store."""

from __future__ import annotations

from wxparser.config import Config
from wxparser.db import Database
from wxparser.reprocess import reprocess


def _cfg(tmp_path):
    return Config(out_dir=tmp_path, pg_database="wxparser_test")


def _fresh(cfg) -> Database:
    """A test DB with the raw transcript store emptied so each reprocess starts
    from a known state (clear() is structured-only and never touches raw_reports)."""
    db = Database(cfg)
    db._run("TRUNCATE raw_reports")
    return db


def _seed(db: Database, recs: list[dict]) -> None:
    for r in recs:
        db.insert_raw_report(r)


def test_reprocess_rebuilds_conditions_forecast_almanac_alert(tmp_path):
    cfg = _cfg(tmp_path)
    db = _fresh(cfg)
    _seed(db, [
        {"captured_at": "2026-06-24T12:00:00Z", "id": "t1", "product_type": "current_conditions",
         "text": "At Muncie, it was clear. The temperature was 70 degrees."},
        {"captured_at": "2026-06-24T12:01:00Z", "id": "t2", "product_type": "zone_forecast",
         "text": "Tonight, clear. Lows in the lower 60s."},
        {"captured_at": "2026-06-24T15:08:56Z", "type": "same_alert", "id": "a1",
         "alert": {"event": "RWT", "areas": ["018035"], "counties": ["Delaware County, IN"],
                   "purge_minutes": 360, "raw": "ZCZC"}},
        {"captured_at": "2026-06-24T12:02:00Z", "type": "observation", "id": "o1"},  # derived
        {"captured_at": "2026-06-24T12:03:00Z", "id": "t3", "text": ""},             # blank
    ])
    stats = reprocess(cfg, db)
    assert stats["transcripts"] == 2 and stats["alerts"] == 1
    assert stats["skipped_envelope"] == 1 and stats["blank"] == 1
    conds = {c["condition"]: c["value"] for c in db.all_conditions_for_city("Muncie", 1)}
    assert conds["temperature_f"] == 70
    assert db.latest_forecasts()[0]["periods"]
    rows = db.alerts_history(None, None, None, 10, 0)
    assert db.alerts_history_count(None, None, None) == 1 and rows[0]["event"] == "RWT"


def test_reprocess_applies_corrections_retroactively(tmp_path):
    # the garbled home header "Edmondsee" is fixed by the extractor AT REPROCESS
    # TIME over the raw stored text — the whole point of the projection model.
    cfg = _cfg(tmp_path)
    db = _fresh(cfg)
    _seed(db, [{
        "captured_at": "2026-06-24T23:00:00Z", "id": "t1", "product_type": "current_conditions",
        "text": "At 7 p.m., Edmondsee, light rain and fog were reported. "
                "The temperature was 68 degrees."}])
    reprocess(cfg, db)
    conds = {c["condition"]: c["value"] for c in db.all_conditions_for_city("Muncie", 1)}
    assert conds["temperature_f"] == 68


def test_reprocess_empty_store_clears(tmp_path):
    cfg = _cfg(tmp_path)
    db = _fresh(cfg)  # raw store emptied — nothing to replay
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 50},
                      "2026-06-24T12:00:00Z")
    stats = reprocess(cfg, db)
    assert stats == {} and db.latest_readings() == []   # cleared, nothing to rebuild


def test_reprocess_low_confidence_stored_not_voted(tmp_path):
    # a low-confidence transcript replays as stored-but-not-voted, mirroring the
    # live worker's gate, and is counted in low_conf_skipped
    cfg = Config(out_dir=tmp_path, pg_database="wxparser_test", stt_confidence_floor=0.5)
    db = _fresh(cfg)
    _seed(db, [
        {"captured_at": "2026-06-24T12:00:00Z", "id": "t1",
         "text": "At Muncie, it was clear. The temperature was 12 degrees.",
         "stt": {"avg_confidence": 0.2}}])
    stats = reprocess(cfg, db)
    assert stats["low_conf_skipped"] == 1
    assert stats["transcripts"] == 1                     # still counted as replayed
    assert db.all_conditions_for_city("Muncie") == []    # but not voted


def test_reprocess_skips_blank_text(tmp_path):
    cfg = _cfg(tmp_path)
    db = _fresh(cfg)
    _seed(db, [
        {"captured_at": "2026-06-24T12:00:00Z", "id": "t1",
         "text": "At Muncie, it was clear. The temperature was 70 degrees."},
        {"captured_at": "2026-06-24T12:01:00Z", "id": "t2", "text": "   "},  # whitespace -> blank
    ])
    stats = reprocess(cfg, db)
    assert stats["transcripts"] == 1 and stats["blank"] == 1
