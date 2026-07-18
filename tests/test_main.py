"""main.py: pipeline helpers + a full --once run with capture/STT mocked."""

from __future__ import annotations

import numpy as np

import wxparser.main as main
from wxparser.pipeline import PipelineState
from wxparser.config import Config
from wxparser.dedup import TextDeduper
from wxparser.same import parse_header
from wxparser.stt import TranscriptSegment, Transcript

_HDR = "ZCZC-WXR-TOR-018035-018057+0045-1741830-KJY93-"


def _t(text):
    return Transcript(text=text, segments=[TranscriptSegment(0.0, 1.0, text)], language="en")


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


def test_emit_alert_lands_raw_report(tmp_path, make_cfg):
    # _emit_alert lands the SAME envelope in the raw store (the alerts table is
    # covered separately by test_emit_alert_with_db).
    from wxparser.db import Database
    cfg = make_cfg()
    db = Database(cfg)
    db.clear()
    db._run("TRUNCATE raw_reports")
    main._emit_alert(parse_header(_HDR), cfg, db)
    raws = db.query_raw_reports()
    assert len(raws) == 1 and raws[0]["type"] == "same_alert" and "TOR" in raws[0]["id"]


def test_run_file_mocked(tmp_path, monkeypatch, make_cfg):
    from wxparser.db import Database
    cfg = make_cfg()
    db = Database(cfg)
    db.clear()
    db._run("TRUNCATE raw_reports")
    monkeypatch.setattr(main, "transcribe", lambda wav, c: _t("Highs around 80"))
    assert main.run_file(tmp_path / "x.wav", cfg) == 0
    assert db.count_raw_reports(q="Highs around 80") == 1
    # blank transcript -> nothing saved
    monkeypatch.setattr(main, "transcribe", lambda wav, c: _t("[BLANK_AUDIO]"))
    assert main.run_file(tmp_path / "y.wav", cfg) == 0
    assert db.count_raw_reports() == 1                   # unchanged


def test_run_live_once(tmp_path, monkeypatch, make_cfg):
    from wxparser.db import Database
    cfg = make_cfg(same_enabled=False)
    main._STOP.clear()
    # empty raw store -> the "no recent reports to prime" branch; a home-city
    # (Muncie) forecast -> the matching-city forecast-prime branch
    db = Database(cfg)
    db.clear()
    db._run("TRUNCATE raw_reports")
    db.write_forecast([{"period": "Tonight", "low_f": 60}], "2026-06-24T18:00:00Z")
    db.close()
    monkeypatch.setattr(
        main, "stream_frames",
        lambda c, on_retry=None, should_stop=None: _frames("s" + "S" * 70 + "s" * 60, c))
    monkeypatch.setattr(main, "transcribe_samples", lambda samples, c: _t("Highs around 80."))
    # covers the producer loop + worker wiring; the save itself is racy on shutdown
    # (the poison pill out-prioritises a 1-segment backlog by design) and is covered
    # deterministically by the _stt_worker tests, so only assert clean completion.
    assert main.run_live(cfg, once=True) == 0
    main._STOP.clear()


def test_run_live_dumps_fingerprints_when_configured(tmp_path, monkeypatch, make_cfg):
    # the diagnostic dump must record every segment the producer sees, gated or
    # not: tuning the gate needs the repeats it currently throws away
    from wxparser.db import Database
    from wxparser.fingerprint import read_dump
    dump = tmp_path / "fp.bin"
    cfg = make_cfg(same_enabled=False, fp_dump_path=dump)
    main._STOP.clear()
    db = Database(cfg); db.clear(); db._run("TRUNCATE raw_reports"); db.close()
    monkeypatch.setattr(
        main, "stream_frames",
        lambda c, on_retry=None, should_stop=None: _frames("s" + "S" * 70 + "s" * 60, c))
    monkeypatch.setattr(main, "transcribe_samples", lambda samples, c: _t("Highs around 80."))
    assert main.run_live(cfg, once=True) == 0
    main._STOP.clear()
    stamps, mat = read_dump(dump, cfg.fp_n_mels * cfg.fp_time_bins)
    assert len(stamps) >= 1 and mat.shape[1] == cfg.fp_n_mels * cfg.fp_time_bins
    assert stamps[0].endswith("Z")


