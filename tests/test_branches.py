"""Branch-coverage fills: exercise the *other* side of conditionals."""

from __future__ import annotations

import itertools
import json
import queue as _q

import numpy as np

import wxparser.main as main
from wxparser.config import Config
from wxparser.dedup import TextDeduper
from wxparser.extract import (
    AlmanacAggregator,
    CityConditionsAggregator,
    ForecastAggregator,
    extract_alert_details,
    extract_forecast_fields,
    extract_observation,
)
from wxparser.fingerprint import Fingerprinter
from wxparser.formats import net_bulletin, sitrep
from wxparser.segment import segment_stream
from wxparser.stt import TranscriptSegment, Transcript


def _t(text):
    return Transcript(text=text, segments=[TranscriptSegment(0.0, 1.0, text)], language="en")


# --- stt: flags-off command path ----------------------------------------- #
def test_transcribe_with_all_flags_off(monkeypatch):
    import wxparser.stt as stt
    cfg = Config(whisper_dynamic_audio_ctx=False, whisper_fast_decode=False, whisper_prompt="")
    payload = {"transcription": [{"text": " Clear.", "offsets": {"from": 0, "to": 1}}],
               "result": {"language": "en"}}

    def run(cmd, capture_output=True, text=True):
        # no -ac / -bs / --prompt when those flags are off
        assert "-ac" not in cmd and "-bs" not in cmd and "--prompt" not in cmd
        from pathlib import Path
        Path(cmd[cmd.index("-of") + 1]).with_suffix(".json").write_text(json.dumps(payload))
        return type("P", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr(stt.subprocess, "run", run)
    assert stt.transcribe_samples(np.zeros(16000, dtype=np.int16), cfg).text == "Clear."


# --- extract: validation / absent branches -------------------------------- #
def test_extract_out_of_range_and_missing_branches():
    # high/low out of range -> not stored
    assert "high_f" not in extract_forecast_fields("Highs around 200.")
    assert "low_f" not in extract_forecast_fields("Lows around 200.")
    # pressure with no trend word after it
    o = extract_observation("The barometric pressure was 29.97 inches.")
    assert o["pressure_in"] == 29.97 and "pressure_trend" not in o
    # steady temp is skipped when an explicit high is already present
    assert "steady_f" not in extract_forecast_fields(
        "Highs around 80. Temperature near 80.")
    # steady temp present but out of range -> not stored
    assert "steady_f" not in extract_forecast_fields("Temperature near 200.")
    # precip percentage out of range -> not stored
    assert "precip_pct" not in extract_forecast_fields("Chance of rain 150 percent.")
    # wind direction with an unparseable speed -> direction only
    w = extract_observation("The wind was south at light.")
    assert w["wind"] == "south" and "wind_speed_mph" not in w


def test_nearby_out_of_range_skipped():
    out = CityConditionsAggregator().update("It was 200 at Marion and 74 at Anderson.")
    cities = {r["city"] for r in out}
    assert "Anderson" in cities and "Marion" not in cities      # 200 rejected by range


def test_city_header_non_primary_ignored():
    # a non-home header is skipped; with a roundup present there's no home fallback
    out = CityConditionsAggregator(primary_city="Muncie").update(
        "At Anderson, the temperature was 70 degrees. Nearby, 74 at Marion.")
    cities = {r["city"] for r in out}
    assert "Muncie" not in cities and "Marion" in cities


def test_alert_details_dedupes_repeated_location():
    # same city twice via the lowercase locator -> the second is folded out
    d = extract_alert_details("Storm near Yorktown then near Yorktown again moving east.")
    assert d.get("locations", []).count("Yorktown") == 1     # dup location folded


# --- fingerprint: zero-energy (norm == 0) --------------------------------- #
def test_fingerprint_zero_energy():
    v, dig = Fingerprinter(Config()).compute(np.zeros(16000, dtype=np.int16))
    assert float(np.linalg.norm(v)) == 0.0 and len(dig) == 16   # un-normalised branch


# --- dedup: history entry that does NOT beat the best --------------------- #
def test_dedup_loop_non_best():
    d = TextDeduper(Config())
    d.prime([
        {"id": "1", "product_type": "zone_forecast",
         "text": "tonight mostly cloudy lows in the lower 60s chance of rain 40%"},
        {"id": "2", "product_type": "tornado_warning",
         "text": "tornado warning take cover seek shelter now in a sturdy building"},
    ])
    # entry 1 is the best match; entry 2 is far less similar (s > best is False)
    r = d.consider({"id": "3", "product_type": "zone_forecast",
                    "text": "tonight mostly cloudy lows in the lower 60s chance of rain 50%"})
    assert r.supersedes == "1"


# --- segment: a cut that's too short -> dropped (seg is None) -------------- #
def test_segment_cut_too_short_is_dropped():
    cfg = Config(vad_max_segment_s=0.4)      # 20 frames; below 1.0s min_speech
    n = int(cfg.frame_seconds * cfg.sample_rate)
    frames = ((np.full(n, 8000, dtype=np.int16), i * cfg.frame_seconds) for i in range(100))
    assert list(segment_stream(frames, cfg)) == []   # every cut is too short -> None


# --- raw store: every reader round-trips through Postgres ----------------- #
def test_raw_store_readers_roundtrip(tmp_path):
    from wxparser.db import Database
    db = Database(Config(out_dir=tmp_path, pg_database="wxparser_test"))
    db._run("TRUNCATE raw_reports")
    for r in [
        {"id": "1", "captured_at": "2026-06-24T10:00:00Z",
         "product_type": "zone_forecast", "text": "tonight clear"},
        {"id": "2", "captured_at": "2026-06-24T12:00:00Z",
         "product_type": "zone_forecast", "text": "highs 80"},
    ]:
        db.insert_raw_report(r)
    assert len(db.query_raw_reports()) == 2
    assert db.count_raw_reports() == 2
    assert len(db.raw_reports_since("2026-06-24T00:00:00Z", 10)) == 2
    assert len(db.recent_raw_reports(10)) == 2


# --- formats: all-absent fields ------------------------------------------- #
def test_formats_minimal_snapshot():
    snap = {"generated_at": "2026-06-24T12:00:00Z", "station": "KJY93", "city": "Muncie",
            "conditions": [], "roundup": [],
            "forecast": [{"city": "Muncie", "periods": [{"period": "Tonight"}]}],
            "alerts": []}
    assert "no current data" in net_bulletin(snap)        # empty conditions line
    assert "Tonight: (no data)" in net_bulletin(snap)     # period with no fields
    assert "NONE" in sitrep(snap)                          # no alerts
    # a snapshot with no forecast periods at all
    bare = dict(snap, forecast=[])
    assert net_bulletin(bare).endswith("\n") and "== FORECAST ==" not in sitrep(bare)


# --- main: worker with db=None and hb=None (the None-side branches) ------- #
def test_stt_worker_none_db_hb(tmp_path, monkeypatch):
    main._STOP.clear()
    cfg = Config(out_dir=tmp_path)
    texts = iter([
        "At Muncie, the temperature was 80 degrees. Tonight, lows in the lower 60s.",
        "Tornado warning for Delaware County until 630 PM. Take cover spotter activation.",
        "Sunrise today is at 6.13 AM and sunset is at 9.15 PM.",  # almanac, db/hb None branches
    ])
    monkeypatch.setattr(main, "transcribe_samples", lambda s, c: _t(next(texts)))
    q = _q.PriorityQueue()
    seq = itertools.count()

    class Seg:
        samples = np.zeros(16000, dtype=np.int16)
        duration_s = 1.0
    q.put((0, next(seq), (Seg(), "d")))
    q.put((0, next(seq), (Seg(), "d")))
    q.put((0, next(seq), (Seg(), "d")))
    q.put((0, next(seq), None))
    main._stt_worker(q, cfg, False, TextDeduper(cfg), CityConditionsAggregator(),
                     ForecastAggregator(), AlmanacAggregator(), None, None)   # db None, hb None


def test_worker_error_without_hb(tmp_path, monkeypatch):
    main._STOP.clear()

    def boom(s, c):
        raise RuntimeError("x")
    monkeypatch.setattr(main, "transcribe_samples", boom)
    q = _q.PriorityQueue()

    class Seg:
        samples = np.zeros(16000, dtype=np.int16)
        duration_s = 1.0
    q.put((0, 0, (Seg(), "d")))
    q.put((0, 1, None))
    main._stt_worker(q, Config(out_dir=tmp_path), False, TextDeduper(Config()),
                     CityConditionsAggregator(), ForecastAggregator(),
                     AlmanacAggregator(), None, None)  # error, hb None


def test_worker_db_yes_hb_none(tmp_path, monkeypatch):
    from wxparser.db import Database
    main._STOP.clear()
    cfg = Config(out_dir=tmp_path, pg_database="wxparser_test")
    db = Database(cfg)
    db.clear()
    texts = iter([
        "At Muncie, the temperature was 80 degrees. Tonight, lows in the lower 60s.",
        # an alert PRODUCT with no extractable details -> writes a detail row but
        # skips the detail-print (the `if details:` false branch)
        "Hazardous weather outlook for central Indiana.",
    ])
    monkeypatch.setattr(main, "transcribe_samples", lambda s, c: _t(next(texts)))
    q = _q.PriorityQueue()
    seq = itertools.count()

    class Seg:
        samples = np.zeros(16000, dtype=np.int16)
        duration_s = 1.0
    q.put((0, next(seq), (Seg(), "d")))
    q.put((0, next(seq), (Seg(), "d")))
    q.put((0, next(seq), None))
    main._stt_worker(q, cfg, False, TextDeduper(cfg), CityConditionsAggregator(),
                     ForecastAggregator(), AlmanacAggregator(), db, None)     # db yes, hb None
    assert db.all_conditions_for_city("Muncie")


def test_sitrep_spoken_without_detail():
    snap = {"generated_at": "2026-06-24T12:00:00Z", "station": "K", "city": "Muncie",
            "conditions": [], "roundup": [], "forecast": [],
            "alerts": [{"event": "TOR", "event_label": "Tornado Warning", "counties": ["X"],
                        "expires_at": "2026-06-24T13:00:00Z",
                        "spoken": [{"spotter_activation": False}]}]}   # no until/threats
    assert "spoken:" not in sitrep(snap)
