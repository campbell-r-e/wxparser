"""Fail-loud health assessment tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wxparser.config import CONFIG
from wxparser.health import assess

_NOW = datetime(2026, 6, 25, 18, 0, 0, tzinfo=timezone.utc)


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


def test_degraded_when_worker_wedged():
    # backlog queued but nothing transcribed recently -> STT worker stuck
    hb = {"updated_at": _ago(0.2), "last_segment_at": _ago(0.3),
          "last_stt_ok_at": _ago(10), "queue_depth": 3}
    r = assess(hb, CONFIG, _NOW)
    assert r["status"] == "degraded"
    assert any("wedged" in c for c in r["checks"])