def test_run_live_sheds_when_stt_queue_saturated(tmp_path, monkeypatch, make_cfg):
    # Backpressure is the real STT load cap (the fingerprint can't be -- it scores
    # 0.988 on both repeats and distinct segments). With the worker not draining and
    # the cap at 1, the first novel queues and every later novel sheds instead of
    # growing the queue unbounded (2026-07-18: 0.995 passed ~100% and the queue ran
    # away). Distinct per-segment amplitudes keep each segment novel so the shed
    # path -- not the repeat path -- is what's exercised.
    cfg = make_cfg(same_enabled=False, stt_max_queue=1)
    main._STOP.clear()
    from wxparser.db import Database
    db = Database(cfg); db.clear(); db._run("TRUNCATE raw_reports"); db.close()

    def frames(c, on_retry=None, should_stop=None):
        # min_silence and min_speech are both 1.0s = 50 frames at 20ms, so each block
        # needs >=50 speech frames and >=50 silence between to segment cleanly.
        # Independent per-block NOISE (not constant amplitude -- a DC block has no
        # spectral content and would fingerprint identically -> read as a repeat):
        # decorrelated spectra score low similarity, so every segment is novel and
        # the shed branch, not the repeat branch, is what gets exercised.
        n = int(c.frame_seconds * c.sample_rate)
        t = 0.0
        for seed in range(5):                            # 5 distinct -> 5 novel segments
            for _ in range(60):                          # silence closes the prior segment
                yield np.zeros(n, dtype=np.int16), t; t += c.frame_seconds
            rng = np.random.RandomState(seed)
            for _ in range(70):                          # 1.4s speech > min_speech 1.0s
                yield (rng.uniform(-9000, 9000, n)).astype(np.int16), t
                t += c.frame_seconds
        for _ in range(60):
            yield np.zeros(n, dtype=np.int16), t; t += c.frame_seconds

    # worker that never drains: the queue can only fill, so backpressure must cap it
    monkeypatch.setattr(main, "_stt_worker", lambda *a, **k: main._STOP.wait())
    monkeypatch.setattr(main, "stream_frames", frames)
    # stop the producer once it has seen enough segments to have queued then shed
    orig = main.Heartbeat.set
    seen = {"n": 0}

    def counting_set(self, **kw):
        if "segments" in kw:
            seen["n"] = kw["segments"]
            if kw["segments"] >= 5:
                main._STOP.set()
        return orig(self, **kw)
    monkeypatch.setattr(main.Heartbeat, "set", counting_set)

    assert main.run_live(cfg, once=False) == 0
    hb = main.Heartbeat.read(cfg)
    assert hb["shed"] >= 1              # queue hit the cap and routine segments shed
    assert hb["novel"] <= cfg.stt_max_queue + 1   # only cap-worth queued before shedding
    main._STOP.clear()


def test_emit_alert_no_db_is_noop(tmp_path):
    # db None + webhook unset -> the skip-persistence branch; must not raise
    cfg = Config(out_dir=tmp_path)
    main._emit_alert(parse_header(_HDR), cfg, db=None)


def test_run_live_repeat_and_same_enabled(tmp_path, monkeypatch, make_cfg):
    from wxparser.db import Database
    cfg = make_cfg(same_enabled=True)
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
    # a stored raw report so run_live's text-dedup priming branch runs
    db.insert_raw_report({"id": "p", "captured_at": "2026-06-24T00:00:00Z",
                          "product_type": "zone_forecast", "text": "earlier forecast"})

    def frames(c, on_retry=None, should_stop=None):
        # 15 leading silence each so both segments get the FULL 10-frame pre-roll
        # and are byte-identical -> the second trips the novelty gate as a repeat.
        p = "s" * 15 + "S" * 70 + "s" * 60
        yield from _frames(p, c)                 # novel
        yield from _frames(p, c)                 # identical -> repeat (gated)
    monkeypatch.setattr(main, "stream_frames", frames)
    monkeypatch.setattr(main, "transcribe_samples", lambda s, c: _t("Highs around 80."))
    assert main.run_live(cfg, once=False) == 0   # SAME-enabled + priming + repeat paths
    main._STOP.clear()


