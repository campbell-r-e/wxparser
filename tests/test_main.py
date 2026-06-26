"""main.py: pipeline helpers + a full --once run with capture/STT mocked."""

from __future__ import annotations

import numpy as np

import wxparser.main as main
from wxparser.config import Config
from wxparser.dedup import TextDeduper
from wxparser.same import parse_header
from wxparser.stt import Segment, Transcript

_HDR = "ZCZC-WXR-TOR-018035-018057+0045-1741830-KJY93-"


def _t(text):
    return Transcript(text=text, segments=[Segment(0.0, 1.0, text)], language="en")


def _frames(pattern, cfg):
    n = int(cfg.frame_seconds * cfg.sample_rate)
    t = 0.0
    for ch in pattern:
        yield np.full(n, 8000 if ch == "S" else 0, dtype=np.int16), t
        t += cfg.frame_seconds


def test_save_new_then_duplicate(tmp_path):
    cfg = Config(out_dir=tmp_path)
    d = TextDeduper(cfg)
    r1 = main._save(_t("Highs around 80 with west winds"), cfg, 10.0, "fp", d)
    assert r1 is not None and r1["product_type"] == "zone_forecast"
    r2 = main._save(_t("Highs around 80 with west winds"), cfg, 10.0, "fp", d)
    assert r2 is None                              # text-duplicate -> dropped


def test_tee_to_same_passes_through_and_feeds():
    fed = []

    class Mon:
        def feed(self, frame, t):
            fed.append(t)
    out = list(main._tee_to_same(iter([(np.zeros(4), 0.0), (np.zeros(4), 0.1)]), Mon()))
    assert len(out) == 2 and fed == [0.0, 0.1]
    # None monitor is a no-op passthrough
    assert len(list(main._tee_to_same(iter([(np.zeros(4), 0.0)]), None))) == 1


def test_emit_alert_writes_report(tmp_path):
    cfg = Config(out_dir=tmp_path)
    main._emit_alert(parse_header(_HDR), cfg, db=None)   # db None, webhook unset
    assert cfg.reports_jsonl.exists() and "TOR" in cfg.reports_jsonl.read_text()


def test_run_file_mocked(tmp_path, monkeypatch):
    cfg = Config(out_dir=tmp_path)
    monkeypatch.setattr(main, "transcribe", lambda wav, c: _t("Highs around 80"))
    assert main.run_file(tmp_path / "x.wav", cfg) == 0
    assert "Highs around 80" in cfg.reports_jsonl.read_text()
    # blank transcript -> nothing saved
    monkeypatch.setattr(main, "transcribe", lambda wav, c: _t("[BLANK_AUDIO]"))
    assert main.run_file(tmp_path / "y.wav", cfg) == 0


def test_run_live_once(tmp_path, monkeypatch):
    cfg = Config(out_dir=tmp_path, pg_database="wxparser_test", same_enabled=False)
    main._STOP.clear()
    monkeypatch.setattr(main, "stream_frames",
                        lambda c, on_retry=None: _frames("s" + "S" * 70 + "s" * 60, c))
    monkeypatch.setattr(main, "transcribe_samples", lambda samples, c: _t("Highs around 80."))
    # covers the producer loop + worker wiring; the save itself is racy on shutdown
    # (the poison pill out-prioritises a 1-segment backlog by design) and is covered
    # deterministically by the _stt_worker tests, so only assert clean completion.
    assert main.run_live(cfg, once=True) == 0
    main._STOP.clear()


def test_run_live_repeat_and_same_enabled(tmp_path, monkeypatch):
    from wxparser.db import Database
    cfg = Config(out_dir=tmp_path, pg_database="wxparser_test", same_enabled=True)
    main._STOP.clear()
    # seed a stored reading + report so the aggregator/text-dedup priming runs
    db = Database(cfg)
    db.clear()
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 80},
                      "2026-06-24T12:00:00Z")
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 80},
                      "2026-06-24T12:30:00Z")
    # a forecast for a non-home area so the priming loop hits its no-match branch
    db.write_forecast([{"period": "Tonight", "low_f": 60}], "2026-06-24T18:00:00Z",
                      city="Anderson")
    # a stored almanac field so run_live's almanac-prime branch runs
    db.record_almanac({"field": "sunrise", "value": "6:14 AM"}, "2026-06-24T06:00:00Z")
    cfg.reports_jsonl.write_text(
        '{"id":"p","captured_at":"2026-06-24T00:00:00Z","product_type":"zone_forecast",'
        '"text":"earlier forecast"}\n', encoding="utf-8")

    def frames(c, on_retry=None):
        # 15 leading silence each so both segments get the FULL 10-frame pre-roll
        # and are byte-identical -> the second trips the novelty gate as a repeat.
        p = "s" * 15 + "S" * 70 + "s" * 60
        yield from _frames(p, c)                 # novel
        yield from _frames(p, c)                 # identical -> repeat (gated)
    monkeypatch.setattr(main, "stream_frames", frames)
    monkeypatch.setattr(main, "transcribe_samples", lambda s, c: _t("Highs around 80."))
    assert main.run_live(cfg, once=False) == 0   # SAME-enabled + priming + repeat paths
    main._STOP.clear()


