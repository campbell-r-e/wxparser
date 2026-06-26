"""Edge-case coverage across small modules."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np

from wxparser import notify
from wxparser.config import Config
from wxparser.db import _as_obj, _ts, period_window
from wxparser.extract import (
    ForecastAggregator,
    extract_alert_details,
    extract_observation,
    parse_temp_value,
)
from wxparser.fingerprint import _pool_time
from wxparser.formats import aprs_weather, sitrep
from wxparser.same import SAMEMonitor, decode, fips_county, looks_like_same, parse_header
from wxparser.segment import _finish
from wxparser.store import build_observation


# --- db helpers ----------------------------------------------------------- #
def test_period_window_variants():
    issued = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
    assert period_window("Today", issued)[0].endswith("06:00:00Z")
    assert period_window("Tonight", issued)[0].endswith("18:00:00Z")
    assert period_window("this evening", issued)[0].endswith("18:00:00Z")
    assert period_window("Friday", issued) != (None, None)        # weekday day window
    assert period_window("nonsense period", issued) == (None, None)


def test_db_value_coercers():
    assert _ts("already-a-string") == "already-a-string"
    assert _as_obj(None) is None
    assert _as_obj({"a": 1}) == {"a": 1}
    assert _as_obj('{"a": 1}') == {"a": 1}      # JSON string -> object


# --- store ---------------------------------------------------------------- #
def test_build_observation():
    o = build_observation({"temperature_f": {"value": 70}}, Config(),
                          captured_at="2026-06-24T12:00:00Z")
    assert o["type"] == "observation" and o["captured_at"] == "2026-06-24T12:00:00Z"
    assert o["id"]


# --- extract -------------------------------------------------------------- #
def test_extract_dewpoint_and_temp_parsing():
    assert extract_observation("The dewpoint was 65 degrees.")["dewpoint_f"] == 65
    assert parse_temp_value("in the 80s") == 85          # bare decade -> mid
    assert parse_temp_value("75") == 75                  # bare number
    assert parse_temp_value("nothing numeric here") is None


def test_extract_alert_wind_threat():
    d = extract_alert_details("Damaging winds to 60 mph and flash flooding are expected.")
    assert any("wind" in t for t in d.get("threats", []))
    assert "flash flood" in d.get("threats", [])


def test_forecast_prime_skips_nameless():
    fc = ForecastAggregator()
    fc.prime([{"low_f": 60}, {"period": "Tonight", "low_f": 61}])   # first has no period
    assert [p["period"] for p in fc.snapshot()] == ["Tonight"]


# --- formats -------------------------------------------------------------- #
def test_sitrep_spoken_and_negative_temp():
    snap = {"generated_at": "2026-06-24T12:00:00Z", "station": "KJY93", "city": "Muncie",
            "conditions": [{"condition": "temperature_f", "value": -5, "stale": False},
                           {"condition": "wind", "value": "north at 5"}],
            "roundup": [], "forecast": [],
            "alerts": [{"event": "TOR", "event_label": "Tornado Warning",
                        "counties": ["Delaware County, IN"], "expires_at": "2026-06-24T13:00:00Z",
                        "spoken": [{"until": "1 PM", "threats": ["tornado"]}]}]}
    out = sitrep(snap)
    assert "spoken: until 1 PM" in out
    assert "t-05" in aprs_weather(snap)                  # negative temp encoding


# --- segment / fingerprint ------------------------------------------------ #
def test_finish_empty_buffer_is_none():
    assert _finish([], 0.0, 1.0, Config()) is None


def test_pool_time_empty():
    out = _pool_time(np.zeros((0, 4)), 8)
    assert out.shape == (8, 4)


# --- same ----------------------------------------------------------------- #
def test_fips_and_short_audio_guards():
    assert fips_county("12345") is None                  # not 6 digits
    assert decode(np.zeros(100, dtype=np.float64)) == []  # too short
    assert looks_like_same(np.zeros(100, dtype=np.float64)) is False


def test_same_monitor_dedupes_repeat_header():
    from wxparser.same import encode
    cfg = Config(same_buffer_s=30.0)
    got = []
    mon = SAMEMonitor(cfg, on_alert=lambda m: got.append(m))
    audio = encode("ZCZC-WXR-TOR-018035+0045-1741830-KJY93-", sr=cfg.sample_rate)
    n = int(cfg.frame_seconds * cfg.sample_rate)

    def burst_then_silence():
        t = 0.0
        for i in range(0, len(audio), n):
            mon.feed(audio[i:i + n], t); t += cfg.frame_seconds
        for _ in range(int((cfg.same_silence_s + 0.5) / cfg.frame_seconds)):
            mon.feed(np.zeros(n, dtype=np.float64), t); t += cfg.frame_seconds
    burst_then_silence()
    burst_then_silence()                                 # same header again
    assert len(got) == 1                                 # deduped by raw header


# --- extract / formats / same / store / db extra branches ----------------- #
def test_field_voter_empty():
    from wxparser.extract import _FieldVoter
    assert _FieldVoter(5).best() is None


def test_city_conditions_prime():
    from wxparser.extract import CityConditionsAggregator
    agg = CityConditionsAggregator()
    agg.prime([{"city": "Muncie", "condition": "temperature_f", "value": 70}])
    out = {(r["city"], r["condition"]): r["value"]
           for r in agg.update("At Muncie, the temperature was 70 degrees.")}
    assert out[("Muncie", "temperature_f")] == 70


def test_sitrep_no_alerts_and_aprs_no_temp():
    from wxparser.formats import aprs_weather
    snap = {"generated_at": "2026-06-24T12:00:00Z", "station": "KJY93", "city": "Muncie",
            "conditions": [{"condition": "sky", "value": "clear"}], "roundup": [],
            "forecast": [], "alerts": []}
    assert "NONE" in sitrep(snap)                 # no-alert branch
    assert "t..." in aprs_weather(snap)            # missing temperature -> "..."


def test_decode_short_but_nonempty_audio():
    assert decode(np.zeros(5000, dtype=np.float64)) == []   # passes size guard, n_bits < 8


def test_store_filter_branches(tmp_path):
    import json as _json

    from wxparser.config import Config as _C
    from wxparser.store import count_reports, load_recent_reports, query_reports, reports_since
    d = tmp_path / "s"
    d.mkdir()
    (d / "reports.jsonl").write_text(
        "bad\n"
        + _json.dumps({"id": "1", "captured_at": "2026-06-24T10:00:00Z",
                       "product_type": "zone_forecast", "text": "tonight clear"}) + "\n"
        + _json.dumps({"id": "2", "captured_at": "2026-06-24T12:00:00Z",
                       "product_type": "current_conditions", "text": "temp 70"}) + "\n",
        encoding="utf-8")
    cfg = _C(out_dir=d)
    assert [r["id"] for r in query_reports(cfg, to="2026-06-24T11:00:00Z")] == ["1"]
    assert [r["id"] for r in query_reports(cfg, frm="2026-06-24T11:00:00Z")] == ["2"]  # frm filter
    assert count_reports(cfg, frm="2026-06-24T11:00:00Z") == 1                         # frm in matcher
    assert count_reports(cfg, to="2026-06-24T11:00:00Z", q="tonight") == 1
    assert count_reports(cfg, product="current_conditions") == 1
    assert count_reports(cfg, q="zzz-not-present") == 0       # q-mismatch branch
    assert [r["id"] for r in reports_since(cfg, "2026-06-24T11:00:00Z", 10)] == ["2"]
    assert [r["id"] for r in load_recent_reports(cfg, 10)] == ["1", "2"]   # skips bad line


def test_db_alert_now_param_history_filters_and_close():
    from wxparser.config import Config as _C
    from wxparser.db import Database
    db = Database(_C(pg_database="wxparser_test"))
    db.clear()
    db.write_alert({"id": "a1", "captured_at": "2026-06-24T06:00:00Z",
                    "alert": {"event": "TOR", "purge_minutes": 45, "raw": "Z",
                              "areas": [], "counties": []}})
    assert len(db.get_active_alerts(now="2026-06-24T06:30:00Z")) == 1   # now param branch
    db.write_forecast([{"period": "Tonight", "low_f": 60}], "2026-06-24T18:00:00Z", city="Muncie")
    assert db.forecast_history_count("2026-06-24T00:00:00Z", "2026-06-25T00:00:00Z", "Muncie") == 1
    total, _ = db.alerts_history("2026-06-24T00:00:00Z", "2026-06-25T00:00:00Z", "TOR", 10, 0)
    assert total == 1
    db.close()


def test_capture_exceeds_retry_budget(monkeypatch):
    import wxparser.capture as capture
    from wxparser.config import Config as _C

    class _P:
        stdout = type("S", (), {"read": staticmethod(lambda n: b"")})()
        def poll(self): return 1
        def terminate(self): pass
        def wait(self, timeout=None): pass
    cfg = _C(capture_retry_backoff_s=0.0, capture_max_retries=0)
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *a, **k: _P())
    try:
        next(capture.stream_frames(cfg))
        assert False, "expected RuntimeError after exhausting retries"
    except RuntimeError as e:
        assert "arecord failed" in str(e)


# --- notify --------------------------------------------------------------- #
def test_webhook_failure_is_swallowed(capsys):
    notify.post_webhook(Config(webhook_url="http://127.0.0.1:1/x", webhook_timeout_s=1),
                        "alert", {"id": "x"})
    time.sleep(0.5)                                       # let the daemon thread fail
    assert "webhook POST" in capsys.readouterr().err
