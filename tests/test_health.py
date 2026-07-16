"""Fail-loud health assessment tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wxparser.config import Config
from wxparser.timefmt import ISO_FMT

CONFIG = Config()  # a plain default config for the assess() tests below
from wxparser.health import Heartbeat, assess

_NOW = datetime(2026, 6, 25, 18, 0, 0, tzinfo=timezone.utc)


def test_heartbeat_roundtrip(tmp_path):
    cfg = Config(out_dir=tmp_path)
    hb = Heartbeat(cfg)
    assert Heartbeat.read(cfg) is None            # nothing flushed yet
    hb.touch("last_segment_at")
    hb.incr("segments"); hb.incr("segments")
    hb.set(queue_depth=3)
    hb.flush()
    d = Heartbeat.read(cfg)
    assert d["segments"] == 2 and d["queue_depth"] == 3
    assert d["last_segment_at"] is not None and d["updated_at"] is not None


def test_heartbeat_read_missing_is_none(tmp_path):
    assert Heartbeat.read(Config(out_dir=tmp_path / "absent")) is None


def _ago(mins: float) -> str:
    return (_NOW - timedelta(minutes=mins)).strftime(ISO_FMT)


def test_ok_when_signals_fresh():
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
          "last_novel_at": _ago(2), "last_stt_ok_at": _ago(1), "queue_depth": 0}
    assert assess(hb, CONFIG, _NOW)["status"] == "ok"


def _healthy_hb():
    return {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
            "last_novel_at": _ago(2), "last_stt_ok_at": _ago(1), "queue_depth": 0}


def test_degraded_when_conditions_stop_reaching_the_store():
    # Live 2026-07-16: audio flowing, STT draining, queue empty -- every plumbing
    # signal nominal -- while the novelty gate dropped each conditions re-read
    # before STT, so nothing was extracted for 10.5h. /now served 75F against an
    # actual 89.6F and /health still answered "all signals nominal". Stale DATA is
    # its own fault, independent of the pipeline looking healthy.
    out = assess(_healthy_hb(), CONFIG, _NOW, reading_at=_ago(10.5 * 60))
    assert out["status"] == "degraded"
    assert any("conditions not updating" in c for c in out["checks"])
    assert out["readings_stale_min"] == 630.0


def test_fresh_readings_keep_status_ok():
    out = assess(_healthy_hb(), CONFIG, _NOW, reading_at=_ago(20))
    assert out["status"] == "ok"
    assert out["readings_stale_min"] == 20.0


def test_no_readings_yet_is_not_a_fault():
    # a freshly installed box has an empty store; that is not extraction failing
    out = assess(_healthy_hb(), CONFIG, _NOW, reading_at=None)
    assert out["status"] == "ok"
    assert out["readings_stale_min"] is None


def test_stale_readings_do_not_soften_down_to_degraded():
    hb = {"updated_at": _ago(99), "last_segment_at": _ago(99),
          "last_novel_at": _ago(99), "last_stt_ok_at": _ago(99), "queue_depth": 0}
    assert assess(hb, CONFIG, _NOW, reading_at=_ago(600))["status"] == "down"


def test_down_when_no_heartbeat_file():
    assert assess(None, CONFIG, _NOW)["status"] == "down"


def test_down_when_heartbeat_stale():
    # heartbeat not flushed in minutes -> capture process is down (the novelty
    # drought also fires here but must not soften "down" to "degraded")
    hb = {"updated_at": _ago(10), "last_segment_at": _ago(10)}
    assert assess(hb, CONFIG, _NOW)["status"] == "down"


def test_degraded_when_audio_silent():
    # process alive (fresh heartbeat) but no segments -> deaf radio
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(10),
          "last_novel_at": _ago(2), "queue_depth": 0}
    r = assess(hb, CONFIG, _NOW)
    assert r["status"] == "degraded"
    assert any("deaf" in c for c in r["checks"])


def test_degraded_when_no_novel_speech():
    # dead-but-not-silent radio: static passes the VAD gate so segments keep
    # flowing (audio fresh), but everything fingerprints as a repeat and nothing
    # novel has queued in over an hour -> static/dead carrier, fail loud
    # (2026-07-07: 4h outage where every check stayed green)
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
          "last_novel_at": _ago(90), "last_stt_ok_at": _ago(1), "queue_depth": 0}
    r = assess(hb, CONFIG, _NOW)
    assert r["status"] == "degraded"
    assert any("novel" in c for c in r["checks"])
    assert r["last_novel_min"] == 90.0


def test_degraded_when_no_novel_reference():
    # neither last_novel_at nor started_at to measure the drought from -> can't
    # prove novel content ever arrived, so fail loud
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
          "last_stt_ok_at": _ago(1), "queue_depth": 0}
    r = assess(hb, CONFIG, _NOW)
    assert r["status"] == "degraded"
    assert any("novel" in c for c in r["checks"])


def test_not_wedged_during_startup_grace():
    # just booted: segments queued, first STT still running, nothing novel yet ->
    # not wedged, no novelty drought (both fall back to the recent process start)
    hb = {"updated_at": _ago(0.2), "started_at": _ago(0.4),
          "last_segment_at": _ago(0.3), "last_stt_ok_at": None, "queue_depth": 2}
    assert assess(hb, CONFIG, _NOW)["status"] == "ok"


def test_idle_then_single_segment_not_wedged():
    # looping broadcast idled STT for 30m, then ONE novel segment queues (q=1):
    # idle-then-busy, drains within a cycle -> NOT wedged (the overnight false
    # positive: a single just-queued segment is below the wedged-queue floor).
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
          "last_novel_at": _ago(0.3), "last_stt_ok_at": _ago(30), "queue_depth": 1}
    assert assess(hb, CONFIG, _NOW)["status"] == "ok"


def test_backlog_with_fresh_stt_not_wedged():
    # post-restart catch-up: queue building (q=4) but STT actively draining
    # (last ok 3m, under the wedge window) -> not wedged
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
          "last_novel_at": _ago(1), "last_stt_ok_at": _ago(3), "queue_depth": 4}
    assert assess(hb, CONFIG, _NOW)["status"] == "ok"


def test_degraded_when_worker_wedged():
    # a REAL backlog (q>1) stuck with nothing transcribed past the wedge window
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
          "last_novel_at": _ago(1), "last_stt_ok_at": _ago(15), "queue_depth": 3}
    r = assess(hb, CONFIG, _NOW)
    assert r["status"] == "degraded"
    assert any("wedged" in c for c in r["checks"])


def test_wedged_when_no_stt_reference():
    # backlog queued but neither last_stt_ok nor started_at to measure progress
    # from -> can't prove the worker is draining, so fail loud
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
          "last_novel_at": _ago(1), "last_stt_ok_at": None, "queue_depth": 2}
    r = assess(hb, CONFIG, _NOW)
    assert r["status"] == "degraded"
    assert any("wedged" in c for c in r["checks"])


def test_age_min_tolerates_corrupt_timestamp():
    from wxparser.health import _age_min
    now = datetime(2026, 6, 26, tzinfo=timezone.utc)
    assert _age_min("not-a-date", now) is None        # corrupt -> None, no crash
    assert _age_min(None, now) is None                # missing -> None
    assert _age_min("2026-06-26T00:00:00Z", now) is not None   # valid -> float


def test_flush_writes_through_to_db(tmp_path):
    class DBStub:
        def __init__(self):
            self.written = []

        def write_heartbeat(self, station, payload):
            self.written.append((station, payload))

    cfg = Config(out_dir=tmp_path)
    stub = DBStub()
    hb = Heartbeat(cfg, db=stub)
    hb.flush()
    station, payload = stub.written[-1]
    assert station == cfg.station and payload["updated_at"]
    assert Heartbeat.read(cfg)["updated_at"] == payload["updated_at"]  # file leg too


def test_flush_survives_db_failure(tmp_path):
    class ExplodingDB:
        def write_heartbeat(self, station, payload):
            raise RuntimeError("db down")

    cfg = Config(out_dir=tmp_path)
    hb = Heartbeat(cfg, db=ExplodingDB())
    hb.flush()                                # a DB outage must never crash capture
    assert Heartbeat.read(cfg)                # the file still flushed