def test_stt_worker_full_paths(tmp_path, monkeypatch, make_cfg):
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
    cfg = make_cfg()
    db = Database(cfg)
    db.clear()
    texts = iter([
        # OBS + forecast
        "At Muncie, the temperature was 80 degrees. Tonight, lows in the lower 60s.",
        # update (supersedes)
        "At Muncie, the temperature was 81 degrees. Tonight, lows in the lower 60s with showers.",
        # alert detail
        "Tornado warning for Delaware County until 630 PM. Take cover now spotter activation.",
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
    main._stt_worker(q, cfg, False, PipelineState(
        CityConditionsAggregator(), ForecastAggregator(), AlmanacAggregator(),
        deduper=TextDeduper(cfg), db=db, hb=Heartbeat(cfg)))
    # OBS, forecast, an alert detail, and an almanac field were all written
    assert db.all_conditions_for_city("Muncie")
    assert db.alert_details_between("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    assert {r["field"] for r in db.latest_almanac()} >= {"sunrise", "sunset"}


def test_stt_worker_once_stops_after_save(tmp_path, monkeypatch, make_cfg):
    # once=True + a saved segment must set _STOP and exit the loop -- covered here
    # deterministically rather than via the inherently racy run_live(once=True).
    import itertools
    import queue as _q

    from wxparser.db import Database
    from wxparser.extract import AlmanacAggregator, CityConditionsAggregator, ForecastAggregator
    from wxparser.health import Heartbeat
    main._STOP.clear()
    cfg = make_cfg()
    db = Database(cfg)
    db.clear()
    monkeypatch.setattr(main, "transcribe_samples",
                        lambda s, c: _t("At Muncie, the temperature was 80 degrees."))
    q = _q.PriorityQueue()

    class Seg:
        samples = np.zeros(16000, dtype=np.int16)
        duration_s = 1.0
    q.put((0, next(itertools.count()), (Seg(), "d")))   # one saveable segment
    main._stt_worker(q, cfg, True, PipelineState(
        CityConditionsAggregator(), ForecastAggregator(), AlmanacAggregator(),
        deduper=TextDeduper(cfg), db=db, hb=Heartbeat(cfg)))
    assert main._STOP.is_set()                           # once + save -> stop set, loop exits
    main._STOP.clear()


def test_stt_worker_fails_loud_on_store_error(monkeypatch, make_cfg):
    # a failure past transcribe (e.g. a DB outage outliving the reconnect) must
    # take the whole process down for systemd, not silently kill the worker
    # thread while the producer queues audio forever
    import itertools
    import queue as _q

    from wxparser.extract import (
        AlmanacAggregator,
        CityConditionsAggregator,
        ForecastAggregator,
    )
    main._STOP.clear()
    cfg = make_cfg()
    monkeypatch.setattr(main, "transcribe_samples",
                        lambda s, c: _t("At Muncie, the temperature was 80 degrees."))

    def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(main, "_handle_transcript", boom)
    died = []
    monkeypatch.setattr(main, "_die", lambda: died.append(True))
    q = _q.PriorityQueue()

    class Seg:
        samples = np.zeros(16000, dtype=np.int16)
        duration_s = 1.0
    q.put((0, next(itertools.count()), (Seg(), "d")))
    main._stt_worker(q, cfg, False, PipelineState(
        CityConditionsAggregator(), ForecastAggregator(), AlmanacAggregator(),
        deduper=TextDeduper(cfg), db=None, hb=None))
    assert died == [True] and main._STOP.is_set()        # fail-loud path taken
    main._STOP.clear()


def test_die_hard_exits(monkeypatch):
    codes = []
    monkeypatch.setattr(main.os, "_exit", lambda code: codes.append(code))
    main._die()
    assert codes == [1]


def test_stt_worker_low_confidence_stored_not_voted(tmp_path, monkeypatch, capsys, make_cfg):
    # a transcript whose measured confidence falls below the floor is saved to
    # the store but NOT voted into the aggregates, and the gate is logged
    import queue as _q

    from wxparser.db import Database
    from wxparser.extract import AlmanacAggregator, CityConditionsAggregator, ForecastAggregator
    from wxparser.health import Heartbeat
    main._STOP.clear()
    cfg = make_cfg(stt_confidence_floor=0.5)
    db = Database(cfg)
    db.clear()
    db._run("TRUNCATE raw_reports")
    garbled = Transcript(text="At Muncie, the temperature was 12 degrees.",
                         segments=[TranscriptSegment(0.0, 1.0, "x")], language="en",
                         avg_confidence=0.21)
    monkeypatch.setattr(main, "transcribe_samples", lambda s, c: garbled)
    q = _q.PriorityQueue()

    class Seg:
        samples = np.zeros(16000, dtype=np.int16)
        duration_s = 1.0
    q.put((0, 0, (Seg(), "d")))
    q.put((0, 1, None))                                  # poison pill
    main._stt_worker(q, cfg, False, PipelineState(
        CityConditionsAggregator(), ForecastAggregator(), AlmanacAggregator(),
        deduper=TextDeduper(cfg), db=db, hb=Heartbeat(cfg)))
    assert not db.all_conditions_for_city("Muncie")      # not voted
    assert db.count_raw_reports() == 1                    # still stored (in raw_reports)
    assert "low-conf 0.21" in capsys.readouterr().out    # gate logged


def test_emit_alert_with_db(tmp_path, make_cfg):
    from wxparser.db import Database
    cfg = make_cfg()
    db = Database(cfg)
    db.clear()
    main._emit_alert(parse_header(_HDR), cfg, db)
    assert db.alerts_history_count(None, None, None) == 1


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
    main._stt_worker(q, cfg, False, PipelineState(
        CityConditionsAggregator(), ForecastAggregator(), AlmanacAggregator(),
        deduper=TextDeduper(cfg), db=None, hb=Heartbeat(cfg)))


def test_stt_worker_handles_empty_queue():
    import queue as _q

    from wxparser.extract import (
        AlmanacAggregator,
        CityConditionsAggregator,
        ForecastAggregator,
    )
    main._STOP.clear()

    class _Q:
        def __init__(self):
            self.n = 0

        def get(self, timeout=None):
            self.n += 1
            if self.n == 1:
                raise _q.Empty              # first call: queue empty -> continue
            return (0, 0, None)             # then poison -> break

        def task_done(self):
            pass
    main._stt_worker(_Q(), Config(), False, PipelineState(
        CityConditionsAggregator(), ForecastAggregator(), AlmanacAggregator(),
        deduper=TextDeduper(Config())))


def test_run_live_breaks_when_stopped(tmp_path, monkeypatch, make_cfg):
    cfg = make_cfg(same_enabled=False)

    def frames(c, on_retry=None, should_stop=None):
        # run_live clears _STOP on entry, so simulate the stop arriving just after
        # startup (as capture begins) -> the producer's first segment hits the break.
        main._STOP.set()
        return _frames("s" + "S" * 70 + "s" * 60, c)
    monkeypatch.setattr(main, "stream_frames", frames)
    monkeypatch.setattr(main, "transcribe_samples", lambda s, c: _t("x"))
    assert main.run_live(cfg, once=False) == 0
    main._STOP.clear()


def test_main_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "run_file", lambda wav, cfg: 0)
    monkeypatch.setattr(main, "run_live", lambda cfg, once: 0)
    assert main.main(["--file", str(tmp_path / "a.wav")]) == 0
    assert main.main(["--once"]) == 0
