"""SQLite store round-trip tests (observations / forecasts / alerts)."""

from __future__ import annotations

from datetime import datetime, timezone

from wxparser.config import CONFIG
from wxparser.db import Database, period_window


def _db() -> Database:
    db = Database(CONFIG, database="wxparser_test")
    db.clear()  # isolate each test
    return db


def test_observation_roundtrip():
    db = _db()
    db.write_observation({
        "id": "o1", "captured_at": "2026-06-24T06:00:00Z", "station": "KJY93",
        "fields": {"temperature_f": {"value": 61, "votes": 3, "total": 4, "source": "voice"}},
    })
    cur = db.get_current()
    assert cur["fields"]["temperature_f"]["value"] == 61
    assert cur["station"] == "KJY93"


def test_forecast_roundtrip_with_valid_window():
    db = _db()
    db.write_forecast(
        [{"period": "Tonight", "low_f": 61, "precip_pct": 70, "sky": "partly cloudy"}],
        issued_at="2026-06-24T12:00:00Z",
    )
    fc = db.get_forecast()
    assert fc["periods"][0]["period"] == "Tonight"
    assert fc["periods"][0]["low_f"] == 61
    assert fc["periods"][0]["valid_from"] is not None  # window computed


def test_alert_active_then_expires():
    db = _db()
    rec = {
        "id": "a1", "captured_at": "2026-06-24T06:00:00Z",
        "alert": {"event": "TOR", "event_label": "Tornado Warning", "areas": ["018035"],
                  "counties": ["Delaware County, IN"], "purge_minutes": 45,
                  "issued_raw": "1741830", "station": "KJY93", "raw": "ZCZC-..."},
    }
    db.write_alert(rec)
    assert len(db.get_active_alerts(now="2026-06-24T06:30:00Z")) == 1   # within 45 min
    assert len(db.get_active_alerts(now="2026-06-24T07:00:00Z")) == 0   # expired


def test_period_window_weekday_night():
    issued = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)  # Wednesday
    vf, vt = period_window("Saturday Night", issued)
    assert vf is not None and vt is not None and vf < vt


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all db tests passed")


if __name__ == "__main__":
    _run()