def test_stt_worker_full_paths(tmp_path, monkeypatch):
    import itertools
    import queue as _q

    from wxparser.db import Database
    from wxparser.extract import (
        AlmanacAggregator,
        CityConditionsAggregator,
        ForecastAggregator,
    )
    from wxparser.health import Heartbeat
    main._STOP.clear()
    cfg = Config(out_dir=tmp_path, pg_database="wxparser_test")
    db = Database(cfg)
    db.clear()
    texts = iter([
        "At Muncie, the temperature was 80 degrees. Tonight, lows in the lower 60s.",  # OBS + forecast
        "At Muncie, the temperature was 81 degrees. Tonight, lows in the lower 60s with showers.",  # update (supersedes)
        "Tornado warning for Delaware County until 630 PM. Take cover now spotter activation.",  # alert detail
        "Sunrise today is at 6.13 AM and sunset is at 9.15 PM.",                       # almanac
        "[BLANK_AUDIO]",                                                               # blank
    ])
    monkeypatch.setattr(main, "transcribe_samples", lambda s, c: _t(next(texts)))
    q = _q.PriorityQueue()
    seq = itertools.count()

    class Seg:
        samples = np.zeros(16000, dtype=np.int16)
        duration_s = 1.0
    for _ in range(5):
        q.put((0, next(seq), (Seg(), "d")))
    q.put((0, next(seq), None))                  # poison pill (after the 5 segments)
    main._stt_worker(q, cfg, False, TextDeduper(cfg), CityConditionsAggregator(),
                     ForecastAggregator(), AlmanacAggregator(), db, Heartbeat(cfg))
    # OBS, forecast, an alert detail, and an almanac field were all written
    assert db.all_conditions_for_city("Muncie")
    assert db.alert_details_between("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    assert {r["field"] for r in db.latest_almanac()} >= {"sunrise", "sunset"}


def test_emit_alert_with_db(tmp_path):
    from wxparser.db import Database
    cfg = Config(out_dir=tmp_path, pg_database="wxparser_test")
    db = Database(cfg)
    db.clear()
    main._emit_alert(parse_header(_HDR), cfg, db)
    total, _ = db.alerts_history(None, None, None, 1, 0)
    assert total == 1


def test_stt_worker_survives_transcribe_error(tmp_path, monkeypatch):
    import queue as _q

    from wxparser.extract import (
        AlmanacAggregator,
        CityConditionsAggregator,
        ForecastAggregator,
    )
    main._STOP.clear()
    cfg = Config(out_dir=tmp_path)

    def _boom(s, c):
        raise RuntimeError("boom")
    monkeypatch.setattr(main, "transcribe_samples", _boom)
    q = _q.PriorityQueue()

    class _Seg:
        samples = np.zeros(16000, dtype=np.int16)
        duration_s = 1.0
    from wxparser.health import Heartbeat
    q.put((0, 0, (_Seg(), "digest")))
    q.put((0, 1, None))                                    # poison AFTER the segment
    # swallows the STT error (covers the except path, incl. the heartbeat update)
    main._stt_worker(q, cfg, False, TextDeduper(cfg), CityConditionsAggregator(),
                     ForecastAggregator(), AlmanacAggregator(), None, Heartbeat(cfg))


def test_stt_worker_handles_empty_queue():
    import queue as _q

    from wxparser.extract import (
        AlmanacAggregator,
        CityConditionsAggregator,
        ForecastAggregator,
    )
    main._STOP.clear()

    class _Q:
        def __init__(self): self.n = 0
        def get(self, timeout=None):
            self.n += 1
            if self.n == 1:
                raise _q.Empty                            # first call: queue empty -> continue
            return (0, 0, None)                            # then poison -> break
        def task_done(self): pass
    main._stt_worker(_Q(), Config(), False, TextDeduper(Config()),
                     CityConditionsAggregator(), ForecastAggregator(),
                     AlmanacAggregator(), None)


def test_run_live_breaks_when_stopped(tmp_path, monkeypatch):
    cfg = Config(out_dir=tmp_path, pg_database="wxparser_test", same_enabled=False)
    monkeypatch.setattr(main, "stream_frames",
                        lambda c, on_retry=None: _frames("s" + "S" * 70 + "s" * 60, c))
    monkeypatch.setattr(main, "transcribe_samples", lambda s, c: _t("x"))
    main._STOP.set()                                      # already stopped -> first seg hits break
    assert main.run_live(cfg, once=False) == 0
    main._STOP.clear()


def test_main_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "run_file", lambda wav, cfg: 0)
    monkeypatch.setattr(main, "run_live", lambda cfg, once: 0)
    assert main.main(["--file", str(tmp_path / "a.wav")]) == 0
    assert main.main(["--once"]) == 0
