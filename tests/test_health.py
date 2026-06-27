"""Fail-loud health assessment tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wxparser.config import CONFIG, Config
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
    return (_NOW - timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_ok_when_signals_fresh():
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
          "last_stt_ok_at": _ago(1), "queue_depth": 0}
    assert assess(hb, CONFIG, _NOW)["status"] == "ok"


def test_down_when_no_heartbeat_file():
    assert assess(None, CONFIG, _NOW)["status"] == "down"


def test_down_when_heartbeat_stale():
    # heartbeat not flushed in minutes -> capture process is down
    hb = {"updated_at": _ago(10), "last_segment_at": _ago(10)}
    assert assess(hb, CONFIG, _NOW)["status"] == "down"


def test_degraded_when_audio_silent():
    # process alive (fresh heartbeat) but no segments -> deaf radio
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(10), "queue_depth": 0}
    r = assess(hb, CONFIG, _NOW)
    assert r["status"] == "degraded"
    assert any("deaf" in c for c in r["checks"])


def test_not_wedged_during_startup_grace():
    # just booted: a segment queued, first STT still running -> not yet wedged
    hb = {"updated_at": _ago(0.2), "started_at": _ago(0.4),
          "last_segment_at": _ago(0.3), "last_stt_ok_at": None, "queue_depth": 1}
    assert assess(hb, CONFIG, _NOW)["status"] == "ok"


def test_degraded_when_worker_wedged():
    # backlog queued but nothing transcribed recently -> STT worker stuck
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
          "last_stt_ok_at": _ago(10), "queue_depth": 3}
    r = assess(hb, CONFIG, _NOW)
    assert r["status"] == "degraded"
    assert any("wedged" in c for c in r["checks"])


def test_age_min_tolerates_corrupt_timestamp():
    from wxparser.health import _age_min
    now = datetime(2026, 6, 26, tzinfo=timezone.utc)
    assert _age_min("not-a-date", now) is None        # corrupt -> None, no crash
    assert _age_min(None, now) is None                # missing -> None
    assert _age_min("2026-06-26T00:00:00Z", now) is not None   # valid -> float
